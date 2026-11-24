import csv
import logging
import os
import random
import re
import shutil
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime
from email.utils import formatdate

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

import configuracoes
from coletor import Coletor
from processador_relatorio import (
    CONFIG_PADRAO,
    PASTA_REMOTA,
    atualizar_progresso,
    gerar_relatorio_nativo,
    info_roi,
    listar_diretorio_nativo,
    status_progresso,
    ultima_execucao,
)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

CONFIG_EXPERIMENTO = CONFIG_PADRAO["condicao"]   # condição padrão (botão "Gerar relatório")

# Porta do servidor. A versão antiga usa 5050; a nova usa 5051 para as duas poderem
# rodar ao mesmo tempo. Para trocar: SERVIDOR_PORTA=8085 python3 servidor_web.py
PORTA = int(os.environ.get("SERVIDOR_PORTA", "5051"))

# Taxa do link usada para a ESTIMATIVA inicial de download (Mbps)
LINK_MBPS = 3.0

PASTA_RELATORIOS = os.path.expanduser("~/relatorios_gerados")
PASTA_COLETOR = os.path.join(PASTA_RELATORIOS, "coletor")
ARQUIVO_REGISTRO = os.path.join(PASTA_RELATORIOS, "registro_experimentos.csv")
ARQUIVO_LOG = os.path.join(PASTA_RELATORIOS, "servidor.log")

TAMANHO_BLOCO = 64 * 1024   # bytes enviados por vez
JANELA_TAXA_S = 5.0         # janela para calcular a taxa atual

# coletor de um job para sozinho se ninguém baixar o relatório neste tempo
COLETOR_ESPERA_DOWNLOAD_S = float(os.environ.get("COLETOR_ESPERA_DOWNLOAD_S", "1800"))

os.makedirs(PASTA_RELATORIOS, exist_ok=True)
os.makedirs(PASTA_COLETOR, exist_ok=True)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(ARQUIVO_LOG, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("servidor_anymal")


# ============================================================
# ESTADO
# ============================================================

PASTA_APP = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(PASTA_APP, "templates"),
            static_folder=os.path.join(PASTA_APP, "static"))
# logos ficam em cache no navegador por 1 dia: não voltam a passar pelo link limitado
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400


@app.context_processor
def variaveis_comuns():
    return {"config": CONFIG_EXPERIMENTO, "n_fila": len(fila) + (1 if job_ativo["id"] else 0)}


trava = threading.Lock()
trava_registro = threading.Lock()
tem_trabalho = threading.Condition(trava)

jobs = {}                  # job_id -> dados do relatório
fila = deque()             # job_ids esperando, na ordem
job_ativo = {"id": None}   # o que está processando agora (um por vez)
downloads = {}             # nome do arquivo -> estado do download
coletores = {}             # job_id (ou "manual") -> Coletor


# ============================================================
# REGISTRO DAS MEDIÇÕES (CSV)
# ============================================================

CAMPOS_REGISTRO = [
    "data_hora",
    "evento",               # processamento | download
    "job_id",
    "lote",                 # jobs enfileirados juntos
    "condicao",             # original | 1200/70 | roi:1200/70 ...
    "repeticao",
    "missao",
    "arquivo",
    "status",               # ok | erro | completo | interrompido
    "tamanho_bytes",
    "inicio_byte",          # > 0 quando o download foi retomado
    "bytes_transferidos",
    "espera_fila_s",        # tempo entre pedir e começar a processar
    "duracao_s",
    "taxa_media_Mbps",
    "imagens",              # fotos na missão
    "imagens_roi",          # quantas foram comprimidas por ROI (marcação + térmicas)
    "imagens_roi_termica",  # dessas, quantas pela região fixa da escala térmica
    "t_traducao_s",         # tempos de cada etapa do processamento
    "t_compressao_s",
    "t_pdf_s",
    "t_zip_s",
    "coletor_csv",
    "observacao",
]

CAMPOS_ETAPAS = ("imagens", "imagens_roi", "imagens_roi_termica", "t_traducao_s", "t_compressao_s", "t_pdf_s", "t_zip_s")


def registrar(**dados):
    linha = {campo: dados.get(campo, "") for campo in CAMPOS_REGISTRO}
    linha["data_hora"] = datetime.now().isoformat(timespec="seconds")
    with trava_registro:
        arquivar_registro_antigo()
        novo = not os.path.exists(ARQUIVO_REGISTRO)
        with open(ARQUIVO_REGISTRO, "a", newline="", encoding="utf-8") as arquivo:
            escritor = csv.DictWriter(arquivo, fieldnames=CAMPOS_REGISTRO)
            if novo:
                escritor.writeheader()
            escritor.writerow(linha)


def arquivar_registro_antigo():
    """Se o CSV tem colunas antigas, renomeia em vez de misturar formatos."""
    if not os.path.exists(ARQUIVO_REGISTRO):
        return
    with open(ARQUIVO_REGISTRO, "r", encoding="utf-8") as arquivo:
        cabecalho = arquivo.readline().strip().split(",")
    if cabecalho != CAMPOS_REGISTRO:
        carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.replace(ARQUIVO_REGISTRO, ARQUIVO_REGISTRO.replace(".csv", f"_antigo_{carimbo}.csv"))


# ============================================================
# AUXILIARES
# ============================================================

def ler_progresso_processador():
    try:
        dados = dict(status_progresso)
    except Exception:
        return None, ""
    pct = dados.get("porcentagem")
    pct = float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None
    return pct, str(dados.get("mensagem", ""))


def dentro_da_base(caminho):
    """True só se o caminho estiver dentro de PASTA_REMOTA (bloqueia o resto do robô)."""
    try:
        base = os.path.realpath(PASTA_REMOTA)
        alvo = os.path.realpath(caminho)
        return os.path.commonpath([base, alvo]) == base
    except (ValueError, TypeError):
        return False


def salvar_resultado(resultado, destino):
    """Salva o ZIP no disco. Grava em .parcial e renomeia no final."""
    temporario = destino + ".parcial"
    if isinstance(resultado, (bytes, bytearray)):
        with open(temporario, "wb") as arquivo:
            arquivo.write(resultado)
    elif isinstance(resultado, str) and os.path.isfile(resultado):
        shutil.copyfile(resultado, temporario)
    elif hasattr(resultado, "read"):
        if hasattr(resultado, "seek"):
            resultado.seek(0)
        with open(temporario, "wb") as arquivo:
            shutil.copyfileobj(resultado, arquivo, TAMANHO_BLOCO)
    else:
        raise TypeError("Formato de retorno inesperado do processador: " + type(resultado).__name__)
    os.replace(temporario, destino)


def tamanho_legivel(n):
    for unidade in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidade == "GB":
            return f"{n:.0f} {unidade}" if unidade == "B" else f"{n:.1f} {unidade}"
        n /= 1024


def contar_fotos(pasta):
    try:
        return sum(1 for _, _, arquivos in os.walk(pasta)
                   for a in arquivos if a.lower().endswith((".jpg", ".jpeg", ".png")))
    except OSError:
        return None


def ler_nome_relatorio(nome):
    """Relatorio_PDF_<missao>_PT_<config>_<AAAAmmdd_HHMMSS>[_jobid].zip -> partes."""
    base = nome[:-4]
    missao, config, data = base, "", ""
    base = re.sub(r"_j[0-9a-f]{8}$", "", base)
    if base.startswith("Relatorio_PDF_") and "_PT_" in base:
        missao, resto = base[len("Relatorio_PDF_"):].rsplit("_PT_", 1)
        if len(resto) > 16 and re.match(r"\d{8}_\d{6}$", resto[-15:]):
            config, carimbo = resto[:-16], resto[-15:]
            data = datetime.strptime(carimbo, "%Y%m%d_%H%M%S").strftime("%d/%m/%Y %H:%M")
        else:
            config = resto
    return missao, config, data


def nome_seguro(texto):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", texto).strip("_") or "missao"


def host_valido(h):
    """Só IP/nome simples: vai como argumento do ping (nada de '-opção')."""
    return h if h and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", h) else None


def ip_cliente():
    """IP de quem pediu (o PC), para o coletor medir o ping até ele."""
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip() or None


# ============================================================
# FILA
# ============================================================

def enfileirar(missao, condicoes, repeticoes=1, embaralhar=False, coletor=False,
               ping_host=None, rotulo=""):
    """Cria um job por (condição × repetição) e põe na fila. Devolve a lista de ids."""
    cfgs = [configuracoes.ler_condicao(c) for c in condicoes]   # valida tudo antes
    pedidos = []
    for rep in range(1, int(repeticoes) + 1):
        bloco = list(cfgs)
        if embaralhar:
            random.shuffle(bloco)          # ordem aleatória dentro de cada repetição
        pedidos += [(rep, c) for c in bloco]

    lote = uuid.uuid4().hex[:6]
    nome_pasta = nome_seguro(os.path.basename(missao.rstrip("/")))
    ids = []
    with tem_trabalho:
        for rep, cfg in pedidos:
            job_id = uuid.uuid4().hex[:8]
            carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
            jobs[job_id] = {
                "id": job_id, "lote": lote, "rotulo": rotulo,
                "missao": missao, "config": cfg, "condicao": cfg["condicao"], "repeticao": rep,
                "etapa": "na_fila", "erro": "",
                "arquivo": (f"Relatorio_PDF_{nome_pasta}_PT_{configuracoes.nome_arquivo(cfg)}"
                            f"_{carimbo}_j{job_id}.zip"),
                "coletor": bool(coletor), "ping_host": host_valido(ping_host), "coletor_csv": "",
                "t_pedido": time.monotonic(), "pedido_em": datetime.now().isoformat(timespec="seconds"),
                "t_inicio": None, "espera_fila_s": None,
                "tempo_processamento": None, "tamanho": None, "etapas": {},
            }
            fila.append(job_id)
            ids.append(job_id)
        tem_trabalho.notify()
    log.info("Fila: +%d job(s) do lote %s (%s)", len(ids), lote, ", ".join(c["condicao"] for _, c in pedidos))
    return ids


def trabalhador():
    """Thread única: processa os jobs da fila, um de cada vez, na ordem."""
    while True:
        with tem_trabalho:
            while not fila:
                tem_trabalho.wait()
            job_id = fila.popleft()
            job_ativo["id"] = job_id
        try:
            processar(job_id)
        except Exception:
            log.error("Falha inesperada no trabalhador:\n%s", traceback.format_exc())
        finally:
            with trava:
                job_ativo["id"] = None


def iniciar_coletor(job):
    nome = f"{job['id']}_{nome_seguro(job['condicao'])}_r{job['repeticao']}.csv"
    c = Coletor(os.path.join(PASTA_COLETOR, nome), ping_host=job.get("ping_host"),
                max_segundos=COLETOR_ESPERA_DOWNLOAD_S + 3 * 3600)
    c.fase = "processando"
    try:
        c.iniciar()
    except Exception as erro:
        log.warning("Coletor não iniciou: %s", erro)
        return
    coletores[job["id"]] = c
    job["coletor_csv"] = nome


def parar_coletor(job_id, motivo=""):
    c = coletores.get(job_id)
    if c is None:
        return None
    resumo = c.parar()                 # grava resumo e tc_depois ANTES de sair da lista de ativos
    coletores.pop(job_id, None)
    log.info("Coletor do job %s parado (%s): %s", job_id, motivo, resumo)
    if job_id in jobs:
        jobs[job_id]["coletor_resumo"] = resumo
    return resumo


def vigiar_coletor(job_id):
    """Para o coletor se o relatório não for baixado a tempo (evita coletor eterno)."""
    def vigia():
        limite = time.monotonic() + COLETOR_ESPERA_DOWNLOAD_S
        while time.monotonic() < limite:
            if job_id not in coletores:
                return
            estado = downloads.get(jobs[job_id]["arquivo"])
            if estado and estado["concluido"]:
                return
            time.sleep(2)
        if not (downloads.get(jobs[job_id]["arquivo"]) or {}).get("ativo"):
            parar_coletor(job_id, "ninguém baixou o relatório")
    threading.Thread(target=vigia, daemon=True).start()


def processar(job_id):
    job = jobs[job_id]
    job["etapa"] = "processando"
    job["t_inicio"] = time.monotonic()
    job["espera_fila_s"] = job["t_inicio"] - job["t_pedido"]
    inicio = job["t_inicio"]
    atualizar_progresso(0, "Iniciando processamento...")
    if job["coletor"]:
        iniciar_coletor(job)
    log.info("Processamento iniciado: %s (%s, rep. %s)", job["missao"], job["condicao"], job["repeticao"])

    comum = dict(evento="processamento", job_id=job_id, lote=job["lote"], condicao=job["condicao"],
                 repeticao=job["repeticao"], missao=job["missao"], arquivo=job["arquivo"],
                 espera_fila_s=round(job["espera_fila_s"], 2), coletor_csv=job["coletor_csv"])
    try:
        resultado = gerar_relatorio_nativo(job["missao"], job["config"])
        job["tempo_processamento"] = time.monotonic() - inicio

        destino = os.path.join(PASTA_RELATORIOS, job["arquivo"])
        salvar_resultado(resultado, destino)
        job["tamanho"] = os.path.getsize(destino)
        job["etapas"] = {k: ultima_execucao.get(k) for k in CAMPOS_ETAPAS if k in ultima_execucao}
        job["etapa"] = "pronto"
        if job_id in coletores:
            coletores[job_id].fase = "aguardando_download"
            vigiar_coletor(job_id)

        log.info("Processamento concluído em %.1f s, arquivo %s (%d bytes)",
                 job["tempo_processamento"], job["arquivo"], job["tamanho"])
        registrar(status="ok", tamanho_bytes=job["tamanho"],
                  duracao_s=round(job["tempo_processamento"], 2),
                  **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in job["etapas"].items()},
                  **comum)

    except Exception as erro:
        job["tempo_processamento"] = time.monotonic() - inicio
        job["etapa"] = "erro"
        job["erro"] = str(erro)
        log.error("Erro no processamento:\n%s", traceback.format_exc())
        registrar(status="erro", duracao_s=round(job["tempo_processamento"], 2),
                  observacao=str(erro)[:200], **comum)
        parar_coletor(job_id, "erro")
        try:
            atualizar_progresso(100, "Erro durante o processamento.")
        except Exception:
            pass


# ============================================================
# DOWNLOAD COM PROGRESSO E RETOMADA
# ============================================================

def estado_download_publico(estado):
    tamanho = estado["tamanho"]
    recebido = estado["inicio_byte"] + estado["enviados"]
    taxa = estado["taxa_bps"]
    restante = max(0, tamanho - recebido)
    return {
        "ativo": estado["ativo"],
        "concluido": estado["concluido"],
        "tamanho_bytes": tamanho,
        "recebido_bytes": recebido,
        "pct": round(recebido / tamanho * 100, 1) if tamanho else 100.0,
        "taxa_Mbps": round(taxa * 8 / 1e6, 3) if taxa else None,
        "eta_s": round(restante / taxa, 1) if (taxa and estado["ativo"]) else None,
        "decorrido_s": round(
            (time.monotonic() - estado["t_inicio"]) if estado["ativo"] else estado["duracao_s"], 1),
    }


def job_do_arquivo(nome):
    for job in jobs.values():
        if job["arquivo"] == nome:
            return job
    return None


def enviar_com_progresso(caminho, nome):
    info = os.stat(caminho)
    tamanho = info.st_size
    etag = '"%x-%x"' % (tamanho, int(info.st_mtime))
    ultima_modificacao = formatdate(info.st_mtime, usegmt=True)

    inicio, fim, codigo = 0, max(0, tamanho - 1), 200

    faixa = request.headers.get("Range")
    if_range = request.headers.get("If-Range")
    if faixa and if_range and if_range not in (etag, ultima_modificacao):
        faixa = None  # arquivo mudou: envia inteiro

    if faixa and tamanho > 0:
        achado = re.match(r"bytes=(\d*)-(\d*)$", faixa.strip())
        if achado and (achado.group(1) or achado.group(2)):
            if achado.group(1):
                inicio = int(achado.group(1))
                if achado.group(2):
                    fim = min(int(achado.group(2)), tamanho - 1)
            else:
                inicio = max(0, tamanho - int(achado.group(2)))
            if inicio > fim or inicio >= tamanho:
                return Response(status=416, headers={"Content-Range": f"bytes */{tamanho}"})
            codigo = 206

    comprimento = (fim - inicio + 1) if tamanho > 0 else 0

    estado = {
        "ativo": True, "concluido": False, "tamanho": tamanho, "inicio_byte": inicio,
        "enviados": 0, "taxa_bps": None, "t_inicio": time.monotonic(), "duracao_s": 0.0,
    }
    downloads[nome] = estado
    job = job_do_arquivo(nome)
    if job and job["id"] in coletores:
        coletores[job["id"]].fase = "download"

    if inicio > 0:
        log.info("Download RETOMADO: %s a partir do byte %d", nome, inicio)
    else:
        log.info("Download iniciado: %s (%d bytes)", nome, tamanho)

    def gerar():
        amostras = deque()
        restante = comprimento
        try:
            with open(caminho, "rb") as arquivo:
                arquivo.seek(inicio)
                while restante > 0:
                    bloco = arquivo.read(min(TAMANHO_BLOCO, restante))
                    if not bloco:
                        break
                    yield bloco
                    # só chega aqui depois que o bloco foi entregue à rede
                    restante -= len(bloco)
                    estado["enviados"] += len(bloco)

                    agora = time.monotonic()
                    amostras.append((agora, estado["enviados"]))
                    while amostras and agora - amostras[0][0] > JANELA_TAXA_S:
                        amostras.popleft()
                    if len(amostras) >= 2 and amostras[-1][0] - amostras[0][0] > 0.5:
                        estado["taxa_bps"] = ((amostras[-1][1] - amostras[0][1])
                                              / (amostras[-1][0] - amostras[0][0]))
        finally:
            duracao = time.monotonic() - estado["t_inicio"]
            completo = restante == 0
            estado["ativo"] = False
            estado["concluido"] = completo
            estado["duracao_s"] = duracao
            taxa_media = (estado["enviados"] * 8 / duracao / 1e6) if duracao > 0 else 0.0

            log.info("Download %s: %s, %d bytes em %.1f s (%.3f Mbps)",
                     "COMPLETO" if completo else "INTERROMPIDO", nome, estado["enviados"], duracao, taxa_media)
            j = job_do_arquivo(nome) or {}
            registrar(
                evento="download", job_id=j.get("id", ""), lote=j.get("lote", ""),
                condicao=j.get("condicao", ""), repeticao=j.get("repeticao", ""),
                missao=j.get("missao", ""), arquivo=nome,
                status="completo" if completo else "interrompido",
                tamanho_bytes=tamanho, inicio_byte=inicio, bytes_transferidos=estado["enviados"],
                duracao_s=round(duracao, 2), taxa_media_Mbps=round(taxa_media, 4),
                coletor_csv=j.get("coletor_csv", ""),
            )
            if completo and j:
                parar_coletor(j["id"], "download completo")

    cabecalhos = {
        "Content-Length": str(comprimento),
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": ultima_modificacao,
        "Content-Disposition": f'attachment; filename="{nome}"',
        "Cache-Control": "no-store",
    }
    if codigo == 206:
        cabecalhos["Content-Range"] = f"bytes {inicio}-{fim}/{tamanho}"

    return Response(gerar(), status=codigo, headers=cabecalhos,
                    mimetype="application/zip", direct_passthrough=True)


# ============================================================
# ROTAS: NAVEGAÇÃO
# ============================================================

@app.route("/api/progresso")
def rota_progresso():
    return jsonify(status_progresso)


@app.route("/")
def index():
    caminho_atual = request.args.get("path") or PASTA_REMOTA
    if not dentro_da_base(caminho_atual):
        return redirect(url_for("index"))

    itens = listar_diretorio_nativo(caminho_atual)
    if itens is None:
        return "Caminho não encontrado no robô.", 404

    parent_dir = os.path.dirname(caminho_atual.rstrip("/"))
    if not dentro_da_base(parent_dir):
        parent_dir = PASTA_REMOTA

    for item in itens:
        caminho = item["caminho_absoluto"]
        item["eh_missao"] = item["is_dir"] and os.path.isfile(os.path.join(caminho, "report.xml"))
        item["n_fotos"] = contar_fotos(caminho) if item["eh_missao"] else None
        item["tamanho_txt"] = "" if item["is_dir"] else tamanho_legivel(os.path.getsize(caminho))

    base = os.path.realpath(PASTA_REMOTA)
    relativo = os.path.relpath(os.path.realpath(caminho_atual), base)
    migalhas = [(os.path.basename(base.rstrip("/")) or base, PASTA_REMOTA)]
    acumulado = PASTA_REMOTA
    if relativo != ".":
        for parte in relativo.split(os.sep):
            acumulado = os.path.join(acumulado, parte)
            migalhas.append((parte, acumulado))

    return render_template("index.html", itens=itens, caminho_atual=caminho_atual,
                           parent_dir=parent_dir, pasta_base=PASTA_REMOTA,
                           migalhas=migalhas, roi=info_roi())


@app.route("/baixar_arquivo")
def baixar_arquivo():
    caminho = request.args.get("path")
    if not caminho or not dentro_da_base(caminho):
        abort(403)
    if os.path.isfile(caminho):
        return send_file(caminho, as_attachment=True)
    abort(404)


# ============================================================
# ROTAS: GERAR RELATÓRIO / FILA
# ============================================================

@app.route("/traduzir_missao")
def traduzir_missao():
    """Botão 'Gerar relatório': um job com a condição padrão (ou ?condicao=...)."""
    caminho_missao = request.args.get("path")
    if not caminho_missao:
        abort(400)
    if not dentro_da_base(caminho_missao):
        abort(403)
    try:
        ids = enfileirar(caminho_missao, [request.args.get("condicao") or CONFIG_EXPERIMENTO],
                         coletor=request.args.get("coletor") == "1", ping_host=ip_cliente())
    except ValueError as erro:
        return str(erro), 400
    return redirect(url_for("pagina_job", job_id=ids[0]))


@app.route("/api/fila", methods=["GET", "POST"])
def api_fila():
    """
    POST {"missao": "/home/.../missao", "condicoes": ["original", "roi:1200/70"],
          "repeticoes": 3, "embaralhar": true, "coletor": true, "rotulo": "teste A"}
    GET  -> estado de todos os jobs
    """
    if request.method == "GET":
        return jsonify({"ativo": job_ativo["id"], "fila": list(fila),
                        "jobs": [job_publico(j) for j in sorted(jobs.values(), key=lambda j: j["t_pedido"])]})
    dados = request.get_json(silent=True) or {}
    missao = dados.get("missao", "")
    if not missao or not dentro_da_base(missao) or not os.path.isfile(os.path.join(missao, "report.xml")):
        return jsonify({"erro": f"missão inválida (precisa estar em {PASTA_REMOTA} e ter report.xml)"}), 400
    try:
        ids = enfileirar(missao, dados.get("condicoes") or [CONFIG_EXPERIMENTO],
                         repeticoes=int(dados.get("repeticoes", 1)), embaralhar=bool(dados.get("embaralhar")),
                         coletor=bool(dados.get("coletor")), ping_host=dados.get("ping_host") or ip_cliente(),
                         rotulo=str(dados.get("rotulo", ""))[:80])
    except ValueError as erro:
        return jsonify({"erro": str(erro)}), 400
    return jsonify({"jobs": ids, "lote": jobs[ids[0]]["lote"]})


@app.route("/fila", methods=["GET", "POST"])
def pagina_fila():
    """Página da fila; o formulário enfileira uma missão com várias condições."""
    if request.method == "POST":
        missao = request.form.get("missao", "")
        if not dentro_da_base(missao):
            abort(403)
        condicoes = request.form.getlist("condicao") + [
            c for c in re.split(r"[,\s]+", request.form.get("extras", "")) if c]
        try:
            enfileirar(missao, condicoes, repeticoes=int(request.form.get("repeticoes", 1) or 1),
                       embaralhar=request.form.get("embaralhar") == "1",
                       coletor=request.form.get("coletor") == "1", ping_host=ip_cliente())
        except ValueError as erro:
            return str(erro), 400
        return redirect(url_for("pagina_fila"))
    missao = request.args.get("missao", "")
    if missao and not dentro_da_base(missao):
        missao = ""
    lista = [job_publico(j) for j in sorted(jobs.values(), key=lambda j: j["t_pedido"], reverse=True)]
    return render_template("fila.html", aba="fila", jobs=lista, missao=missao,
                           missao_nome=os.path.basename(missao.rstrip("/")),
                           condicoes=configuracoes.CONDICOES_ARTIGO, padrao=CONFIG_EXPERIMENTO, roi=info_roi())


@app.route("/api/job/<job_id>/cancelar", methods=["POST"])
def cancelar_job(job_id):
    with trava:
        if job_id in fila:
            fila.remove(job_id)
            jobs[job_id]["etapa"] = "cancelado"
            return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "só dá para cancelar o que ainda está na fila"}), 409


@app.route("/fila/limpar", methods=["POST"])
def limpar_fila():
    with trava:
        for job_id in list(fila):
            jobs[job_id]["etapa"] = "cancelado"
        fila.clear()
    return redirect(url_for("pagina_fila"))


def job_publico(job):
    """O que a página e o experimento_pc.py precisam saber de um job."""
    r = {
        "id": job["id"], "lote": job["lote"], "rotulo": job["rotulo"],
        "missao": job["missao"], "missao_nome": os.path.basename(job["missao"].rstrip("/")),
        "condicao": job["condicao"], "repeticao": job["repeticao"],
        "etapa": job["etapa"], "erro": job["erro"], "arquivo": job["arquivo"],
        "pedido_em": job["pedido_em"], "config": CONFIG_EXPERIMENTO, "link_mbps": LINK_MBPS,
        "coletor_csv": job["coletor_csv"], "coletor_ativo": job["id"] in coletores,
        "coletor_resumo": job.get("coletor_resumo"),
    }
    if job["etapa"] == "na_fila":
        r["posicao"] = (list(fila).index(job["id"]) + 1) if job["id"] in fila else None
        r["a_frente"] = (r["posicao"] or 1) - 1 + (1 if job_ativo["id"] else 0)
    if job["espera_fila_s"] is not None:
        r["espera_fila_s"] = round(job["espera_fila_s"], 2)
    if job["etapa"] == "processando":
        decorrido = time.monotonic() - job["t_inicio"]
        pct, mensagem = ler_progresso_processador()
        r.update(decorrido_s=round(decorrido, 1), pct=pct, mensagem=mensagem)
        if pct and 0 < pct < 100:
            r["eta_processamento_s"] = round(decorrido * (100 - pct) / pct, 1)
    elif job["tempo_processamento"] is not None:
        r["tempo_processamento_s"] = round(job["tempo_processamento"], 2)
        r.update(job.get("etapas") or {})
    if job["tamanho"]:
        r["tamanho_bytes"] = job["tamanho"]
        r["estimativa_download_s"] = round(job["tamanho"] * 8 / (LINK_MBPS * 1e6), 1)
        r["url_download"] = url_for("baixar_relatorio", nome=job["arquivo"])
    estado = downloads.get(job["arquivo"])
    if estado:
        r["download"] = estado_download_publico(estado)
    return r


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify({"etapa": "desconhecido"}), 404
    return jsonify(job_publico(job))


@app.route("/relatorios/<nome>")
def baixar_relatorio(nome):
    if nome != os.path.basename(nome) or not nome.endswith(".zip"):
        abort(404)
    caminho = os.path.join(PASTA_RELATORIOS, nome)
    if not os.path.isfile(caminho):
        abort(404)
    return enviar_com_progresso(caminho, nome)


@app.route("/relatorios")
def lista_relatorios():
    arquivos = []
    nomes = [n for n in os.listdir(PASTA_RELATORIOS) if n.endswith(".zip")]
    nomes.sort(key=lambda n: os.path.getmtime(os.path.join(PASTA_RELATORIOS, n)), reverse=True)
    for nome in nomes:
        tamanho = os.path.getsize(os.path.join(PASTA_RELATORIOS, nome))
        missao, config, data = ler_nome_relatorio(nome)
        segundos = tamanho * 8 / (LINK_MBPS * 1e6)
        arquivos.append({
            "nome": nome, "missao": missao, "config": config, "roi": "roi" in config, "data": data,
            "mb": round(tamanho / 1048576, 2),
            "estimativa": f"{segundos:.0f} s" if segundos < 90 else f"{segundos / 60:.1f} min",
        })
    return render_template("relatorios.html", arquivos=arquivos, link_mbps=LINK_MBPS)


@app.route("/job/<job_id>")
def pagina_job(job_id):
    if job_id not in jobs:
        return render_template("job_perdido.html")
    job = jobs[job_id]
    return render_template("job.html", job_id=job_id, missao=os.path.basename(job["missao"].rstrip("/")),
                           condicao=job["condicao"])


# ============================================================
# ROTAS: COLETOR
# ============================================================

@app.route("/api/coletor/iniciar", methods=["POST"])
def coletor_iniciar():
    """Coletor manual (ex.: 30 s de tráfego de fundo com o robô deitado)."""
    if "manual" in coletores:
        return jsonify({"erro": "já há um coletor manual rodando"}), 409
    rotulo = nome_seguro(request.args.get("rotulo", "manual"))
    nome = f"manual_{datetime.now():%Y%m%d_%H%M%S}_{rotulo}.csv"
    c = Coletor(os.path.join(PASTA_COLETOR, nome),
                ping_host=host_valido(request.args.get("ping") or ip_cliente()))
    c.fase = rotulo
    c.iniciar()
    coletores["manual"] = c
    return jsonify({"arquivo": nome, "interface": c.interface})


@app.route("/api/coletor/parar", methods=["POST"])
def coletor_parar():
    c = coletores.get("manual")
    if c is None:
        return jsonify({"erro": "nenhum coletor manual rodando"}), 409
    nome = os.path.basename(c.destino)
    return jsonify({"arquivo": nome, "resumo": parar_coletor("manual", "pedido manual")})


@app.route("/coletor/<nome>")
def baixar_coletor(nome):
    """CSV/TXT/JSON do coletor (o experimento_pc.py baixa automaticamente)."""
    if nome != os.path.basename(nome) or not nome.endswith((".csv", ".txt", ".json")):
        abort(404)
    return send_from_directory(PASTA_COLETOR, nome, as_attachment=True, max_age=0)


@app.route("/api/coletor/arquivos/<job_id>")
def arquivos_coletor(job_id):
    job = jobs.get(job_id)
    if not job or not job["coletor_csv"]:
        return jsonify({"arquivos": []})
    base = os.path.splitext(job["coletor_csv"])[0]
    nomes = [n for n in (job["coletor_csv"], base + "_tc_antes.txt", base + "_tc_depois.txt", base + "_resumo.json")
             if os.path.exists(os.path.join(PASTA_COLETOR, n))]
    return jsonify({"arquivos": nomes, "ativo": job_id in coletores})


# ============================================================
# INICIALIZAÇÃO
# ============================================================

threading.Thread(target=trabalhador, daemon=True, name="fila").start()

if __name__ == "__main__":
    print()
    print("==============================================")
    print(" SERVIDOR ANYMAL")
    print("==============================================")
    print("Condição padrão:", CONFIG_EXPERIMENTO)
    roi = info_roi()
    print("Compressão por ROI:", f"disponível ({roi['imagens_anotadas']} fotos anotadas)" if roi["ativa"]
          else "DESLIGADA: " + roi["motivo"])
    print("Pasta das missões:", PASTA_REMOTA)
    print("Porta:", PORTA, f"(abra http://<ip-do-robo>:{PORTA})")
    print("Relatórios salvos em:", PASTA_RELATORIOS)
    print("Registro das medições:", ARQUIVO_REGISTRO)
    print("Coletor:", PASTA_COLETOR)
    print("==============================================")
    print()

    log.info("Servidor iniciado (%s)", CONFIG_EXPERIMENTO)
    app.run(host="0.0.0.0", port=PORTA, debug=False, threaded=True)
