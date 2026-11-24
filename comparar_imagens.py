"""
Compara as fotos dos relatórios baixados (ZIPs) com as fotos ORIGINAIS da inspeção.

Para cada foto e cada condição: tamanho e resolução antes e depois, redução, PSNR e SSIM
(na foto inteira e só na peça marcada / na escala térmica). Gera também um resumo por
condição e comparações lado a lado em HD para mostrar.

Uso (no PC, terminal do VS Code):

    python comparar_imagens.py --originais inspecoes_anymal/2026-05-20 --zips relatorios_anymal

    --originais   pasta da inspeção copiada do robô (a que tem report.xml e resources/)
    --zips        pasta com os ZIPs baixados (ou os próprios .zip)
    --saida       pasta de resultados (padrão: comparacao_<inspeção>)
    --exemplos 8  quantas fotos ganham a comparação lado a lado em HD
    --rapido      métricas em meia resolução (~4× mais rápido; para olhar, não para o artigo)
    --sem-metricas  só tamanhos e resoluções (segundos)

Saídas:
    imagens.csv        uma linha por foto e condição (abre no Excel)
    resumo.csv / .md   uma linha por condição
    comparacoes/       <foto>.jpg (foto inteira) e <foto>_detalhe.jpg (zoom na peça)

Liga cada foto do ZIP à original pelo mapa_imagens.csv que vem dentro do ZIP; nos ZIPs
antigos, sem esse arquivo, reconhece a foto pela imagem (miniaturas parecidas).

Dependências: numpy, pillow, scikit-image (python -m pip install -r requirements-pc.txt).
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import compressor_roi as cr

EXT = (".jpg", ".jpeg", ".png")


# ============================================================
# CONDIÇÃO A PARTIR DO NOME DO ZIP
# ============================================================

def condicao_do_nome(nome: str) -> str:
    """'600x600-ROI_2026-05-20_...zip' -> 'roi:600/35'; também entende o nome antigo."""
    base = os.path.basename(nome)[:-4]
    if base.startswith("Relatorio_PDF_") and "_PT_" in base:           # formato antigo
        cfg = base.rsplit("_PT_", 1)[1]
        cfg = re.sub(r"_\d{8}_\d{6}(_j[0-9a-f]{8})?$", "", cfg)
        if cfg == "original":
            return "original"
        m = re.match(r"^(?:(\d+)x\d+_q(\d+)_)?roi(\d+)q(\d+)f(\d+)(?:qf(\d+))?(pol|circ)?$", cfg)
        if m:
            return f"roi:{m.group(3)}/{m.group(4)}" + {"pol": "+pol", "circ": "+circ"}.get(m.group(7), "")
        m = re.match(r"^(\d+)x\d+_q(\d+)$", cfg)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        return cfg
    rot = base.split("_", 1)[0]                                         # formato novo
    if rot == "original":
        return "original"
    m = re.match(r"^(\d+)x\d+(?:-q(\d+))?(-ROI)?(-pol|-circ)?", rot)
    if not m:
        return rot
    lado = int(m.group(1))
    qual = int(m.group(2)) if m.group(2) else {600: 35, 800: 45, 1200: 70, 1600: 85}.get(lado, 0)
    s = ("roi:" if m.group(3) else "") + f"{lado}/{qual}"
    return s + {"-pol": "+pol", "-circ": "+circ"}.get(m.group(4) or "", "")


def chave_ordem(cond: str):
    if cond == "original":
        return (0, 0, 0, 0)
    m = re.match(r"^(roi:)?(\d+)/(\d+)", cond)
    if not m:
        return (9, 0, 0, cond)
    return (1, int(m.group(2)), int(m.group(3)), 1 if m.group(1) else 0)


def inspecao_do_nome(nome: str) -> str:
    base = os.path.basename(nome)[:-4]
    if base.startswith("Relatorio_PDF_") and "_PT_" in base:
        return base[len("Relatorio_PDF_"):].rsplit("_PT_", 1)[0]
    m = re.match(r"^[^_]+_(.+)_\d{8}-\d{6}", base)
    return m.group(1) if m else ""


# ============================================================
# CASAMENTO FOTO DO ZIP <-> ORIGINAL
# ============================================================

def assinatura(img: Image.Image) -> np.ndarray:
    """Miniatura 32×32 em cinza, normalizada: fotos iguais dão vetores quase iguais."""
    v = np.asarray(img.convert("L").resize((32, 32), Image.BILINEAR), dtype=np.float32).ravel()
    return (v - v.mean()) / (v.std() + 1e-6)


def assinatura_arquivo(caminho):
    with Image.open(caminho) as im:
        tam = im.size
        if im.format == "JPEG":
            im.draft("L", (128, 128))
        return assinatura(im), tam


def casar(fotos_zip, originais):
    """fotos_zip: [(nome, assinatura, (w,h))]; originais: {caminho: (assinatura, (w,h))}."""
    caminhos = list(originais)
    pares = []
    for i, (_, s, (w, h)) in enumerate(fotos_zip):
        for j, c in enumerate(caminhos):
            so, (wo, ho) = originais[c]
            if abs(w / h - wo / ho) > 0.03:
                continue
            pares.append((float(np.mean((s - so) ** 2)), i, j))
    pares.sort()
    usados_i, usados_j, res = set(), set(), {}
    for d, i, j in pares:
        if i in usados_i or j in usados_j:
            continue
        usados_i.add(i)
        usados_j.add(j)
        res[fotos_zip[i][0]] = (caminhos[j], d)
    return res


# ============================================================
# LEITURA DOS ZIPS
# ============================================================

def ler_zip(caminho_zip, originais_idx, cache_assin):
    """Devolve dados do pacote e a lista de fotos [(membro, caminho_original, roi, distancia)]."""
    z = zipfile.ZipFile(caminho_zip)
    nomes = z.namelist()
    # ZIPs novos têm tudo numa pasta (600x600-ROI_<inspeção>/...); os antigos, na raiz
    pdfs = [n for n in nomes if n.lower().endswith(".pdf")]
    mapas = [n for n in nomes if n.endswith("mapa_imagens.csv")]
    pacote = {"zip": os.path.basename(caminho_zip), "bytes_zip": os.path.getsize(caminho_zip),
              "bytes_pdf": z.getinfo(pdfs[0]).file_size if pdfs else 0,
              "condicao": condicao_do_nome(caminho_zip), "mapa": bool(mapas)}
    fotos = []
    if pacote["mapa"]:
        texto = z.read(mapas[0]).decode("utf-8-sig")
        for l in csv.DictReader(io.StringIO(texto)):
            pacote["condicao"] = l.get("condicao") or pacote["condicao"]
            orig = originais_idx["rel"].get(l["arquivo_original"]) or \
                originais_idx["base"].get(os.path.basename(l["arquivo_original"]))
            if orig and l["arquivo_no_zip"] in nomes:
                fotos.append((l["arquivo_no_zip"], orig, l.get("roi", ""), 0.0))
        return pacote, fotos
    # ZIP antigo: reconhece pela imagem
    membros = [n for n in nomes if n.lower().endswith(EXT)]
    lista = []
    for m in membros:
        with Image.open(io.BytesIO(z.read(m))) as im:
            lista.append((m, assinatura(im), im.size))
    for m, (c, d) in casar(lista, cache_assin).items():
        fotos.append((m, c, "", d))
    return pacote, fotos


# ============================================================
# MÉTRICAS (em paralelo, uma original por tarefa)
# ============================================================

def psnr(e2):
    mse = float(e2.mean()) if e2.size else 0.0
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def medir(tarefa):
    """(original, [(cond, zip, membro)], regioes, rapido, sem_metricas) -> linhas."""
    original, itens, regioes, rapido, sem_metricas = tarefa
    from skimage.metrics import structural_similarity
    linhas = []
    with Image.open(original) as im:
        wo, ho = im.size
    bytes_o = os.path.getsize(original)
    if not sem_metricas:
        img_o = cr.abrir_imagem(original)
        alvo = (wo // 2, ho // 2) if rapido else (wo, ho)
        if rapido:
            img_o = img_o.resize(alvo, cr.LANCZOS)
        cinza_o = np.asarray(img_o.convert("L"), dtype=np.float64)
        peca = (np.asarray(cr.mascara_poligonos(regioes, *alvo)) > 127) if regioes else None
    for cond, caminho_zip, membro in itens:
        with zipfile.ZipFile(caminho_zip) as z:
            dados = z.read(membro)
        comp = Image.open(io.BytesIO(dados))
        w, h = comp.size
        l = {"condicao": cond, "arquivo_original": os.path.basename(original), "arquivo_no_zip": membro,
             "tipo": "termica" if (wo, ho) == (1158, 768) else "rgb",
             "bytes_original": bytes_o, "bytes_comprimido": len(dados),
             "reducao_percent": round(100 * (1 - len(dados) / bytes_o), 2),
             "vezes_menor": round(bytes_o / len(dados), 2) if dados else "",
             "largura_original": wo, "altura_original": ho, "largura": w, "altura": h}
        if not sem_metricas:
            cinza_c = np.asarray(comp.convert("RGB").resize(alvo, cr.LANCZOS).convert("L"), dtype=np.float64)
            _, mapa = structural_similarity(cinza_o, cinza_c, data_range=255, full=True)
            e2 = (cinza_o - cinza_c) ** 2
            l.update(psnr=round(psnr(e2), 2), ssim=round(float(mapa.mean()), 4))
            if peca is not None and peca.any():
                l.update(psnr_peca=round(psnr(e2[peca]), 2), ssim_peca=round(float(mapa[peca].mean()), 4))
        linhas.append(l)
    return linhas


# ============================================================
# FIGURAS LADO A LADO
# ============================================================

def fonte(tam):
    for nome in ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(nome, tam)
        except OSError:
            continue
    return ImageFont.load_default()


def grade(paineis, colunas, destino):
    """paineis: [(Image, legenda)] -> uma imagem em grade, legenda embaixo de cada painel."""
    w = max(p.size[0] for p, _ in paineis)
    h = max(p.size[1] for p, _ in paineis)
    faixa = 44
    linhas = (len(paineis) + colunas - 1) // colunas
    saida = Image.new("RGB", (colunas * w + (colunas + 1) * 12, linhas * (h + faixa) + (linhas + 1) * 12), "white")
    d = ImageDraw.Draw(saida)
    f = fonte(24)
    for i, (p, leg) in enumerate(paineis):
        x = 12 + (i % colunas) * (w + 12)
        y = 12 + (i // colunas) * (h + faixa + 12)
        saida.paste(p, (x, y))
        d.text((x + 4, y + h + 8), leg, fill=(20, 20, 20), font=f)
    saida.save(destino, quality=92)


def figuras(original, itens, regioes, linhas, pasta, largura=1280):
    """Foto inteira em HD (largura 1280) e zoom na peça, original + cada condição."""
    base = os.path.splitext(os.path.basename(original))[0]
    img_o = cr.abrir_imagem(original)
    met = {(l["condicao"]): l for l in linhas}
    paineis, detalhes = [], []
    escala = largura / img_o.size[0]
    alvo = (largura, round(img_o.size[1] * escala))
    kb_o = os.path.getsize(original) / 1024
    paineis.append((img_o.resize(alvo, cr.LANCZOS), f"original · {kb_o:.0f} KB · {img_o.size[0]}×{img_o.size[1]}"))
    caixa = None
    if regioes:
        W, H = img_o.size
        xs = [x * W for r in regioes for x, _ in r["poligono"]]
        ys = [y * H for r in regioes for _, y in r["poligono"]]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        meio = max(max(xs) - min(xs), max(ys) - min(ys), 0.08 * min(W, H)) * 0.65
        caixa = (int(max(0, cx - meio)), int(max(0, cy - meio)), int(min(W, cx + meio)), int(min(H, cy + meio)))
        lado_det = 640
        det = img_o.crop(caixa)
        det = det.resize((lado_det, round(lado_det * det.size[1] / det.size[0])), cr.LANCZOS)
        detalhes.append((det, f"original · {kb_o:.0f} KB"))
    for cond, caminho_zip, membro in itens:
        with zipfile.ZipFile(caminho_zip) as z:
            comp = Image.open(io.BytesIO(z.read(membro))).convert("RGB")
        l = met.get(cond, {})
        ssim = f" · SSIM {l['ssim']:.3f}" if "ssim" in l else ""
        leg = f"{cond} · {l.get('bytes_comprimido', 0) / 1024:.0f} KB · {comp.size[0]}×{comp.size[1]}{ssim}"
        paineis.append((comp.resize(alvo, Image.NEAREST if comp.size[0] < alvo[0] / 1.5 else cr.LANCZOS), leg))
        if caixa:
            f = comp.size[0] / img_o.size[0]
            d = comp.crop(tuple(int(v * f) for v in caixa)).resize(detalhes[0][0].size, Image.NEAREST)
            ssim_p = f" · SSIM peça {l['ssim_peca']:.3f}" if "ssim_peca" in l else ""
            detalhes.append((d, f"{cond}{ssim_p}"))
    grade(paineis, 2, os.path.join(pasta, base + ".jpg"))
    if detalhes:
        grade(detalhes, 4, os.path.join(pasta, base + "_detalhe.jpg"))


# ============================================================
# PRINCIPAL
# ============================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--originais", required=True)
    ap.add_argument("--zips", required=True, nargs="+")
    ap.add_argument("--anotacoes", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "anotacoes_roi"))
    ap.add_argument("--saida", default=None)
    ap.add_argument("--exemplos", type=int, default=8)
    ap.add_argument("--rapido", action="store_true")
    ap.add_argument("--sem-metricas", action="store_true")
    ap.add_argument("--todos", action="store_true", help="não descarta ZIPs repetidos da mesma condição")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a = ap.parse_args(argv)

    t0 = time.time()
    fotos_orig = cr.listar_imagens(a.originais)
    if not fotos_orig:
        print("Nenhuma foto em", a.originais)
        return 1
    inspecao = os.path.basename(os.path.normpath(a.originais))
    if inspecao == "resources":
        inspecao = os.path.basename(os.path.dirname(os.path.normpath(a.originais)))
    saida = a.saida or f"comparacao_{inspecao}"
    os.makedirs(os.path.join(saida, "comparacoes"), exist_ok=True)

    raiz = os.path.normpath(a.originais)
    if os.path.basename(raiz) == "resources":
        raiz = os.path.dirname(raiz)
    idx = {"rel": {os.path.relpath(p, raiz).replace("\\", "/"): p for p in fotos_orig},
           "base": {os.path.basename(p): p for p in fotos_orig}}

    # ZIPs: os da mesma inspeção; por condição, fica o mais recente
    zips = []
    for z in a.zips:
        zips += [os.path.join(z, n) for n in sorted(os.listdir(z)) if n.endswith(".zip")] if os.path.isdir(z) else [z]
    da_insp = [z for z in zips if inspecao_do_nome(z) in (inspecao, "")]
    if da_insp:
        zips = da_insp
    else:
        print(f"Aviso: nenhum ZIP com '{inspecao}' no nome; usando todos.")
    print(f"{len(fotos_orig)} fotos originais em {a.originais}; {len(zips)} ZIPs")

    try:
        anot = cr.CompressorROI(cr.AnotacoesROI(a.anotacoes) if os.path.exists(a.anotacoes) else None)
    except Exception:
        anot = cr.CompressorROI(None)

    cache_assin = None
    pacotes, itens_por_orig, vistos = [], {}, {}
    for z in sorted(zips, key=os.path.getmtime, reverse=True):
        cond = condicao_do_nome(z)
        if not a.todos and cond in vistos:
            print(f"  (repetido, ignorado: {os.path.basename(z)}; fica {vistos[cond]})")
            continue
        try:
            with zipfile.ZipFile(z) as zz:
                tem_mapa = any(n.endswith("mapa_imagens.csv") for n in zz.namelist())
        except zipfile.BadZipFile:
            print(f"  ZIP corrompido ou incompleto, ignorado: {os.path.basename(z)}")
            continue
        if not tem_mapa and cache_assin is None:
            print("  ZIPs sem mapa_imagens.csv: reconhecendo as fotos pela imagem...")
            cache_assin = {p: assinatura_arquivo(p) for p in fotos_orig}
        pacote, fotos = ler_zip(z, idx, cache_assin or {})
        rotulo = pacote["condicao"] + (f" [{os.path.basename(z)[:12]}]" if a.todos and cond in vistos else "")
        vistos.setdefault(cond, os.path.basename(z))
        pacote["condicao"] = rotulo
        pacote["fotos"] = len(fotos)
        ruins = [f for f in fotos if f[3] > 0.35]
        if ruins:
            print(f"  {os.path.basename(z)}: {len(ruins)} fotos com casamento duvidoso (confira em imagens.csv)")
        pacotes.append(pacote)
        print(f"  {rotulo:<22} {pacote['bytes_zip'] / 1048576:8.2f} MB  {len(fotos)} fotos  ({os.path.basename(z)})")
        for membro, orig, _, dist in fotos:
            itens_por_orig.setdefault(orig, []).append((rotulo, z, membro, dist))

    if not itens_por_orig:
        print("Nenhuma foto dos ZIPs foi ligada às originais. Confira --originais e --zips.")
        return 1

    # métricas
    tarefas = []
    for orig, itens in itens_por_orig.items():
        regioes, _ = anot.regioes_para(orig)
        tarefas.append((orig, [(c, z, m) for c, z, m, _ in itens], regioes, a.rapido, a.sem_metricas))
    print(f"Calculando {'tamanhos' if a.sem_metricas else 'métricas'} de {sum(len(t[1]) for t in tarefas)} fotos "
          f"({a.workers} processos)...")
    linhas = []
    if a.workers > 1 and not a.sem_metricas:
        with ProcessPoolExecutor(a.workers) as ex:
            for i, ls in enumerate(ex.map(medir, tarefas, chunksize=2), 1):
                linhas += ls
                print(f"  {i}/{len(tarefas)}", end="\r")
    else:
        for t in tarefas:
            linhas += medir(t)
    print()
    dist = {(os.path.basename(o), c, m): d for o, its in itens_por_orig.items() for c, _, m, d in its}
    for l in linhas:
        l["distancia_casamento"] = round(dist.get((l["arquivo_original"], l["condicao"], l["arquivo_no_zip"]), 0), 4)
    linhas.sort(key=lambda l: (l["arquivo_original"], chave_ordem(l["condicao"])))
    escrever_csv(os.path.join(saida, "imagens.csv"), linhas)

    # resumo por condição
    resumo = []
    for p in sorted(pacotes, key=lambda p: chave_ordem(p["condicao"])):
        ls = [l for l in linhas if l["condicao"] == p["condicao"]]
        if not ls:
            continue
        bo, bc = sum(l["bytes_original"] for l in ls), sum(l["bytes_comprimido"] for l in ls)
        r = {"condicao": p["condicao"], "fotos": len(ls),
             "MB_originais": round(bo / 1048576, 2), "MB_comprimidas": round(bc / 1048576, 2),
             "reducao_percent": round(100 * (1 - bc / bo), 1), "vezes_menor": round(bo / bc, 1),
             "KB_medio_por_foto": round(bc / len(ls) / 1024, 1),
             "MB_zip": round(p["bytes_zip"] / 1048576, 2), "MB_pdf": round(p["bytes_pdf"] / 1048576, 2)}
        for k in ("psnr", "ssim", "psnr_peca", "ssim_peca"):
            v = [l[k] for l in ls if k in l]
            if v:
                r[k + "_medio"] = round(float(np.mean(v)), 4 if "ssim" in k else 2)
        resumo.append(r)
    escrever_csv(os.path.join(saida, "resumo.csv"), resumo)
    md = resumo_md(resumo, inspecao, a)
    with open(os.path.join(saida, "resumo.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(md)

    # comparações lado a lado: fotos com peça marcada primeiro, depois térmicas
    if a.exemplos:
        escolhidas = sorted(itens_por_orig, key=lambda o: (anot.regioes_para(o)[1] != "marcacao",
                                                           anot.regioes_para(o)[1] != "fixa", o))
        passo = max(1, len(escolhidas) // a.exemplos)
        escolhidas = (escolhidas[::passo])[:a.exemplos]
        print(f"Gerando {len(escolhidas)} comparações lado a lado em HD...")
        for o in escolhidas:
            itens = sorted([(c, z, m) for c, z, m, _ in itens_por_orig[o]], key=lambda t: chave_ordem(t[0]))
            figuras(o, itens, anot.regioes_para(o)[0],
                    [l for l in linhas if l["arquivo_original"] == os.path.basename(o)],
                    os.path.join(saida, "comparacoes"))
    print(f"Pronto em {time.time() - t0:.0f} s. Resultados em {os.path.abspath(saida)}")
    return 0


def escrever_csv(destino, linhas):
    campos = []
    for l in linhas:
        campos += [k for k in l if k not in campos]
    # utf-8-sig + ';' para abrir certo no Excel em português
    with open(destino, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        w.writeheader()
        for l in linhas:
            w.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in l.items()})


def resumo_md(resumo, inspecao, a):
    tem = lambda k: any(k in r for r in resumo)
    cab = ["Condição", "Fotos", "Originais (MB)", "Comprimidas (MB)", "Redução", "KB/foto", "ZIP (MB)"]
    if tem("ssim_medio"):
        cab += ["SSIM", "PSNR (dB)"]
    if tem("ssim_peca_medio"):
        cab += ["SSIM na peça"]
    l = [f"# Comparação das imagens: inspeção {inspecao}", "",
         f"Gerado em {datetime.now():%d/%m/%Y %H:%M}. Métricas {'em meia resolução' if a.rapido else 'na resolução original'}"
         " (em cinza, foto comprimida ampliada de volta com Lanczos).", "",
         "| " + " | ".join(cab) + " |", "|" + "---|" * len(cab)]
    for r in resumo:
        c = [r["condicao"], str(r["fotos"]), f"{r['MB_originais']:.1f}", f"{r['MB_comprimidas']:.2f}",
             f"{r['reducao_percent']:.1f}% ({r['vezes_menor']:.0f}×)", f"{r['KB_medio_por_foto']:.0f}", f"{r['MB_zip']:.2f}"]
        if tem("ssim_medio"):
            c += [f"{r.get('ssim_medio', 0):.3f}", f"{r.get('psnr_medio', 0):.1f}"]
        if tem("ssim_peca_medio"):
            c += [f"{r['ssim_peca_medio']:.3f}" if "ssim_peca_medio" in r else "—"]
        l.append("| " + " | ".join(c) + " |")
    l += ["", "SSIM vai de 0 a 1 (1 = idêntica à original). \"Na peça\" = só dentro da marcação do Roboflow "
          "ou da escala de temperatura das térmicas.", ""]
    return "\n".join(l)


if __name__ == "__main__":
    sys.exit(main())
