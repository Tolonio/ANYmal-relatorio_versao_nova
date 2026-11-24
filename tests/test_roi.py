"""
Testes: python3 -m pytest -q tests/

Rodam sem WeasyPrint e sem internet (o WeasyPrint é substituído por um stub que
só grava os bytes do HTML como "PDF"; o restante do pipeline é o código real).
"""
import csv
import io
import json
import math
import os
import random
import sys
import time
import types
import zipfile

import numpy as np
import pytest
from PIL import Image

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
sys.path.insert(0, RAIZ)
sys.path.insert(0, AQUI)

import compressor_roi as cr                      # noqa: E402
from gerar_dados_sinteticos import foto          # noqa: E402

W, H = 1600, 900   # fotos menores que as reais para os testes serem rápidos


# ------------------------------------------------------------
# dados de teste
# ------------------------------------------------------------

def coco_roboflow(nomes_regioes, largura_exp=640, altura_exp=640):
    """Monta um COCO como o Roboflow exporta (nomes .rf., imagem 640×640 'Stretch')."""
    coco = {"images": [], "annotations": [], "categories": [{"id": 0, "name": "pecas"}, {"id": 1, "name": "peca"}]}
    aid = 1
    for i, (nome, regs, (w, h)) in enumerate(nomes_regioes):
        base, ext = os.path.splitext(nome)
        coco["images"].append({"id": i, "file_name": f"{base}_{ext[1:]}.rf.{i:06x}ff.jpg",
                               "width": largura_exp, "height": altura_exp})
        for pol in regs:
            seg = []
            for x, y in pol:
                seg += [x * largura_exp / w, y * altura_exp / h]
            coco["annotations"].append({"id": aid, "image_id": i, "category_id": 1,
                                        "segmentation": [seg], "bbox": [0, 0, 1, 1]})
            aid += 1
    return coco


@pytest.fixture(scope="session")
def missao(tmp_path_factory):
    """Missão sintética: report.xml + stylesheet.xsl + resources/*.jpg + export do Roboflow."""
    raiz = tmp_path_factory.mktemp("reports")
    pasta = raiz / "missao_teste"
    (pasta / "resources").mkdir(parents=True)
    entradas, anot = [], []
    for i in range(3):
        img, regs = foto(i, W, H)
        nome = f"2026-05-20-15-1{i}-07-000{i}.jpg"
        img.save(pasta / "resources" / nome, quality=92)
        anot.append((nome, regs, (W, H)))
        entradas.append(f"""<entry><level>INFO</level><time_of_day>18:1{i}:07</time_of_day>
            <message>Successfully took image of Visor {i}</message><file>./resources/{nome}</file></entry>""")
    # foto sem anotação (como as térmicas)
    termica, _ = foto(9, 579, 384)
    termica.save(pasta / "resources" / "termica.jpg", quality=92)
    entradas.append("""<entry><level>INFO</level><time_of_day>18:20:00</time_of_day>
        <message>Successfully analyzed temperature of Motor</message><file>resources/termica.jpg</file></entry>""")
    entradas.append("<entry><level>WARN</level><message>ignorar</message></entry>")

    (pasta / "report.xml").write_text(f"""<?xml version="1.0"?>
<report><name>Inspection route</name><location>Plant</location><date>2026/05/20</date>
{''.join(entradas)}</report>""", encoding="utf-8")
    (pasta / "stylesheet.xsl").write_text("""<?xml version="1.0"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:template match="/"><html><head><title>Mission Report</title></head><body><h1>MISSION REPORT</h1>
<table><tr><th>Time</th><th>Message</th><th>File</th></tr>
<xsl:for-each select="report/entry"><tr><td><xsl:value-of select="time_of_day"/></td>
<td><xsl:value-of select="message"/></td><td><img src="{file}"/></td></tr></xsl:for-each>
</table></body></html></xsl:template></xsl:stylesheet>""", encoding="utf-8")

    export = raiz.parent / "anotacoes_roi" / "train"
    export.mkdir(parents=True)
    (export / "_annotations.coco.json").write_text(json.dumps(coco_roboflow(anot)))
    return {"raiz": str(raiz), "pasta": str(pasta), "anotacoes": str(export.parent), "regioes": anot}


# ------------------------------------------------------------
# compressor_roi
# ------------------------------------------------------------

def test_chave_roboflow_casa_com_original():
    assert cr.chave_imagem("2026-05-20-15-10-07-0001_jpg.rf.3f9c2ab1.jpg") == \
        cr.chave_imagem("/x/resources/2026-05-20-15-10-07-0001.jpg")
    assert cr.chave_imagem("foto.v2_png.rf.ab12.jpg") == cr.chave_imagem("foto.v2.png")
    assert cr.chave_imagem("Visor A.JPG") == cr.chave_imagem("visor_a.jpg")


def test_circulo_minimo_contem_todos_e_e_minimo():
    rng = random.Random(1)
    for _ in range(200):
        pts = [(rng.uniform(0, 100), rng.uniform(0, 50)) for _ in range(rng.randint(1, 15))]
        cx, cy, r = cr.circulo_minimo(pts)
        assert all(math.hypot(x - cx, y - cy) <= r + 1e-6 for x, y in pts)
        # nunca menor que metade do maior par (limite inferior do círculo mínimo)
        diam = max(math.hypot(a[0] - b[0], a[1] - b[1]) for a in pts for b in pts)
        assert r >= diam / 2 - 1e-6
        # e nunca maior que o círculo de raio diam/√3 (limite superior, Jung)
        assert r <= diam / math.sqrt(3) + 1e-6


def test_coco_stretch_volta_para_coordenadas_da_foto(missao):
    anot = cr.AnotacoesROI(missao["anotacoes"])
    assert len(anot) == 3
    nome, regs_px, _ = missao["regioes"][0]
    lidas = anot.regioes_para(os.path.join(missao["pasta"], "resources", nome))
    x, y = lidas[0]["poligono"][0]
    assert abs(x * W - regs_px[0][0][0]) < 1 and abs(y * H - regs_px[0][0][1]) < 1
    assert anot.regioes_para("termica.jpg") is None


def test_yolo_segmentacao_e_caixa(tmp_path):
    (tmp_path / "a_jpg.rf.123.txt").write_text("0 0.1 0.1 0.3 0.1 0.3 0.4\n")
    (tmp_path / "b.txt").write_text("2 0.5 0.5 0.2 0.2\n")
    anot = cr.AnotacoesROI(str(tmp_path))
    assert len(anot.regioes_para("a.jpg")[0]["poligono"]) == 3
    assert anot.regioes_para("b.png")[0]["poligono"][0] == pytest.approx((0.4, 0.4))


def test_augmentation_duplicada_gera_aviso(tmp_path):
    for i, x in enumerate((0.1, 0.8)):   # mesma foto, polígono espelhado
        coco = {"images": [{"id": 0, "file_name": f"f_jpg.rf.{i}a.jpg", "width": 10, "height": 10}],
                "annotations": [{"id": 1, "image_id": 0, "category_id": 0,
                                 "segmentation": [[x * 10, 1, x * 10 + 1, 1, x * 10, 2]]}],
                "categories": [{"id": 0, "name": "p"}]}
        (tmp_path / f"s{i}.json").write_text(json.dumps(coco))
    anot = cr.AnotacoesROI(str(tmp_path))
    assert len(anot) == 1 and any("augmentation" in a for a in anot.avisos)


def test_mascara_protege_circulo_inteiro():
    regs = [{"classe": "p", "poligono": [(0.5, 0.4), (0.52, 0.4), (0.52, 0.6), (0.5, 0.6)]}]
    m = np.asarray(cr.mascara_roi(regs, W, H))
    cx, cy, r = cr.circulos_roi(regs, W, H)[0]
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.hypot(xx - cx, yy - cy)
    assert m[d <= r].min() >= 250            # tudo dentro do círculo fica nítido
    assert m[d >= r + 0.03 * H].max() == 0   # longe dele é fundo


def test_roi_preserva_peca_e_reduz_tamanho(missao):
    nome, regs_px, _ = missao["regioes"][0]
    caminho = os.path.join(missao["pasta"], "resources", nome)
    anot = cr.AnotacoesROI(missao["anotacoes"])
    regs = anot.regioes_para(caminho)
    img = cr.abrir_imagem(caminho)

    roi, info = cr.comprimir_roi(img, regs, lado=1600, qualidade=80, lado_fundo=400)
    alta = cr.comprimir_uniforme(img, 1600, 85)
    assert len(roi) < 0.6 * len(alta)
    assert info["fracao_area_roi"] < 0.2

    peca = np.asarray(cr.mascara_poligonos(regs, W, H)) > 127
    o = np.asarray(img.convert("L"), float)
    erro = lambda b: ((np.asarray(Image.open(io.BytesIO(b)).convert("L"), float) - o) ** 2)
    mse_roi_peca, mse_alta_peca = erro(roi)[peca].mean(), erro(alta)[peca].mean()
    assert mse_roi_peca < 1.6 * mse_alta_peca              # peça ~ igual ao 1600/85
    assert erro(roi)[~peca].mean() > 2 * mse_roi_peca       # fundo foi degradado


def test_prequantizacao_reduz_fundo(missao):
    nome, _, _ = missao["regioes"][1]
    caminho = os.path.join(missao["pasta"], "resources", nome)
    regs = cr.AnotacoesROI(missao["anotacoes"]).regioes_para(caminho)
    img = cr.abrir_imagem(caminho)
    com, _ = cr.comprimir_roi(img, regs, qualidade_fundo=30)
    sem, _ = cr.comprimir_roi(img, regs, qualidade_fundo=0)
    assert len(com) < len(sem)


def test_cli_verificar_e_comprimir(missao, tmp_path, capsys):
    pasta_fotos = os.path.join(missao["pasta"], "resources")
    assert cr.main(["verificar", "--anotacoes", missao["anotacoes"], "--imagens", pasta_fotos,
                    "--saida", str(tmp_path / "v")]) == 0
    assert len(os.listdir(tmp_path / "v")) == 3
    assert cr.main(["comprimir", "--anotacoes", missao["anotacoes"], "--imagens", pasta_fotos,
                    "--saida", str(tmp_path / "c")]) == 0
    saida = capsys.readouterr().out
    assert "ROI: 3  uniforme: 1" in saida


# ------------------------------------------------------------
# processador + servidor (pipeline real, WeasyPrint substituído)
# ------------------------------------------------------------

@pytest.fixture(scope="session")
def modulos(missao, tmp_path_factory):
    casa = tmp_path_factory.mktemp("home")
    os.environ["HOME"] = str(casa)
    os.environ["RELATORIO_ROI_ANOTACOES"] = missao["anotacoes"]
    os.environ["RELATORIO_LADO"] = "800"
    os.environ["RELATORIO_QUALIDADE"] = "45"

    class HTML:   # stub do WeasyPrint: busca cada <img> pelo url_fetcher, como o real faria
        def __init__(self, string, base_url, url_fetcher=None):
            self.html, self.base, self.fetch = string, base_url, url_fetcher

        def write_pdf(self, destino, stylesheets=None):
            import re
            for src in re.findall(r'src="([^"]+)"', self.html):
                url = self.base + src[2:] if src.startswith("./") else self.base + src
                if self.fetch is None:        # como o WeasyPrint sem fetcher: lê o arquivo original
                    from urllib.parse import unquote, urlparse
                    with open(unquote(urlparse(url).path), "rb") as f:
                        destino.write(f.read())
                    continue
                r = self.fetch(url)
                destino.write(r["string"] if isinstance(r, dict) else b"")
            destino.write(b"%PDF-stub " + self.html.encode())

    fake = types.ModuleType("weasyprint")
    fake.HTML, fake.CSS = HTML, lambda string: None
    fake.default_url_fetcher = lambda url, *a, **k: {"string": b""}
    sys.modules["weasyprint"] = fake
    sys.modules["weasyprint.urls"] = types.ModuleType("weasyprint.urls")   # sem URLFetcher: API antiga

    import processador_relatorio as pr
    pr.PASTA_REMOTA = missao["raiz"]
    import servidor_web as sw
    sw.PASTA_REMOTA = missao["raiz"]
    return pr, sw


def test_processador_usa_roi_so_nas_fotos_anotadas(modulos, missao):
    pr, _ = modulos
    assert pr.ROI_ATIVA and "_roi1600q80f400" in pr.CONFIG_NOME
    pacote = pr.gerar_relatorio_nativo(missao["pasta"])
    z = zipfile.ZipFile(pacote)
    nomes = z.namelist()
    assert "Relatorio_Traduzido_PT.pdf" in nomes
    fotos = [n for n in nomes if n.endswith(".jpg")]
    assert len(fotos) == 4 and any(n.startswith("imagens/Visor_0_") for n in fotos)
    tamanhos = {n: Image.open(io.BytesIO(z.read(n))).size for n in fotos}
    assert sorted(tamanhos.values())[-1] == (1600, 900)      # ROI: 1600 de lado
    assert any(max(s) <= 800 for s in tamanhos.values())     # térmica: config uniforme
    e = pr.ultima_execucao
    assert e["imagens"] == 4 and e["imagens_roi"] == 3
    assert all(e[k] >= 0 for k in ("t_traducao_s", "t_compressao_s", "t_pdf_s", "t_zip_s"))


def esperar(cli, job_id, limite=60):
    for _ in range(limite * 10):
        j = cli.get(f"/api/job/{job_id}").get_json()
        if j["etapa"] in ("pronto", "erro", "cancelado"):
            return j
        time.sleep(0.1)
    raise AssertionError(j)


def test_servidor_paginas_e_download_com_retomada(modulos, missao):
    _, sw = modulos
    cli = sw.app.test_client()

    r = cli.get("/")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Gerar relatório" in html and "logo_itv.png" in html
    assert "Compressão por ROI disponível" in html and "missao_teste" in html and "Experimento" in html
    assert cli.get("/static/anymal.png").status_code == 200

    r = cli.get("/traduzir_missao", query_string={"path": missao["pasta"]})
    assert r.status_code == 302
    job_id = r.headers["Location"].rstrip("/").split("/")[-1]
    assert "Gerando relatório" in cli.get(f"/job/{job_id}").get_data(as_text=True)

    j = esperar(cli, job_id)
    assert j["etapa"] == "pronto", j
    assert j["imagens_roi"] == 3 and "t_compressao_s" in j

    url = j["url_download"]
    inteiro = cli.get(url).data
    assert len(inteiro) == j["tamanho_bytes"]
    parte = cli.get(url, headers={"Range": "bytes=100-"})
    assert parte.status_code == 206 and parte.data == inteiro[100:]

    lista = cli.get("/relatorios").get_data(as_text=True)
    assert "missao_teste" in lista and "ROI" in lista

    with open(sw.ARQUIVO_REGISTRO, encoding="utf-8") as f:
        cab = f.readline()
        linhas = f.read()
    assert "t_compressao_s" in cab and ",processamento," in linhas

    assert cli.get("/", query_string={"path": "/etc"}).status_code == 302   # fora da base
    assert cli.get("/job/naoexiste").status_code == 200


def test_ler_nome_relatorio(modulos):
    _, sw = modulos
    m, c, d = sw.ler_nome_relatorio("Relatorio_PDF_rota_A_PT_800x800_q45_roi1600q80f400qf30_20260520_151007.zip")
    assert (m, c, d) == ("rota_A", "800x800_q45_roi1600q80f400qf30", "20/05/2026 15:10")


# ------------------------------------------------------------
# condições, fila, coletor e experimento_pc
# ------------------------------------------------------------

def test_condicoes():
    import configuracoes as cf
    assert cf.ler_condicao("original")["modo"] == "original"
    u = cf.ler_condicao("1200/70")
    assert (u["modo"], u["lado"], u["qualidade"], u["condicao"]) == ("uniforme", 1200, 70, "1200/70")
    r = cf.ler_condicao("ROI:1200/70")
    assert (r["roi_lado"], r["roi_qualidade"], r["roi_fundo"], r["roi_qualidade_fundo"]) == (1200, 70, 300, 30)
    assert (r["lado"], r["qualidade"]) == (1200, 70)        # sem marcação: igual à uniforme
    assert r["condicao"] == "roi:1200/70" and cf.nome_arquivo(r) == "roi1200q70f300qf30"
    e = cf.ler_condicao("roi:1600/80/400/0+pol")
    assert e["condicao"] == "roi:1600/80/400/0+pol" and e["roi_modo"] == "poligono"
    for ruim in ("", "1200", "roi:", "abc", "1200/70/3", "5000000/70", "1200/0", "--x"):
        with pytest.raises(ValueError):
            cf.ler_condicao(ruim)
    for c in cf.CONDICOES_ARTIGO:
        assert cf.ler_condicao(c)["condicao"] == c


def test_modo_original_nao_recomprime(modulos, missao):
    pr, _ = modulos
    z = zipfile.ZipFile(pr.gerar_relatorio_nativo(missao["pasta"], "original"))
    fotos = [n for n in z.namelist() if n.lower().endswith(".jpg")]
    originais = {os.path.getsize(os.path.join(missao["pasta"], "resources", f))
                 for f in os.listdir(os.path.join(missao["pasta"], "resources"))}
    assert len(fotos) == 4 and {len(z.read(n)) for n in fotos} == originais
    assert pr.ultima_execucao["imagens_roi"] == 0 and pr.ultima_execucao["condicao"] == "original"


def test_roi_e_uniforme_no_mesmo_nivel(modulos, missao):
    """roi:L/Q x L/Q: a térmica sai idêntica, as fotos marcadas ficam menores."""
    pr, _ = modulos
    tam = {}
    for c in ("800/45", "roi:800/45"):
        z = zipfile.ZipFile(pr.gerar_relatorio_nativo(missao["pasta"], c))
        tam[c] = {n: len(z.read(n)) for n in z.namelist() if n.endswith(".jpg")}
    termicas = [n for n in tam["800/45"] if "Termica" in n]
    assert termicas and all(tam["800/45"][n] == tam["roi:800/45"][n] for n in termicas)
    assert sum(tam["roi:800/45"].values()) < sum(tam["800/45"].values())


def test_fila_varias_condicoes_com_coletor(modulos, missao):
    _, sw = modulos
    cli = sw.app.test_client()
    r = cli.post("/api/fila", json={"missao": missao["pasta"], "condicoes": ["600/35", "roi:600/35"],
                                    "repeticoes": 2, "embaralhar": True, "coletor": True})
    assert r.status_code == 200, r.get_json()
    ids = r.get_json()["jobs"]
    assert len(ids) == 4
    reps = [cli.get(f"/api/job/{i}").get_json()["repeticao"] for i in ids]
    assert reps == [1, 1, 2, 2]                               # embaralha só dentro da repetição
    finais = [esperar(cli, i) for i in ids]
    assert all(j["etapa"] == "pronto" for j in finais)
    assert {j["condicao"] for j in finais} == {"600/35", "roi:600/35"}

    j = finais[0]
    assert j["coletor_ativo"] and j["coletor_csv"]
    cli.get(j["url_download"]).data                         # baixar encerra o coletor do job
    for _ in range(50):
        if not cli.get(f"/api/coletor/arquivos/{j['id']}").get_json()["ativo"]:
            break
        time.sleep(0.1)
    arquivos = cli.get(f"/api/coletor/arquivos/{j['id']}").get_json()["arquivos"]
    assert any(a.endswith("_resumo.json") for a in arquivos) and any(a.endswith("_tc_antes.txt") for a in arquivos)
    csv_txt = cli.get("/coletor/" + j["coletor_csv"]).get_data(as_text=True)
    assert csv_txt.startswith("timestamp,timestamp_epoch,fase,cpu_percent")

    assert "Fila de relatórios" in cli.get("/fila").get_data(as_text=True)
    assert "Enfileirar experimento" in cli.get("/fila", query_string={"missao": missao["pasta"]}).get_data(as_text=True)
    assert cli.post("/api/fila", json={"missao": missao["pasta"], "condicoes": ["xyz"]}).status_code == 400
    assert cli.post("/api/fila", json={"missao": "/etc", "condicoes": ["600/35"]}).status_code == 400
    assert sw.host_valido("-f") is None and sw.host_valido("192.168.0.10") == "192.168.0.10"


def test_cancelar_na_fila(modulos, missao):
    _, sw = modulos
    cli = sw.app.test_client()
    ids = cli.post("/api/fila", json={"missao": missao["pasta"], "condicoes": ["600/35"], "repeticoes": 3}).get_json()["jobs"]
    ultimo = ids[-1]
    r = cli.post(f"/api/job/{ultimo}/cancelar")
    if r.status_code == 200:            # ainda estava na fila
        assert esperar(cli, ultimo)["etapa"] == "cancelado"
    for i in ids[:-1]:
        assert esperar(cli, i)["etapa"] == "pronto"


def test_experimento_pc_ponta_a_ponta(modulos, missao, tmp_path):
    """Sobe o servidor de verdade numa porta local e roda o script do PC contra ele."""
    import threading
    from werkzeug.serving import make_server
    _, sw = modulos
    srv = make_server("127.0.0.1", 0, sw.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import experimento_pc
        saida = tmp_path / "exp"
        cod = experimento_pc.main([
            "--robo", f"http://127.0.0.1:{srv.server_port}", "--missao", missao["pasta"],
            "--condicoes", "original", "roi:800/45", "--repeticoes", "2", "--semente", "1",
            "--saida", str(saida), "--pausa", "0", "--salvar-zips", "--intervalo", "0.2"])
        assert cod == 0
        linhas = list(csv.DictReader(open(saida / "medicoes.csv", encoding="utf-8")))
        assert len(linhas) == 4 and all(l["status"] == "ok" and l["zip_ok"] == "1" for l in linhas)
        assert all(float(l["t_transferencia_s"]) > 0 and float(l["t_processamento_s"]) > 0 for l in linhas)
        assert all(l["coletor_csv"] for l in linhas)
        resumo = list(csv.DictReader(open(saida / "resumo.csv", encoding="utf-8")))
        assert {r["condicao"] for r in resumo} == {"original", "roi:800/45"}
        orig = next(r for r in resumo if r["condicao"] == "original")
        roi = next(r for r in resumo if r["condicao"] == "roi:800/45")
        assert float(roi["tamanho_bytes_media"]) < float(orig["tamanho_bytes_media"])
        assert len(list((saida / "coletor").glob("*.csv"))) == 4
    finally:
        srv.shutdown()
