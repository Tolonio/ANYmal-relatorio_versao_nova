"""
Resume a pasta gerada por rede/diagnostico_rede.sh (só biblioteca padrão).

    python3 rede/analisar_diagnostico.py rede/diagnostico_AAAAmmdd_HHMMSS
"""

import json
import os
import re
import statistics
import sys
from collections import Counter

CONFIGS = [
    ("A_sem_limite", "sem limite (referência)"),
    ("B_artigo", "artigo: tbf + netem limit 140"),
    ("C_so_taxa", "só tbf 3 Mbit/s"),
    ("D_so_atraso", "só netem 167 ms"),
    ("E_artigo_fila_1000", "artigo com fila 1000"),
    ("F_artigo_sem_tso", "artigo sem TSO/GSO"),
    ("G_destino", "3 Mbit/s + 167 ms só para o PC"),
]


def ler(caminho):
    try:
        with open(caminho, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def iperf(pasta, nome):
    txt = ler(os.path.join(pasta, f"{nome}_iperf.json"))
    if not txt:
        return {}
    try:
        d = json.loads(txt)
    except ValueError:
        return {"erro": txt.strip().splitlines()[-1][:80] if txt.strip() else "vazio"}
    if "error" in d:
        return {"erro": d["error"][:80]}
    env = d.get("end", {}).get("sum_sent", {})
    rec = d.get("end", {}).get("sum_received", {})
    return {"mbps": rec.get("bits_per_second", env.get("bits_per_second", 0)) / 1e6,
            "retrans": env.get("retransmits"),
            "bytes": rec.get("bytes", env.get("bytes"))}


def ping(pasta, arq):
    txt = ler(os.path.join(pasta, arq))
    perda = re.search(r"([\d.]+)% packet loss", txt)
    rtt = re.search(r"= [\d.]+/([\d.]+)/", txt)
    return (float(perda.group(1)) if perda else None, float(rtt.group(1)) if rtt else None)


def descartes(pasta, antes, depois):
    def soma(t, chave):
        return sum(int(n) for n in re.findall(chave + r" (\d+)", t))
    a, d = ler(os.path.join(pasta, antes)), ler(os.path.join(pasta, depois))
    if not d:
        return None, None
    # só a qdisc raiz (primeira "Sent ... dropped"): evita contar duas vezes a filha
    def raiz(t):
        m = re.search(r"Sent \d+ bytes \d+ pkt \(dropped (\d+), overlimits (\d+)", t)
        return (int(m.group(1)), int(m.group(2))) if m else (soma(t, "dropped"), 0)
    ra, rd = raiz(a), raiz(d)
    return rd[0] - ra[0], rd[1] - ra[1]


def tx_mbps(pasta, arq):
    t = ler(os.path.join(pasta, arq)).split()
    return int(t[0]) * 8 / float(t[1]) / 1e6 if len(t) == 2 and float(t[1]) > 0 else None


def ss_resumo(pasta, nome):
    txt = ler(os.path.join(pasta, f"{nome}_ss.txt"))
    cwnd = [int(x) for x in re.findall(r"\bcwnd:(\d+)", txt)]
    rtt = [float(x) for x in re.findall(r"\brtt:([\d.]+)/", txt)]
    retr = re.findall(r"\bretrans:\d+/(\d+)", txt)
    limit = Counter()
    for chave in ("rwnd_limited", "sndbuf_limited", "app_limited"):
        if chave in txt:
            limit[chave] += txt.count(chave)
    return {"cwnd_mediana": statistics.median(cwnd) if cwnd else None,
            "rtt_mediana_ms": statistics.median(rtt) if rtt else None,
            "retrans_total": int(retr[-1]) if retr else None,
            "limitado_por": ", ".join(k for k, _ in limit.most_common(2))}


def destinos(pasta, nome):
    """Bytes por IP de destino no tcpdump do tráfego de fundo."""
    txt = ler(os.path.join(pasta, f"{nome}_fundo_tcpdump.txt"))
    por = Counter()
    for linha in txt.splitlines():
        m = re.search(r"IP6? (\S+?) > (\S+?):.*?(?:length |tcp |udp )(\d+)", linha)
        if not m:
            continue
        dst = m.group(2).rsplit(".", 1)[0] if m.group(2).count(".") == 4 else m.group(2)
        por[dst] += int(m.group(3))
    return por


def main(pasta):
    print(f"# Diagnóstico de rede: {os.path.basename(pasta.rstrip('/'))}\n")
    info = ler(os.path.join(pasta, "info.txt"))
    for chave in ("kernel:", "pc:", "interface:"):
        m = re.search(rf"^{chave} (.*)$", info, re.M)
        if m:
            print(f"- {chave} {m.group(1)}")
    m = re.search(r"tcp_congestion_control = (\S+)", info)
    if m:
        print(f"- controle de congestionamento: {m.group(1)}")
    print()

    print("## Tráfego de fundo (15 s sem download)\n")
    print("| Situação | Saindo da interface | Perda de ping | RTT médio | Descartes na fila |")
    print("|---|---|---|---|---|")
    for nome, rot in (("A_sem_limite", "sem limite"), ("B_artigo", "limite do artigo")):
        tx = tx_mbps(pasta, f"{nome}_fundo_tx.txt")
        perda, rtt = ping(pasta, f"{nome}_fundo_ping.txt")
        drop, _ = descartes(pasta, f"{nome}_fundo_tc_antes.txt", f"{nome}_fundo_tc_depois.txt")
        print(f"| {rot} | {fmt(tx, ' Mbit/s', 2)} | {fmt(perda, '%', 0)} | {fmt(rtt, ' ms', 0)} | {fmt(drop)} |")
    por = destinos(pasta, "A_sem_limite")
    total = sum(por.values())
    if total:
        print("\nPara onde vai o tráfego de fundo (sem limite):\n")
        pc = re.search(r"^pc: (.*)$", info, re.M)
        for ip, b in por.most_common(6):
            marca = "  ← PC" if pc and ip == pc.group(1).strip() else ""
            print(f"- {ip}: {b * 8 / 15 / 1e6:.2f} Mbit/s ({100 * b / total:.0f}%){marca}")
    print()

    print("## Envio TCP robô → PC (iperf3)\n")
    print("| Configuração | Vazão | Retransmissões | Perda de ping | RTT médio | Descartes na fila | cwnd (mediana) |")
    print("|---|---|---|---|---|---|---|")
    res = {}
    for nome, rot in CONFIGS:
        r = iperf(pasta, nome)
        if not r:
            continue
        perda, rtt = ping(pasta, f"{nome}_ping.txt")
        drop, _ = descartes(pasta, f"{nome}_tc_antes.txt", f"{nome}_tc_depois.txt")
        s = ss_resumo(pasta, nome)
        res[nome] = r.get("mbps")
        vaz = r["erro"] if "erro" in r else fmt(r["mbps"], " Mbit/s", 3)
        print(f"| {rot} | {vaz} | {fmt(r.get('retrans'))} | {fmt(perda, '%', 0)} | {fmt(rtt, ' ms', 0)} "
              f"| {fmt(drop)} | {fmt(s['cwnd_mediana'])} |")
    print()
    print("## Leitura automática\n")
    for linha in interpretar(res):
        print("- " + linha)


def interpretar(r):
    g = lambda k: r.get(k) or 0.0
    out = []
    if not r:
        return ["Sem resultados do iperf3."]
    if g("A_sem_limite") and g("A_sem_limite") < 3:
        out.append(f"Mesmo sem limite o link entrega só {g('A_sem_limite'):.2f} Mbit/s: o gargalo é a rede (Wi-Fi), não o tc.")
    if g("C_so_taxa") > 2.0:
        out.append(f"Só a taxa (tbf) entrega {g('C_so_taxa'):.2f} Mbit/s: o limite de 3 Mbit/s sozinho não explica a queda.")
    elif "C_so_taxa" in r:
        out.append(f"Só a taxa (tbf) já derruba para {g('C_so_taxa'):.2f} Mbit/s: o tbf (fila/burst ou tráfego de fundo) é parte do problema.")
    if "D_so_atraso" in r:
        if g("D_so_atraso") > 2.0:
            out.append(f"Só o atraso entrega {g('D_so_atraso'):.2f} Mbit/s: 167 ms sozinho não explica a queda.")
        else:
            out.append(f"Só o atraso já derruba para {g('D_so_atraso'):.2f} Mbit/s: o netem no próprio robô atrapalha o TCP.")
    b = g("B_artigo")
    for k, txt in (("E_artigo_fila_1000", "aumentar a fila do netem para 1000"),
                   ("F_artigo_sem_tso", "desligar TSO/GSO"),
                   ("G_destino", "limitar só o tráfego para o PC")):
        if k in r and b > 0 and g(k) > 3 * b:
            out.append(f"{txt[0].upper() + txt[1:]} sobe de {b:.2f} para {g(k):.2f} Mbit/s ({g(k) / b:.0f}×): é uma causa.")
        elif k in r:
            out.append(f"{txt[0].upper() + txt[1:]}: {g(k):.2f} Mbit/s (artigo: {b:.2f}).")
    if g("G_destino") > 2.0:
        out.append("Com o limite só para o PC o envio chega perto de 3 Mbit/s: a emulação do artigo limitava também "
                   "o tráfego interno do robô. Use 'rede/limitar.sh destino <IP_DO_PC>' nas próximas medições.")
    return out


def fmt(v, suf="", casas=0):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{casas}f}{suf}"
    return f"{v}{suf}"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
