"""
Roda a matriz de experimentos A PARTIR DO PC (VS Code), contra o servidor do ANYmal.

Para cada (repetição × condição), em ordem aleatória dentro de cada repetição:
  1. pede o relatório ao robô (fila), com o coletor CSV ligado;
  2. espera o processamento (o robô informa o tempo de cada etapa);
  3. baixa o ZIP medindo o tempo até o 1º byte e o tempo de transferência;
  4. confere o ZIP, baixa os arquivos do coletor e só então passa para o próximo.

Assim nenhum download disputa o link com outro, e o processamento fica separado
da transferência (limitação apontada no artigo).

Só usa a biblioteca padrão do Python. Exemplo (terminal do VS Code):

  python experimento_pc.py --robo http://192.168.0.50:5051 ^
      --missao /home/integration/.ros/reports/2026-05-20 ^
      --condicoes original 600/35 1200/70 roi:600/35 roi:1200/70 roi:1600/85 ^
      --repeticoes 3 --saida experimento_2026-11-25

  (no Linux/macOS troque ^ por \\)

Saídas em --saida:
  medicoes.csv      uma linha por execução
  resumo.csv        média, desvio, mín. e máx. por condição
  coletor/          CSV 1 Hz do robô + tc -s qdisc antes/depois + resumo, por execução
  zips/             os relatórios (só com --salvar-zips)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime

CAMPOS = ["ordem", "repeticao", "condicao", "job_id", "status", "inicio", "arquivo",
          "tamanho_bytes", "imagens", "imagens_roi", "espera_fila_s",
          "t_processamento_s", "t_traducao_s", "t_compressao_s", "t_pdf_s", "t_zip_s",
          "t_primeiro_byte_s", "t_transferencia_s", "t_total_s", "taxa_Mbps",
          "zip_ok", "coletor_csv", "coletor_tx_Mbps", "coletor_perda_ping_pct",
          "coletor_descartes_qdisc", "erro"]


# ------------------------------------------------------------
# HTTP
# ------------------------------------------------------------

def pedir(url, dados=None, timeout=30):
    corpo = json.dumps(dados).encode() if dados is not None else None
    req = urllib.request.Request(url, data=corpo, method="POST" if corpo else "GET",
                                 headers={"Content-Type": "application/json"} if corpo else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def baixar(url, destino=None, timeout=60, bloco=64 * 1024):
    """Baixa medindo tempos. Retorna (bytes, t_primeiro_byte, t_total)."""
    t0 = time.perf_counter()
    total, t_pb = 0, None
    arq = open(destino, "wb") if destino else None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            esperado = int(r.headers.get("Content-Length") or 0)
            while True:
                pedaco = r.read(bloco)
                if t_pb is None:
                    t_pb = time.perf_counter() - t0
                if not pedaco:
                    break
                total += len(pedaco)
                if arq:
                    arq.write(pedaco)
    finally:
        if arq:
            arq.close()
    t_total = time.perf_counter() - t0
    if esperado and total != esperado:
        raise IOError(f"download incompleto: {total} de {esperado} bytes")
    return total, t_pb or t_total, t_total


# ------------------------------------------------------------
# UMA EXECUÇÃO
# ------------------------------------------------------------

def executar(args, ordem, rep, condicao, pasta_coletor, pasta_zips):
    linha = {"ordem": ordem, "repeticao": rep, "condicao": condicao,
             "inicio": datetime.now().isoformat(timespec="seconds"), "status": "erro"}
    t_inicio = time.perf_counter()
    try:
        r = pedir(args.robo + "/api/fila", {"missao": args.missao, "condicoes": [condicao], "repeticoes": 1,
                                            "coletor": not args.sem_coletor, "rotulo": args.rotulo,
                                            "ping_host": args.ping_host})
        job_id = r["jobs"][0]
        linha["job_id"] = job_id

        # espera o processamento
        limite = time.monotonic() + args.tempo_max_processamento
        ultimo = ""
        while True:
            j = pedir(f"{args.robo}/api/job/{job_id}")
            if j["etapa"] in ("pronto", "erro", "cancelado"):
                break
            msg = f"   {j['etapa']}: {j.get('mensagem') or ''} {j.get('pct') or ''}"
            if msg != ultimo:
                print(msg.ljust(70), end="\r")
                ultimo = msg
            if time.monotonic() > limite:
                raise TimeoutError("processamento passou do tempo máximo")
            time.sleep(args.intervalo)
        print(" " * 72, end="\r")
        if j["etapa"] != "pronto":
            raise RuntimeError(j.get("erro") or j["etapa"])

        for k in ("arquivo", "tamanho_bytes", "imagens", "imagens_roi", "espera_fila_s",
                  "t_traducao_s", "t_compressao_s", "t_pdf_s", "t_zip_s"):
            linha[k] = j.get(k, "")
        linha["t_processamento_s"] = j.get("tempo_processamento_s")

        if args.pausa_antes_download:
            time.sleep(args.pausa_antes_download)

        # baixa
        destino = os.path.join(pasta_zips, j["arquivo"]) if pasta_zips else None
        n, t_pb, t_dl = baixar(args.robo + j["url_download"], destino, timeout=args.timeout_download)
        linha["t_primeiro_byte_s"] = round(t_pb, 3)
        linha["t_transferencia_s"] = round(t_dl, 3)
        linha["taxa_Mbps"] = round(n * 8 / t_dl / 1e6, 4) if t_dl > 0 else ""
        linha["zip_ok"] = ""
        if destino:
            with zipfile.ZipFile(destino) as z:
                linha["zip_ok"] = int(z.testzip() is None)
        linha["status"] = "ok"

        # arquivos do coletor (o robô para o coletor quando o download termina)
        if not args.sem_coletor:
            for _ in range(15):
                a = pedir(f"{args.robo}/api/coletor/arquivos/{job_id}")
                if not a.get("ativo"):
                    break
                time.sleep(1)
            for nome in a.get("arquivos", []):
                baixar(f"{args.robo}/coletor/{nome}", os.path.join(pasta_coletor, nome))
                if nome.endswith("_resumo.json"):
                    with open(os.path.join(pasta_coletor, nome)) as f:
                        res = json.load(f)
                    linha["coletor_tx_Mbps"] = res.get("tx_Mbps_medio_download")
                    linha["coletor_perda_ping_pct"] = res.get("perda_ping_percent")
                    linha["coletor_descartes_qdisc"] = res.get("descartes_qdisc")
                elif nome.endswith(".csv"):
                    linha["coletor_csv"] = nome

    except (urllib.error.URLError, OSError, RuntimeError, TimeoutError, KeyError, zipfile.BadZipFile) as erro:
        linha["erro"] = str(erro)[:200]
    linha["t_total_s"] = round(time.perf_counter() - t_inicio, 3)
    return linha


# ------------------------------------------------------------
# RESUMO
# ------------------------------------------------------------

def resumir(linhas, condicoes):
    saida = []
    for c in condicoes:
        ok = [l for l in linhas if l["condicao"] == c and l["status"] == "ok"]
        r = {"condicao": c, "n_ok": len(ok), "n_erro": sum(1 for l in linhas if l["condicao"] == c) - len(ok)}
        for k in ("tamanho_bytes", "t_processamento_s", "t_transferencia_s", "t_total_s", "taxa_Mbps"):
            v = [float(l[k]) for l in ok if l.get(k) not in ("", None)]
            if v:
                r[k + "_media"] = round(statistics.mean(v), 3)
                r[k + "_dp"] = round(statistics.stdev(v), 3) if len(v) > 1 else 0.0
                r[k + "_min"], r[k + "_max"] = round(min(v), 3), round(max(v), 3)
        saida.append(r)
    return saida


def salvar_csv(linhas, destino, campos=None):
    campos = campos or sorted({k for l in linhas for k in l}, key=lambda k: (k != "condicao", k))
    with open(destino, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(linhas)


def tempo_txt(s):
    s = float(s or 0)
    return f"{s / 60:.1f} min" if s >= 90 else f"{s:.1f} s"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robo", required=True, help="ex.: http://192.168.0.50:5051")
    ap.add_argument("--missao", required=True, help="pasta da missão NO ROBÔ")
    ap.add_argument("--condicoes", nargs="+",
                    default=["original", "600/35", "1200/70", "roi:600/35", "roi:1200/70", "roi:1600/85"])
    ap.add_argument("--repeticoes", type=int, default=3)
    ap.add_argument("--saida", default=f"experimento_{datetime.now():%Y%m%d_%H%M%S}")
    ap.add_argument("--semente", type=int, default=None, help="fixa a ordem aleatória (reprodutível)")
    ap.add_argument("--sem-embaralhar", action="store_true", help="mesma ordem em todas as repetições")
    ap.add_argument("--sem-coletor", action="store_true")
    ap.add_argument("--salvar-zips", action="store_true", help="guarda os ZIPs (e confere se abrem)")
    ap.add_argument("--pausa", type=float, default=10, help="segundos parados entre execuções")
    ap.add_argument("--pausa-antes-download", type=float, default=0)
    ap.add_argument("--ping-host", default=None, help="IP do PC para o robô pingar (padrão: o IP visto pelo robô)")
    ap.add_argument("--rotulo", default="", help="texto livre gravado no robô (ex.: 'tc corrigido, robô deitado')")
    ap.add_argument("--intervalo", type=float, default=1.0)
    ap.add_argument("--tempo-max-processamento", type=float, default=3600)
    ap.add_argument("--timeout-download", type=float, default=120, help="segundos sem receber nada")
    ap.add_argument("--simular", action="store_true", help="só mostra a ordem das execuções")
    args = ap.parse_args(argv)
    args.robo = args.robo.rstrip("/")

    import configuracoes   # valida as condições antes de começar
    for c in args.condicoes:
        configuracoes.ler_condicao(c)

    rng = random.Random(args.semente)
    plano = []
    for rep in range(1, args.repeticoes + 1):
        bloco = list(args.condicoes)
        if not args.sem_embaralhar:
            rng.shuffle(bloco)
        plano += [(rep, c) for c in bloco]

    print(f"{len(plano)} execuções ({len(args.condicoes)} condições × {args.repeticoes} repetições)")
    for i, (rep, c) in enumerate(plano, 1):
        print(f"  {i:3d}. rep {rep}  {c}")
    if args.simular:
        return 0

    try:
        pedir(args.robo + "/api/fila")
    except Exception as erro:
        print(f"\nNão consegui falar com o robô em {args.robo}: {erro}")
        return 1

    pasta_coletor = os.path.join(args.saida, "coletor")
    pasta_zips = os.path.join(args.saida, "zips") if args.salvar_zips else None
    for p in (args.saida, pasta_coletor, pasta_zips):
        if p:
            os.makedirs(p, exist_ok=True)
    with open(os.path.join(args.saida, "parametros.json"), "w", encoding="utf-8") as f:
        json.dump({**vars(args), "plano": plano, "inicio": datetime.now().isoformat()}, f, indent=2, ensure_ascii=False)

    linhas = []
    arquivo_medicoes = os.path.join(args.saida, "medicoes.csv")
    t0 = time.time()
    for i, (rep, c) in enumerate(plano, 1):
        print(f"[{i}/{len(plano)}] rep {rep}  {c}")
        linha = executar(args, i, rep, c, pasta_coletor, pasta_zips)
        linhas.append(linha)
        salvar_csv(linhas, arquivo_medicoes, CAMPOS)      # grava a cada execução: nada se perde
        if linha["status"] == "ok":
            print(f"    {int(linha['tamanho_bytes']) / 1048576:.2f} MB | processamento {tempo_txt(linha['t_processamento_s'])}"
                  f" | transferência {tempo_txt(linha['t_transferencia_s'])} ({linha['taxa_Mbps']} Mbit/s)")
        else:
            print(f"    ERRO: {linha.get('erro')}")
        if i < len(plano) and args.pausa:
            time.sleep(args.pausa)

    resumo = resumir(linhas, args.condicoes)
    salvar_csv(resumo, os.path.join(args.saida, "resumo.csv"))
    print(f"\nTerminado em {tempo_txt(time.time() - t0)}. Resultados em {os.path.abspath(args.saida)}")
    print(f"{'condição':<14}{'n':>3}{'MB':>9}{'proc.':>11}{'transf.':>11}{'Mbit/s':>9}")
    for r in resumo:
        if r["n_ok"]:
            print(f"{r['condicao']:<14}{r['n_ok']:>3}{r['tamanho_bytes_media'] / 1048576:>9.2f}"
                  f"{tempo_txt(r['t_processamento_s_media']):>11}{tempo_txt(r['t_transferencia_s_media']):>11}"
                  f"{r['taxa_Mbps_media']:>9.3f}")
        else:
            print(f"{r['condicao']:<14}  0  (todas com erro)")
    return 0 if all(l["status"] == "ok" for l in linhas) else 2


if __name__ == "__main__":
    sys.exit(main())
