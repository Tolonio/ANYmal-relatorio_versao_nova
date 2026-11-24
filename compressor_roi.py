"""
Compressão por região de interesse (ROI) das fotos de inspeção do ANYmal.

Ideia: a peça inspecionada (marcada no Roboflow) fica com resolução e qualidade
altas; o resto da foto vira um fundo suavizado, que custa poucos bytes no JPEG.
A saída continua sendo UM arquivo JPEG comum, que abre em qualquer visualizador
e entra no PDF do WeasyPrint sem mudança nenhuma.

    foto original ──┬─► reduz para L×L (Lanczos) ───────────────┐
                    │                                           ├─► mistura pela máscara ─► JPEG qualidade Q
                    └─► reduz para F×F e amplia de volta ───────┘      (círculo + borda suave)
                        (fundo sem detalhe fino = barato)

Só depende de Pillow e numpy (já existem no robô). Compatível com Python 3.8.

Formatos de anotação aceitos (exportação do Roboflow):
  * COCO JSON  (_annotations.coco.json; uma pasta com train/valid/test também serve)
  * YOLO       (pasta com arquivos .txt; polígono de segmentação ou caixa)

Uso pela linha de comando:
  python3 compressor_roi.py verificar --anotacoes EXPORT --imagens FOTOS --saida conferencia/
  python3 compressor_roi.py comprimir --anotacoes EXPORT --imagens FOTOS --saida comprimidas/
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

try:
    _RS = Image.Resampling
except AttributeError:          # Pillow < 9.1
    _RS = Image

LANCZOS = _RS.LANCZOS
BICUBIC = _RS.BICUBIC
BOX = _RS.BOX

EXTENSOES_IMAGEM = (".png", ".jpg", ".jpeg")

# ------------------------------------------------------------
# Parâmetros padrão (podem ser trocados por quem chama)
# ------------------------------------------------------------
PADRAO = {
    "lado": 1600,          # lado máximo da foto entregue (resolução da ROI)
    "qualidade": 80,       # qualidade JPEG da foto entregue
    "lado_fundo": 400,     # resolução "efetiva" do fundo (menor = mais borrado e menor)
    "qualidade_fundo": 30, # pré-quantização do fundo (0 = desligada); ver compor_roi()
    "modo": "auto",        # "auto": círculo nas peças compactas, contorno nas alongadas;
                           # "circulo" ou "poligono" forçam um dos dois
    "margem": 0.15,        # círculo cresce 15% do raio para pegar a peça inteira
    "margem_min": 0.012,   # ... e pelo menos 1,2% do lado menor da foto
    "transicao": 0.02,     # largura da borda suave (fração do lado menor)
    "alongamento": 2.5,    # modo auto: se o círculo tem > 2,5× a área da peça, usa o contorno
}

# Regiões aplicadas a TODA foto de um tamanho, mesmo sem marcação (inclusive em
# missões novas). As térmicas (1158×768) têm a escala de temperatura sempre na
# faixa da direita: x de 0,864 a 1,0 em todas as 25 térmicas marcadas no Roboflow.
REGIOES_FIXAS = {
    (1158, 768): [{"classe": "escala_termica",
                   "poligono": [(0.86, 0.0), (1.0, 0.0), (1.0, 1.0), (0.86, 1.0)]}],
}


# ============================================================
# NOMES: Roboflow  <->  arquivos do robô
# ============================================================
# O Roboflow renomeia na exportação:
#   2026-05-20-15-10-07-0001.jpg -> 2026-05-20-15-10-07-0001_jpg.rf.3f9c2a....jpg
_RE_ROBOFLOW = re.compile(
    r"^(?P<base>.+?)_(?:jpe?g|png)\.rf\.[0-9A-Za-z]+\.(?:jpe?g|png|txt)$", re.IGNORECASE
)


def chave_imagem(nome: str) -> str:
    """Chave comparável entre o nome no Roboflow e o nome original no robô."""
    nome = os.path.basename(nome.replace("\\", "/"))
    achado = _RE_ROBOFLOW.match(nome)
    base = achado.group("base") if achado else os.path.splitext(nome)[0]
    # o Roboflow troca pontos/espaços por "_": normaliza tudo igual nos dois lados
    return re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")


# ============================================================
# LEITURA DAS ANOTAÇÕES
# ============================================================
# Cada região guarda o polígono em coordenadas NORMALIZADAS (0 a 1). Assim não
# importa se o Roboflow redimensionou a imagem na exportação (ex.: 640×640 "Stretch").

class AnotacoesROI:
    """Carrega uma exportação do Roboflow e responde 'quais regiões tem esta foto?'."""

    def __init__(self, caminho: str, classes: Optional[Sequence[str]] = None):
        self.caminho = caminho
        self.classes = set(c.lower() for c in classes) if classes else None
        self.regioes: Dict[str, List[dict]] = {}
        self.dimensoes: Dict[str, Tuple[int, int]] = {}   # tamanho da imagem exportada
        self.avisos: List[str] = []
        self._origens: Dict[str, str] = {}                # chave -> arquivo exportado

        if not caminho or not os.path.exists(caminho):
            raise FileNotFoundError(f"Anotações não encontradas: {caminho}")

        jsons, txts = [], []
        if os.path.isfile(caminho):
            (jsons if caminho.lower().endswith(".json") else txts).append(caminho)
        else:
            for raiz, _, arquivos in os.walk(caminho):
                for a in sorted(arquivos):
                    p = os.path.join(raiz, a)
                    if a.lower().endswith(".json"):
                        jsons.append(p)
                    elif a.lower().endswith(".txt") and _parece_rotulo_yolo(p):
                        txts.append(p)

        for p in jsons:
            self._ler_coco(p)
        for p in txts:
            self._ler_yolo(p)

        if not self.regioes:
            self.avisos.append(
                "Nenhuma região lida. Exporte do Roboflow em 'COCO Segmentation' "
                "(ou 'YOLOv8 Segmentation') e aponte para o .json ou para a pasta."
            )

    # ---------------- COCO ----------------
    def _ler_coco(self, caminho_json: str) -> None:
        try:
            with open(caminho_json, "r", encoding="utf-8") as f:
                dados = json.load(f)
        except Exception as erro:
            self.avisos.append(f"Não consegui ler {caminho_json}: {erro}")
            return
        if not isinstance(dados, dict) or "images" not in dados or "annotations" not in dados:
            return  # outro JSON qualquer na pasta

        categorias = {c.get("id"): str(c.get("name", c.get("id"))) for c in dados.get("categories", [])}
        imagens = {}
        for img in dados["images"]:
            nome = (img.get("extra") or {}).get("name") or img.get("file_name", "")
            imagens[img["id"]] = (nome, img.get("file_name", nome),
                                  float(img.get("width") or 0), float(img.get("height") or 0))

        por_imagem: Dict[str, List[dict]] = {}
        for ann in dados["annotations"]:
            info = imagens.get(ann.get("image_id"))
            if not info:
                continue
            nome, exportado, w, h = info
            if w <= 0 or h <= 0:
                continue
            classe = categorias.get(ann.get("category_id"), "")
            if self.classes and classe.lower() not in self.classes:
                continue

            poligonos = []
            seg = ann.get("segmentation")
            if isinstance(seg, list):
                for s in seg:
                    if isinstance(s, list) and len(s) >= 6:
                        poligonos.append([(s[i] / w, s[i + 1] / h) for i in range(0, len(s) - 1, 2)])
            if not poligonos and ann.get("bbox"):
                x, y, bw, bh = ann["bbox"]
                poligonos.append([(x / w, y / h), ((x + bw) / w, y / h),
                                  ((x + bw) / w, (y + bh) / h), (x / w, (y + bh) / h)])
            for pol in poligonos:
                por_imagem.setdefault(chave_imagem(nome), []).append(
                    {"classe": classe, "poligono": _limitar(pol)})
            self.dimensoes.setdefault(chave_imagem(nome), (int(w), int(h)))
            self._origens.setdefault(chave_imagem(nome), exportado)

        self._juntar(por_imagem, caminho_json)

    # ---------------- YOLO ----------------
    def _ler_yolo(self, caminho_txt: str) -> None:
        regioes = []
        with open(caminho_txt, "r", encoding="utf-8") as f:
            for linha in f:
                v = linha.split()
                if len(v) < 5:
                    continue
                classe = v[0]
                if self.classes and classe.lower() not in self.classes:
                    continue
                nums = [float(x) for x in v[1:]]
                if len(nums) == 4:      # caixa: cx cy w h
                    cx, cy, bw, bh = nums
                    pol = [(cx - bw / 2, cy - bh / 2), (cx + bw / 2, cy - bh / 2),
                           (cx + bw / 2, cy + bh / 2), (cx - bw / 2, cy + bh / 2)]
                else:                   # polígono: x1 y1 x2 y2 ...
                    pol = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
                regioes.append({"classe": classe, "poligono": _limitar(pol)})
        if regioes:
            self._juntar({chave_imagem(caminho_txt): regioes}, caminho_txt)

    def _juntar(self, por_imagem: Dict[str, List[dict]], origem: str) -> None:
        for chave, regioes in por_imagem.items():
            if chave in self.regioes:
                # mesma foto em outro split ou cópia aumentada (flip/rotação) do Roboflow
                if _assinatura(self.regioes[chave]) != _assinatura(regioes):
                    self.avisos.append(
                        f"'{chave}' aparece mais de uma vez com regiões diferentes "
                        f"(exportação com augmentation?). Mantida a primeira; ignorado {origem}."
                    )
                continue
            self.regioes[chave] = regioes

    # ---------------- consulta ----------------
    def regioes_para(self, caminho_imagem: str) -> Optional[List[dict]]:
        return self.regioes.get(chave_imagem(caminho_imagem))

    def conferir_proporcao(self, chave: str, largura: int, altura: int) -> Optional[str]:
        """Avisa se o Roboflow exportou com proporção diferente da foto (letterbox?)."""
        dim = self.dimensoes.get(chave)
        if not dim:
            return None
        r_exp, r_orig = dim[0] / dim[1], largura / altura
        if abs(r_exp - r_orig) / r_orig > 0.02:
            return (f"{chave}: exportada {dim[0]}×{dim[1]}, original {largura}×{altura}. "
                    "Certo apenas se o resize do Roboflow foi 'Stretch' (ou nenhum). "
                    "Com 'Fit/letterbox' a ROI fica deslocada: confira com o comando 'verificar'.")
        return None

    def __len__(self) -> int:
        return len(self.regioes)

    def resumo(self) -> str:
        n_reg = sum(len(r) for r in self.regioes.values())
        return f"{len(self.regioes)} imagens anotadas, {n_reg} regiões ({self.caminho})"


def _parece_rotulo_yolo(caminho: str) -> bool:
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            linha = f.readline().split()
        return len(linha) >= 5 and all(_eh_num(x) for x in linha)
    except Exception:
        return False


def _eh_num(x: str) -> bool:
    try:
        float(x)
        return True
    except ValueError:
        return False


def _limitar(pol):
    return [(min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)) for x, y in pol]


def _assinatura(regioes):
    return sorted(tuple(round(c, 3) for p in r["poligono"] for c in p) for r in regioes)


# ============================================================
# GEOMETRIA
# ============================================================

def circulo_minimo(pontos: Sequence[Tuple[float, float]]) -> Tuple[float, float, float]:
    """Menor círculo que contém todos os pontos (algoritmo de Welzl, incremental)."""
    pts = [(float(x), float(y)) for x, y in pontos]
    if not pts:
        raise ValueError("polígono vazio")
    random.Random(0).shuffle(pts)

    def dentro(c, p):
        return math.hypot(p[0] - c[0], p[1] - c[1]) <= c[2] * (1 + 1e-9) + 1e-9

    def de_dois(a, b):
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, math.hypot(a[0] - b[0], a[1] - b[1]) / 2)

    def de_tres(a, b, c):
        d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
        if abs(d) < 1e-12:  # colineares: o maior dos três círculos de dois pontos
            return max((de_dois(a, b), de_dois(a, c), de_dois(b, c)), key=lambda k: k[2])
        ux = ((a[0] ** 2 + a[1] ** 2) * (b[1] - c[1]) + (b[0] ** 2 + b[1] ** 2) * (c[1] - a[1])
              + (c[0] ** 2 + c[1] ** 2) * (a[1] - b[1])) / d
        uy = ((a[0] ** 2 + a[1] ** 2) * (c[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (a[0] - c[0])
              + (c[0] ** 2 + c[1] ** 2) * (b[0] - a[0])) / d
        return (ux, uy, math.hypot(a[0] - ux, a[1] - uy))

    c = (pts[0][0], pts[0][1], 0.0)
    for i, p in enumerate(pts):
        if dentro(c, p):
            continue
        c = (p[0], p[1], 0.0)
        for j in range(i):
            q = pts[j]
            if dentro(c, q):
                continue
            c = de_dois(p, q)
            for k in range(j):
                r = pts[k]
                if not dentro(c, r):
                    c = de_tres(p, q, r)
    return c


def circulos_roi(regioes: List[dict], largura: int, altura: int,
                 margem: float = PADRAO["margem"],
                 margem_min: float = PADRAO["margem_min"]) -> List[Tuple[float, float, float]]:
    """Um círculo (cx, cy, r) em pixels para cada região, já com a margem."""
    menor = min(largura, altura)
    circulos = []
    for reg in regioes:
        pts = [(x * largura, y * altura) for x, y in reg["poligono"]]
        cx, cy, r = circulo_minimo(pts)
        circulos.append((cx, cy, r * (1 + margem) + margem_min * menor))
    return circulos


def area_poligono(pts) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))) / 2


def separar_regioes(regioes: List[dict], largura: int, altura: int, modo: str = PADRAO["modo"],
                    alongamento: float = PADRAO["alongamento"]) -> Tuple[List[dict], List[dict]]:
    """(regiões protegidas por círculo, regiões protegidas pelo contorno)."""
    if modo == "circulo":
        return list(regioes), []
    if modo == "poligono":
        return [], list(regioes)
    if modo != "auto":
        raise ValueError(f"modo de ROI desconhecido: {modo}")
    circ, pol = [], []
    for reg in regioes:
        pts = [(x * largura, y * altura) for x, y in reg["poligono"]]
        if len(pts) < 3:
            circ.append(reg)
            continue
        _, _, r = circulo_minimo(pts)
        compacta = math.pi * r * r <= alongamento * max(area_poligono(pts), 1.0)
        (circ if compacta else pol).append(reg)
    return circ, pol


def mascara_poligonos(regioes: List[dict], largura: int, altura: int) -> Image.Image:
    """Máscara binária (L) só dos polígonos anotados: a peça em si (usada nas métricas)."""
    m = Image.new("L", (largura, altura), 0)
    d = ImageDraw.Draw(m)
    for reg in regioes:
        pts = [(x * largura, y * altura) for x, y in reg["poligono"]]
        if len(pts) >= 3:
            d.polygon(pts, fill=255)
    return m


def mascara_roi(regioes: List[dict], largura: int, altura: int,
                modo: str = PADRAO["modo"], margem: float = PADRAO["margem"],
                margem_min: float = PADRAO["margem_min"],
                transicao: float = PADRAO["transicao"],
                alongamento: float = PADRAO["alongamento"]) -> Image.Image:
    """
    Máscara (L, 0–255) da área protegida. Vale 255 em toda a área protegida
    (círculo com margem, ou contorno dilatado) e cai suavemente para 0 numa faixa
    de largura `transicao` POR FORA dela, para não aparecer uma borda dura.
    """
    menor = min(largura, altura)
    t = max(2.0, transicao * menor)
    circ, pol = separar_regioes(regioes, largura, altura, modo, alongamento)
    m = Image.new("L", (largura, altura), 0)

    if pol:
        d = ImageDraw.Draw(m)
        for reg in pol:
            pts = [(x * largura, y * altura) for x, y in reg["poligono"]]
            if len(pts) >= 3:
                d.polygon(pts, fill=255)
        # dilatação aproximada (borrar + limiar) para dar a margem em volta da peça
        dil = max(1.0, margem_min * menor + t / 2)
        m = m.filter(ImageFilter.GaussianBlur(dil / 2)).point(lambda v: 255 if v > 12 else 0)

    d = ImageDraw.Draw(m)
    for cx, cy, r in circulos_roi(circ, largura, altura, margem, margem_min):
        re_ = r + 0.65 * t   # borda borrada (σ = t/4) centrada fora: ≥ 99% de nitidez até r
        d.ellipse([cx - re_, cy - re_, cx + re_, cy + re_], fill=255)

    return m.filter(ImageFilter.GaussianBlur(t / 4))


# ============================================================
# COMPRESSÃO
# ============================================================

def abrir_imagem(caminho: str, lado_alvo: Optional[int] = None) -> Image.Image:
    """Abre já orientada (EXIF) e em RGB. Com JPEG usa draft() para decodificar reduzido."""
    with Image.open(caminho) as img:
        if lado_alvo and img.format == "JPEG":
            img.draft("RGB", (lado_alvo, lado_alvo))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.load()
        return img


def codificar_jpeg(img: Image.Image, qualidade: int, progressivo: bool = False) -> bytes:
    saida = io.BytesIO()
    img.save(saida, format="JPEG", quality=int(qualidade), optimize=True,
             progressive=bool(progressivo))
    return saida.getvalue()


def reduzir(img: Image.Image, lado: int) -> Image.Image:
    copia = img.copy()
    copia.thumbnail((lado, lado), LANCZOS)
    return copia


def comprimir_uniforme(img: Image.Image, lado: int, qualidade: int) -> bytes:
    """O mesmo da versão do artigo: reduz para caber em lado×lado e codifica com Q."""
    return codificar_jpeg(reduzir(img, lado), qualidade)


def compor_roi(img: Image.Image, regioes: List[dict], lado: int = PADRAO["lado"],
               lado_fundo: int = PADRAO["lado_fundo"],
               qualidade_fundo: int = PADRAO["qualidade_fundo"],
               **geo) -> Tuple[Image.Image, Image.Image]:
    """Devolve (imagem composta, máscara) na resolução de saída, antes do JPEG."""
    nitida = reduzir(img, lado)
    w, h = nitida.size

    # 1) fundo: reduz muito e amplia de volta -> sem detalhe fino
    fundo = img.copy()
    fundo.thumbnail((lado_fundo, lado_fundo), BOX, reducing_gap=2.0)
    fundo = fundo.resize((w, h), BICUBIC)

    # 2) pré-quantização: passa o fundo por um JPEG de qualidade baixa NO MESMO
    #    tamanho e na mesma grade de blocos 8×8/16×16 da saída. Os coeficientes DCT
    #    do fundo ficam múltiplos do passo grosso (quase todos zero); ao recodificar
    #    com a qualidade alta, quase todos continuam zero e o fundo sai barato.
    #    Só funciona porque a grade é a mesma: não recortar/deslocar depois daqui.
    if qualidade_fundo:
        fundo = Image.open(io.BytesIO(codificar_jpeg(fundo, qualidade_fundo))).convert("RGB")

    mascara = mascara_roi(regioes, w, h, **geo)
    return Image.composite(nitida, fundo, mascara), mascara


def comprimir_roi(img: Image.Image, regioes: List[dict], lado: int = PADRAO["lado"],
                  qualidade: int = PADRAO["qualidade"], lado_fundo: int = PADRAO["lado_fundo"],
                  qualidade_fundo: int = PADRAO["qualidade_fundo"],
                  progressivo: bool = False, **geo) -> Tuple[bytes, dict]:
    """Comprime protegendo as regiões. Retorna (bytes JPEG, informações)."""
    composta, mascara = compor_roi(img, regioes, lado, lado_fundo, qualidade_fundo, **geo)
    dados = codificar_jpeg(composta, qualidade, progressivo)
    area = float(np.asarray(mascara, dtype=np.float32).mean() / 255.0)
    return dados, {"regioes": len(regioes), "fracao_area_roi": area,
                   "largura": composta.size[0], "altura": composta.size[1]}


class CompressorROI:
    """
    Objeto usado pelo processador do relatório:
      foto marcada no Roboflow           -> ROI pelas marcações
      sem marcação, tamanho em REGIOES_FIXAS (térmica) -> ROI pela região fixa
      senão                              -> uniforme
    `anotacoes` pode ser None (só as regiões fixas).

        comp = CompressorROI("anotacoes_roi/", lado=1600, qualidade=80, lado_fundo=400,
                             lado_sem_roi=800, qualidade_sem_roi=45)
        dados, usou_roi = comp.comprimir("/caminho/foto.jpg")
    """

    def __init__(self, anotacoes, lado=PADRAO["lado"], qualidade=PADRAO["qualidade"],
                 lado_fundo=PADRAO["lado_fundo"], qualidade_fundo=PADRAO["qualidade_fundo"],
                 lado_sem_roi=800, qualidade_sem_roi=45,
                 modo=PADRAO["modo"], margem=PADRAO["margem"], margem_min=PADRAO["margem_min"],
                 transicao=PADRAO["transicao"], regioes_fixas=None):
        if anotacoes is None or isinstance(anotacoes, AnotacoesROI):
            self.anotacoes = anotacoes
        else:
            self.anotacoes = AnotacoesROI(anotacoes)
        self.regioes_fixas = REGIOES_FIXAS if regioes_fixas is None else regioes_fixas
        self.lado, self.qualidade, self.lado_fundo = int(lado), int(qualidade), int(lado_fundo)
        self.qualidade_fundo = int(qualidade_fundo or 0)
        self.lado_sem_roi, self.qualidade_sem_roi = int(lado_sem_roi), int(qualidade_sem_roi)
        self.geo = {"modo": modo, "margem": float(margem), "margem_min": float(margem_min),
                    "transicao": float(transicao)}
        self.contagem = {"roi": 0, "fixa": 0, "uniforme": 0}

    def regioes_para(self, caminho: str) -> Tuple[Optional[List[dict]], str]:
        """(regiões, origem) com origem 'marcacao', 'fixa' ou ''."""
        regioes = self.anotacoes.regioes_para(caminho) if self.anotacoes else None
        if regioes:
            return regioes, "marcacao"
        if self.regioes_fixas:
            try:
                with Image.open(caminho) as img:      # só lê o cabeçalho
                    tamanho = img.size
            except Exception:
                return None, ""
            if tamanho in self.regioes_fixas:
                return self.regioes_fixas[tamanho], "fixa"
        return None, ""

    def comprimir(self, caminho: str) -> Tuple[bytes, bool]:
        regioes, origem = self.regioes_para(caminho)
        if regioes:
            img = abrir_imagem(caminho, self.lado)
            dados, _ = comprimir_roi(img, regioes, self.lado, self.qualidade, self.lado_fundo,
                                     self.qualidade_fundo, **self.geo)
            self.contagem["roi"] += 1
            if origem == "fixa":
                self.contagem["fixa"] += 1
            return dados, True
        img = abrir_imagem(caminho, self.lado_sem_roi)
        self.contagem["uniforme"] += 1
        return comprimir_uniforme(img, self.lado_sem_roi, self.qualidade_sem_roi), False


# ============================================================
# LINHA DE COMANDO
# ============================================================

def listar_imagens(pasta: str) -> List[str]:
    achadas = []
    for raiz, _, arquivos in os.walk(pasta):
        for a in sorted(arquivos):
            if a.lower().endswith(EXTENSOES_IMAGEM):
                achadas.append(os.path.join(raiz, a))
    return achadas


def desenhar_conferencia(img: Image.Image, regioes: List[dict], lado: int = 1600, **geo) -> Image.Image:
    """Foto reduzida com marcação (amarelo), área protegida (borda ciano) e fundo escurecido."""
    base = reduzir(img, lado)
    w, h = base.size
    masc = mascara_roi(regioes, w, h, **geo)
    escura = Image.blend(base, Image.new("RGB", base.size, (0, 0, 0)), 0.55)
    saida = Image.composite(base, escura, masc)
    d = ImageDraw.Draw(saida)
    esp = max(2, w // 500)
    for reg in regioes:
        pts = [(x * w, y * h) for x, y in reg["poligono"]]
        if len(pts) >= 2:
            d.line(pts + [pts[0]], fill=(255, 214, 0), width=esp)
    # contorno da área protegida (onde a máscara passa de 50%)
    borda = masc.point(lambda v: 255 if v > 127 else 0).filter(ImageFilter.FIND_EDGES)
    borda = borda.filter(ImageFilter.MaxFilter(3 if esp < 3 else 5))
    saida.paste((0, 220, 255), mask=borda)
    return saida


def _cmd_verificar(args) -> int:
    anot = AnotacoesROI(args.anotacoes)
    print(anot.resumo())
    for a in anot.avisos:
        print("AVISO:", a)
    os.makedirs(args.saida, exist_ok=True)
    imagens = listar_imagens(args.imagens)
    chaves_fotos = {chave_imagem(p) for p in imagens}
    sem_foto = sorted(set(anot.regioes) - chaves_fotos)
    avisos_prop = []
    n = 0
    comp = CompressorROI(anot)
    n_fixa = 0
    for p in imagens:
        regs, origem = comp.regioes_para(p)
        if not regs:
            continue
        n_fixa += origem == "fixa"
        img = abrir_imagem(p)
        aviso = anot.conferir_proporcao(chave_imagem(p), *img.size)
        if aviso:
            avisos_prop.append(aviso)
        desenhar_conferencia(img, regs, args.lado, modo=args.modo, margem=args.margem).save(
            os.path.join(args.saida, chave_imagem(p) + "_roi.jpg"), quality=85)
        n += 1
    if avisos_prop:
        print(f"AVISO ({len(avisos_prop)} fotos): {avisos_prop[0]}")
    print(f"\nFotos na pasta: {len(imagens)} | com ROI: {n} (marcação: {n - n_fixa}, região fixa das térmicas: {n_fixa})"
          f" | sem ROI: {len(imagens) - n}")
    if sem_foto:
        print(f"Anotações sem foto correspondente ({len(sem_foto)}), ex.: {sem_foto[:5]}")
    print("Imagens de conferência em:", os.path.abspath(args.saida))
    return 0


def _cmd_comprimir(args) -> int:
    comp = CompressorROI(args.anotacoes, args.lado, args.qualidade, args.fundo, args.qualidade_fundo,
                         args.lado_sem_roi, args.qualidade_sem_roi, modo=args.modo, margem=args.margem)
    os.makedirs(args.saida, exist_ok=True)
    total_orig = total_novo = 0
    for p in listar_imagens(args.imagens):
        dados, usou = comp.comprimir(p)
        destino = os.path.join(args.saida, os.path.splitext(os.path.basename(p))[0] + ".jpg")
        with open(destino, "wb") as f:
            f.write(dados)
        total_orig += os.path.getsize(p)
        total_novo += len(dados)
        print(f"{'ROI ' if usou else 'unif'}  {len(dados) / 1024:8.1f} KB  {os.path.basename(p)}")
    print(f"\nROI: {comp.contagem['roi']}  uniforme: {comp.contagem['uniforme']}  "
          f"total {total_orig / 1048576:.1f} MB -> {total_novo / 1048576:.2f} MB")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compressão por ROI das fotos do ANYmal")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def comuns(p):
        p.add_argument("--anotacoes", required=True, help="exportação do Roboflow (.json ou pasta)")
        p.add_argument("--imagens", required=True, help="pasta com as fotos originais")
        p.add_argument("--saida", required=True)
        p.add_argument("--modo", default=PADRAO["modo"], choices=["auto", "circulo", "poligono"])
        p.add_argument("--margem", type=float, default=PADRAO["margem"])

    v = sub.add_parser("verificar", help="desenha as ROIs sobre as fotos para conferir")
    comuns(v)
    v.add_argument("--lado", type=int, default=1600)
    v.set_defaults(func=_cmd_verificar)

    c = sub.add_parser("comprimir", help="comprime uma pasta de fotos")
    comuns(c)
    c.add_argument("--lado", type=int, default=PADRAO["lado"])
    c.add_argument("--qualidade", type=int, default=PADRAO["qualidade"])
    c.add_argument("--fundo", type=int, default=PADRAO["lado_fundo"])
    c.add_argument("--qualidade-fundo", type=int, default=PADRAO["qualidade_fundo"], help="0 = sem pré-quantização")
    c.add_argument("--lado-sem-roi", type=int, default=800)
    c.add_argument("--qualidade-sem-roi", type=int, default=45)
    c.set_defaults(func=_cmd_comprimir)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
