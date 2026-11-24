import io                      # PDF, ZIP e imagens gerados na memória RAM
import json                    # cache das traduções
import os                      # navegação nas pastas do SSD do ANYmal
import pathlib                 # caminho da missão em formato de URL para o WeasyPrint
import re                      # limpeza de nomes de arquivo
import time                    # tempo de cada etapa (para o artigo)
import unicodedata             # remoção de acentos dos nomes de arquivo
import xml.etree.ElementTree as ET   # leitura e edição do report.xml
import zipfile                 # empacotamento final
from datetime import datetime, timedelta
from urllib.parse import unquote, urlparse

import lxml.etree as lxml_ET   # fusão do report.xml com o stylesheet.xsl
from PIL import Image, ImageOps
from weasyprint import CSS, HTML

# O WeasyPrint mudou a forma de buscar arquivos entre versões:
# as novas usam a classe URLFetcher; as antigas, a função default_url_fetcher.
try:
    from weasyprint.urls import URLFetcher, URLFetcherResponse
except ImportError:
    URLFetcher = None
    from weasyprint import default_url_fetcher


# ============================================================
# CONFIGURAÇÃO DO EXPERIMENTO
# ============================================================
#
# Pode ser alterada aqui ou por variável de ambiente, sem copiar o arquivo:
#
#   RELATORIO_LADO=600 RELATORIO_QUALIDADE=35 python3 servidor_web.py
#
LADO_MAX = int(os.environ.get("RELATORIO_LADO", "800"))
QUALIDADE_JPEG = int(os.environ.get("RELATORIO_QUALIDADE", "45"))

# 0 = não consulta o Google (usa só dicionário e cache). É o que o artigo descreve
# ("no external service is used"), por isso agora é o padrão. Use 1 para ligar.
TRADUCAO_ONLINE = os.environ.get("RELATORIO_TRADUCAO_ONLINE", "0") == "1"

# 0 = ZIP leva só o PDF (as fotos já estão dentro dele)
INCLUIR_IMAGENS_SEPARADAS = os.environ.get("RELATORIO_IMAGENS_SEPARADAS", "1") == "1"

PASTA_REMOTA = "/home/integration/.ros/reports"
PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))

# ---------------- COMPRESSÃO POR ROI ----------------
# Fotos com anotação do Roboflow: a peça fica em ROI_LADO / ROI_QUALIDADE e o resto
# vira fundo suavizado. Fotos sem anotação (ex.: térmicas) seguem LADO_MAX / QUALIDADE_JPEG.
#
#   RELATORIO_ROI=0                          desliga (volta ao comportamento do artigo)
#   RELATORIO_ROI_ANOTACOES=/caminho/export  .json COCO ou pasta exportada do Roboflow
#   RELATORIO_ROI_LADO=1600 RELATORIO_ROI_QUALIDADE=80
#   RELATORIO_ROI_FUNDO=400 RELATORIO_ROI_QUALIDADE_FUNDO=30
#   RELATORIO_ROI_MODO=auto|circulo|poligono RELATORIO_ROI_MARGEM=0.15
#   RELATORIO_ROI_TERMICA=0    desliga a região fixa da escala nas térmicas (1158×768)
ROI_LIGADA = os.environ.get("RELATORIO_ROI", "1") == "1"
ROI_ANOTACOES = os.environ.get("RELATORIO_ROI_ANOTACOES", os.path.join(PASTA_SCRIPT, "anotacoes_roi"))
ROI_LADO = int(os.environ.get("RELATORIO_ROI_LADO", "1600"))
ROI_QUALIDADE = int(os.environ.get("RELATORIO_ROI_QUALIDADE", "80"))
ROI_FUNDO = int(os.environ.get("RELATORIO_ROI_FUNDO", "400"))
ROI_QUALIDADE_FUNDO = int(os.environ.get("RELATORIO_ROI_QUALIDADE_FUNDO", "30"))
ROI_MODO = os.environ.get("RELATORIO_ROI_MODO", "auto")
ROI_TERMICA = os.environ.get("RELATORIO_ROI_TERMICA", "1") == "1"
ROI_MARGEM = float(os.environ.get("RELATORIO_ROI_MARGEM", "0.15"))

try:
    import compressor_roi
except ImportError:            # sem o módulo: funciona como antes
    compressor_roi = None

import configuracoes

ROI_ATIVA = bool(ROI_LIGADA and compressor_roi is not None and os.path.exists(ROI_ANOTACOES))

# ---------------- CONDIÇÃO PADRÃO ----------------
# Cada relatório pode pedir a sua (fila / experimento_pc.py). Sem pedido, vale esta:
#   RELATORIO_CONDICAO=roi:1200/70   (ou original, 1200/70, roi:1600/85/400/30 ...)
# Sem RELATORIO_CONDICAO, monta a partir das variáveis acima (compatível com antes).
if os.environ.get("RELATORIO_CONDICAO"):
    CONFIG_PADRAO = configuracoes.ler_condicao(os.environ["RELATORIO_CONDICAO"])
elif ROI_ATIVA:
    CONFIG_PADRAO = {"modo": "roi", "lado": LADO_MAX, "qualidade": QUALIDADE_JPEG,
                     "roi_lado": ROI_LADO, "roi_qualidade": ROI_QUALIDADE, "roi_fundo": ROI_FUNDO,
                     "roi_qualidade_fundo": ROI_QUALIDADE_FUNDO, "roi_modo": ROI_MODO}
    CONFIG_PADRAO["condicao"] = configuracoes.nome_curto(CONFIG_PADRAO)
else:
    CONFIG_PADRAO = configuracoes.ler_condicao(f"{LADO_MAX}/{QUALIDADE_JPEG}")

CONFIG_NOME = configuracoes.nome_arquivo(CONFIG_PADRAO)
ARQUIVO_CACHE = os.path.join(PASTA_SCRIPT, "cache_traducoes.json")
EXTENSOES_IMAGEM = (".png", ".jpg", ".jpeg")


# ============================================================
# BARRA DE PROGRESSO
# ============================================================

status_progresso = {"porcentagem": 0, "mensagem": "Aguardando..."}

# Preenchido a cada relatório; o servidor lê e grava no CSV de medições.
ultima_execucao = {}


def atualizar_progresso(pct, msg):
    status_progresso["porcentagem"] = pct
    status_progresso["mensagem"] = msg


# ============================================================
# CACHE DE TRADUÇÕES
# ============================================================

def carregar_cache():
    if os.path.exists(ARQUIVO_CACHE):
        try:
            with open(ARQUIVO_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def salvar_cache(cache_dados):
    temporario = ARQUIVO_CACHE + ".tmp"
    with open(temporario, "w", encoding="utf-8") as f:
        json.dump(cache_dados, f, ensure_ascii=False, indent=4)
    os.replace(temporario, ARQUIVO_CACHE)


# ============================================================
# DICIONÁRIO
# ============================================================

dicionario_anymal = {
    "Started mission": "Iniciou a missão", "Paused mission": "Missão pausada",
    "Resumed mission": "Missão retomada", "Navigating to": "Navegando para",
    "Successfully navigated to": "Navegou com sucesso para", "Failed to navigate to": "Falha ao navegar para",
    "The action goal failed during execution.": "A meta de ação falhou durante a execução.",
    "Inspecting": "Inspecionando", "from here": "daqui",
    "Successfully took image of": "Capturada imagem com sucesso de",
    "Successfully analyzed temperature of": "Temperatura analisada com sucesso de",
    "Successfully executed mission": "Missão executada com sucesso",
    "Failed to execute mission": "Falha ao executar a missão",
    "No valid battery state message received.": "Nenhuma mensagem válida de estado da bateria recebida.",
    "is above the threshold of": "está acima do limite de",
    "SwitchOperational Mode: Failed to connect to the Operational Mode Manager.": "Falha ao conectar ao Gerenciador de Modo Operacional.",
    "Performing Switch Operational Mode Dock failed": "Falha ao realizar o acoplamento do Modo Operacional."
}


# ============================================================
# TRADUTOR (cache primeiro; desiste do Google após a 1ª falha)
# ============================================================

class Tradutor:

    def __init__(self, memoria):
        self.memoria = memoria
        self.modificado = False
        self.online = TRADUCAO_ONLINE
        self.api = None
        if self.online:
            try:
                from deep_translator import GoogleTranslator   # só importa se for usar
                self.api = GoogleTranslator(source="en", target="pt")
            except Exception:
                self.online = False

    def traduzir(self, texto):
        if not texto:
            return ""
        if texto in self.memoria:
            return self.memoria[texto]
        if not self.online:
            return texto
        try:
            traducao = self.api.translate(texto)
        except Exception:
            # sem internet: não tenta de novo nesta execução
            self.online = False
            return texto
        if traducao:
            self.memoria[texto] = traducao
            self.modificado = True
            return traducao
        return texto


def traduzir_mensagem(texto_original, tradutor):
    if not texto_original:
        return ""
    texto_traduzido = texto_original
    modificado = False
    for en, pt in dicionario_anymal.items():
        if en in texto_traduzido:
            texto_traduzido = texto_traduzido.replace(en, pt)
            modificado = True
    if modificado:
        return texto_traduzido
    return tradutor.traduzir(texto_original)


# ============================================================
# AUXILIARES
# ============================================================

def converter_fuso_horario(hora_utc):
    if not hora_utc:
        return ""
    try:
        tempo = datetime.strptime(hora_utc, "%H:%M:%S")
        return (tempo - timedelta(hours=3)).strftime("%H:%M:%S")
    except ValueError:
        return hora_utc


def sanitizar_nome(nome):
    """Garante que o nome da foto não tenha caracteres inválidos"""
    nome = nome.replace("'", "").replace('"', "")
    nome = unicodedata.normalize("NFKD", nome).encode("ASCII", "ignore").decode("ASCII")
    nome = re.sub(r"[^\w\s-]", "", nome).strip()
    return re.sub(r"[-\s]+", "_", nome)


def normalizar_caminho(caminho):
    """Remove o './' da frente dos caminhos se existir"""
    c = caminho.replace("\\", "/")
    return c[2:] if c.startswith("./") else c


def nome_unico(nome, usados):
    """Evita dois arquivos com o mesmo nome dentro do ZIP"""
    base, extensao = os.path.splitext(nome)
    candidato = nome
    n = 2
    while candidato.lower() in usados:
        candidato = f"{base}_{n}{extensao}"
        n += 1
    usados.add(candidato.lower())
    return candidato


def listar_diretorio_nativo(caminho):
    itens = []
    if not os.path.exists(caminho):
        return None
    for nome in os.listdir(caminho):
        if not nome.startswith("."):
            caminho_abs = os.path.join(caminho, nome)
            itens.append({"nome": nome, "is_dir": os.path.isdir(caminho_abs), "caminho_absoluto": caminho_abs})
    itens.sort(key=lambda x: (not x["is_dir"], x["nome"].lower()))
    return itens


# ============================================================
# COMPRESSÃO DE IMAGEM (feita UMA vez, usada no PDF e no ZIP)
# ============================================================

_compressor = None   # CompressorROI da execução atual (None = só uniforme)
_cfg = dict(CONFIG_PADRAO)   # condição do relatório em andamento


def carregar_compressor_roi(cfg):
    """Lê as anotações a cada relatório (dá para atualizar sem reiniciar o servidor)."""
    if cfg["modo"] != "roi":
        return None
    if compressor_roi is None:
        raise RuntimeError("condição com ROI, mas o arquivo compressor_roi.py não está junto do servidor")
    tem_marcacoes = os.path.exists(ROI_ANOTACOES)
    if not tem_marcacoes and not ROI_TERMICA:
        raise RuntimeError(f"condição {cfg['condicao']} pede ROI, mas não há anotações em {ROI_ANOTACOES}")
    if not tem_marcacoes:
        print("AVISO: sem anotações do Roboflow; ROI só na escala das térmicas")
    return compressor_roi.CompressorROI(
        ROI_ANOTACOES if tem_marcacoes else None,
        lado=cfg["roi_lado"], qualidade=cfg["roi_qualidade"],
        lado_fundo=cfg["roi_fundo"], qualidade_fundo=cfg["roi_qualidade_fundo"],
        lado_sem_roi=cfg["lado"], qualidade_sem_roi=cfg["qualidade"],
        modo=cfg.get("roi_modo", "auto"), margem=ROI_MARGEM,
        regioes_fixas=None if ROI_TERMICA else {})


def info_roi():
    """Resumo para a interface web."""
    info = {"ativa": ROI_ATIVA, "caminho": ROI_ANOTACOES, "imagens_anotadas": 0,
            "config": CONFIG_PADRAO["condicao"], "termica": bool(ROI_TERMICA and compressor_roi)}
    if ROI_ATIVA:
        try:
            info["imagens_anotadas"] = len(compressor_roi.AnotacoesROI(ROI_ANOTACOES))
        except Exception:
            pass
    return info


def comprimir_imagem(caminho):
    if _compressor is not None:
        dados, _ = _compressor.comprimir(caminho)
        return dados
    lado, qualidade = _cfg["lado"], _cfg["qualidade"]
    with Image.open(caminho) as img:
        if img.format == "JPEG":
            # decodifica o JPEG já reduzido: bem mais rápido em fotos grandes
            img.draft("RGB", (lado, lado))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((lado, lado), Image.Resampling.LANCZOS)
        saida = io.BytesIO()
        img.save(saida, format="JPEG", optimize=True, quality=qualidade)
        return saida.getvalue()


def criar_url_fetcher(imagens_comprimidas):
    """Entrega ao WeasyPrint as imagens já comprimidas, em vez das originais."""

    def imagem_comprimida(url):
        """Devolve os bytes comprimidos se a URL for uma imagem local; senão None."""
        if not url.startswith("file:"):
            return None
        caminho = os.path.realpath(unquote(urlparse(url).path))
        if not (caminho.lower().endswith(EXTENSOES_IMAGEM) and os.path.isfile(caminho)):
            return None
        dados = imagens_comprimidas.get(caminho)
        if dados is None:
            try:
                dados = comprimir_imagem(caminho)
                imagens_comprimidas[caminho] = dados
            except Exception:
                return None
        return dados

    # WeasyPrint novo: classe
    if URLFetcher is not None:

        class RespostaComprimida(URLFetcherResponse):
            # Sem isto, o WeasyPrint vê que a URL é um arquivo local
            # e embute a foto ORIGINAL no PDF, ignorando a comprimida.
            @property
            def path(self):
                return None

        class BuscadorComprimido(URLFetcher):
            def fetch(self, url, headers=None):
                dados = imagem_comprimida(url)
                if dados is not None:
                    return RespostaComprimida(url, dados, {"Content-Type": "image/jpeg"})
                return super().fetch(url, headers)

        return BuscadorComprimido()

    # WeasyPrint antigo: função
    def buscar(url, *args, **kwargs):
        dados = imagem_comprimida(url)
        if dados is not None:
            # redirected_url não pode ser file:, senão o WeasyPrint relê a original
            return {"string": dados, "mime_type": "image/jpeg",
                    "redirected_url": url.replace("file:", "comprimida:", 1)}
        return default_url_fetcher(url, *args, **kwargs)

    return buscar


# ============================================================
# GERAÇÃO DO RELATÓRIO
# ============================================================

def gerar_relatorio_nativo(caminho_missao, condicao=None):
    """
    Gera o ZIP (PDF + fotos) de uma missão.

    condicao: None (usa CONFIG_PADRAO), um texto ('original', '1200/70', 'roi:1200/70')
              ou um dicionário de configuracoes.ler_condicao().
    """
    global _compressor, _cfg
    if condicao is None:
        _cfg = dict(CONFIG_PADRAO)
    elif isinstance(condicao, str):
        _cfg = configuracoes.ler_condicao(condicao)
    else:
        _cfg = dict(condicao)
    original = _cfg["modo"] == "original"

    ultima_execucao.clear()
    ultima_execucao["condicao"] = _cfg["condicao"]
    tempos = {}
    t_etapa = time.monotonic()

    atualizar_progresso(5, "Lendo arquivos da missão...")
    tradutor = Tradutor(carregar_cache())
    _compressor = carregar_compressor_roi(_cfg)

    caminho_xml = os.path.join(caminho_missao, "report.xml")
    caminho_xsl = os.path.join(caminho_missao, "stylesheet.xsl")
    if not os.path.exists(caminho_xsl):
        caminho_xsl = os.path.join(PASTA_REMOTA, "stylesheet.xsl")
    if not os.path.exists(caminho_xml):
        raise Exception(f"report.xml não encontrado na pasta: {caminho_missao}")

    # ---------------- ETAPA A: DADOS E TRADUÇÃO ----------------
    atualizar_progresso(10, "Extraindo e traduzindo dados...")
    tree = ET.parse(caminho_xml)
    root = tree.getroot()

    for tag in ["name", "location"]:
        elemento = root.find(tag)
        if elemento is not None and elemento.text:
            elemento.text = tradutor.traduzir(elemento.text)

    tag_data = root.find("date")
    if tag_data is not None and tag_data.text:
        data_original = tag_data.text.strip()
        try:
            if "/" in data_original:
                tag_data.text = datetime.strptime(data_original, "%Y/%m/%d").strftime("%d/%m/%Y")
            elif "-" in data_original:
                tag_data.text = datetime.strptime(data_original, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            pass

    entradas_validas = []
    for entry in root.findall("entry"):
        level = entry.find("level")
        if level is not None and level.text and level.text.strip().upper() == "WARN":
            root.remove(entry)
        else:
            entradas_validas.append(entry)

    total_entradas = len(entradas_validas)
    mapa_renomeio = {}
    contador_imagens = 1

    for i, entry in enumerate(entradas_validas):
        msg = entry.find("message")
        texto_msg = ""
        if msg is not None and msg.text:
            texto_msg = traduzir_mensagem(msg.text, tradutor)
            msg.text = texto_msg

        texto_hora = f"Evento_{i}"
        tag_time = entry.find("time_of_day")
        if tag_time is not None and tag_time.text:
            tag_time.text = converter_fuso_horario(tag_time.text)
            texto_hora = tag_time.text.replace(":", "-")

        tag_time_alt = entry.find("time")
        if tag_time_alt is not None and tag_time_alt.text:
            tag_time_alt.text = converter_fuso_horario(tag_time_alt.text)
            texto_hora = tag_time_alt.text.replace(":", "-")

        file_node = entry.find("file")
        if file_node is not None and file_node.text:
            caminho_antigo = file_node.text.strip()
            if caminho_antigo.lower().endswith(EXTENSOES_IMAGEM):
                nome_base = f"Inspecao_Local_{contador_imagens}"
                if "Capturada imagem com sucesso de" in texto_msg:
                    nome_base = texto_msg.split(" de ")[-1].strip()
                elif "Temperatura analisada com sucesso de" in texto_msg:
                    nome_base = "Termica_" + texto_msg.split(" de ")[-1].strip()
                elif "Inspecionando" in texto_msg:
                    nome_base = texto_msg.split("Inspecionando")[-1].split("daqui")[0].strip()

                novo_nome = f"{sanitizar_nome(nome_base)}_{texto_hora}{os.path.splitext(caminho_antigo)[1]}"
                pasta_original = os.path.dirname(normalizar_caminho(caminho_antigo))
                novo_caminho_zip = f"{pasta_original}/{novo_nome}" if pasta_original else novo_nome
                mapa_renomeio[normalizar_caminho(caminho_antigo)] = novo_caminho_zip
                contador_imagens += 1

        atualizar_progresso(10 + int((i + 1) / total_entradas * 20),
                            f"Processando evento {i + 1}/{total_entradas}...")

    if tradutor.modificado:
        salvar_cache(tradutor.memoria)

    tempos["t_traducao_s"] = time.monotonic() - t_etapa
    t_etapa = time.monotonic()

    # ---------------- ETAPA B: COMPRESSÃO DAS IMAGENS ----------------
    imagens = []
    for root_dir, _, files in os.walk(caminho_missao):
        for file in sorted(files):
            if file.lower().endswith(EXTENSOES_IMAGEM):
                imagens.append(os.path.join(root_dir, file))

    imagens_comprimidas = {}
    falhas = []
    for n, caminho in enumerate([] if original else imagens, 1):
        try:
            imagens_comprimidas[os.path.realpath(caminho)] = comprimir_imagem(caminho)
        except Exception:
            falhas.append(caminho)
        atualizar_progresso(30 + int(n / len(imagens) * 40),
                            f"Comprimindo imagem {n}/{len(imagens)} ({_cfg['condicao']})...")

    for caminho in falhas:
        print("AVISO: não foi possível comprimir, vai a original:", caminho)

    tempos["t_compressao_s"] = time.monotonic() - t_etapa
    t_etapa = time.monotonic()

    # ---------------- ETAPA C: PDF ----------------
    atualizar_progresso(72, "Montando o PDF...")

    with open(caminho_xsl, "r", encoding="utf-8") as file:
        xsl_str = file.read()

    dic_xsl = {
        "font-family: Impact, Charcoal, sans-serif;": "font-family: Arial, Helvetica, sans-serif;",
        "<title>Mission Report</title>": "<title>Relatório de Missão</title>",
        "MISSION REPORT": "RELATÓRIO DE MISSÃO",
        "<th>Level</th>": "<th>Nível</th>", "<th>Time</th>": "<th>Hora</th>",
        "<th>Event</th>": "<th>Evento</th>", "<th>Value</th>": "<th>Valor</th>",
        "<th>Status</th>": "<th>Situação</th>", "<th>File</th>": "<th>Arquivo</th>",
        "<th>Author</th>": "<th>Autor</th>", "<th>Message</th>": "<th>Mensagem</th>",
        "UTC Time": "Horário de Brasília", "Location:": "Local:", "Date:": "Data:", "Robot:": "Robô:"
    }
    for en, pt in dic_xsl.items():
        xsl_str = xsl_str.replace(en, pt)

    xml_lxml = lxml_ET.fromstring(ET.tostring(root, encoding="utf-8"))
    xsl_lxml = lxml_ET.fromstring(xsl_str.encode("utf-8"))
    html_content = str(lxml_ET.XSLT(xsl_lxml)(xml_lxml))

    url_base = pathlib.Path(caminho_missao).absolute().as_uri() + "/"
    css_pagina = CSS(string="@page { size: A4 landscape; margin: 1cm; } "
                            "img { max-width: 100%; height: auto; max-height: 300px; }")

    pdf_memoria = io.BytesIO()
    # original: sem url_fetcher próprio, o WeasyPrint usa as fotos como estão no disco
    extra = {} if original else {"url_fetcher": criar_url_fetcher(imagens_comprimidas)}
    HTML(string=html_content, base_url=url_base, **extra).write_pdf(
        pdf_memoria, stylesheets=[css_pagina])

    tempos["t_pdf_s"] = time.monotonic() - t_etapa
    t_etapa = time.monotonic()

    # ---------------- ETAPA D: ZIP ----------------
    atualizar_progresso(90, "Empacotando o relatório...")

    zip_memoria = io.BytesIO()
    # ZIP_STORED: JPEG e PDF já são comprimidos; recomprimir só gasta CPU
    with zipfile.ZipFile(zip_memoria, "w", zipfile.ZIP_STORED) as zipf:
        zipf.writestr("Relatorio_Traduzido_PT.pdf", pdf_memoria.getvalue())

        if INCLUIR_IMAGENS_SEPARADAS:
            usados = {"relatorio_traduzido_pt.pdf"}
            for caminho in imagens:
                relativo = normalizar_caminho(os.path.relpath(caminho, caminho_missao))
                novo_nome = mapa_renomeio.get(relativo, relativo)
                if novo_nome.startswith("resources/"):
                    novo_nome = novo_nome.replace("resources/", "imagens/", 1)

                dados = imagens_comprimidas.get(os.path.realpath(caminho))
                if dados is not None:
                    zipf.writestr(nome_unico(os.path.splitext(novo_nome)[0] + ".jpg", usados), dados)
                else:
                    zipf.write(caminho, nome_unico(novo_nome, usados))

    tempos["t_zip_s"] = time.monotonic() - t_etapa
    ultima_execucao.update(tempos)
    ultima_execucao.update({
        "imagens": len(imagens),
        "imagens_roi": _compressor.contagem["roi"] if _compressor else 0,
        "imagens_roi_termica": _compressor.contagem["fixa"] if _compressor else 0,
        "bytes_fotos": (sum(os.path.getsize(c) for c in imagens) if original
                        else sum(len(v) for v in imagens_comprimidas.values())),
        "bytes_pdf": len(pdf_memoria.getvalue()),
    })
    _compressor = None

    atualizar_progresso(100, "Concluído! Enviando o relatório...")
    zip_memoria.seek(0)
    return zip_memoria
