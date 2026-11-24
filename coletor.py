"""
Coletor de dados do robô a 1 Hz, em CSV (substitui o monitor_anymal.py nas medições).

Lê só /proc e /sys (sem psutil) e grava, por amostra:
  timestamp, timestamp_epoch, fase, cpu_percent, ram_used_MB, ram_percent,
  rx_Mbps, tx_Mbps, tx_pacotes_s, tx_descartes, ping_ms, ping_ok,
  disk_read_MBps, disk_write_MBps

Também salva `tc -s qdisc show dev <interface>` no início e no fim (descartes da fila
do traffic control) e um resumo .json da execução.

Uso pelo servidor (um coletor por relatório) ou sozinho, para medir o tráfego de fundo:
  python3 coletor.py --segundos 30 --saida fundo_robo_deitado.csv [--ping 192.168.0.10]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import subprocess
import threading
import time
from datetime import datetime

CAMPOS = ["timestamp", "timestamp_epoch", "fase", "cpu_percent", "ram_used_MB", "ram_percent",
          "rx_Mbps", "tx_Mbps", "tx_pacotes_s", "tx_descartes", "ping_ms", "ping_ok",
          "disk_read_MBps", "disk_write_MBps"]

_RE_DISCO = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|mmcblk\d+|vd[a-z]+|xvd[a-z]+)$")


def interface_padrao() -> str:
    """Interface da rota padrão (a que vai para a rede da planta). COLETOR_INTERFACE força outra."""
    if os.environ.get("COLETOR_INTERFACE"):
        return os.environ["COLETOR_INTERFACE"]
    try:
        with open("/proc/net/route") as f:
            for linha in f.readlines()[1:]:
                partes = linha.split()
                if len(partes) > 2 and partes[1] == "00000000":
                    return partes[0]
    except OSError:
        pass
    return "eth0"


def _ler(caminho, padrao=0):
    try:
        with open(caminho) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return padrao


def _cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    ocioso = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), ocioso


def _memoria():
    info = {}
    with open("/proc/meminfo") as f:
        for linha in f:
            k, v = linha.split(":", 1)
            info[k] = int(v.split()[0])
    total = info.get("MemTotal", 1)
    usada = total - info.get("MemAvailable", info.get("MemFree", 0))
    return usada / 1024, 100.0 * usada / total


def _disco():
    lidos = escritos = 0
    try:
        with open("/proc/diskstats") as f:
            for linha in f:
                p = linha.split()
                if len(p) > 9 and _RE_DISCO.match(p[2]):
                    lidos += int(p[5])
                    escritos += int(p[9])
    except OSError:
        pass
    return lidos * 512, escritos * 512


def snapshot_tc(interface: str) -> str:
    try:
        r = subprocess.run(["tc", "-s", "qdisc", "show", "dev", interface],
                           capture_output=True, text=True, timeout=5)
        return r.stdout + r.stderr
    except Exception as erro:
        return f"(tc indisponível: {erro})"


def descartes_tc(texto: str) -> int:
    """Soma dos 'dropped N' de todas as qdiscs (inclui filhas: é uma aproximação)."""
    return sum(int(n) for n in re.findall(r"dropped (\d+)", texto))


class _Pinger(threading.Thread):
    """Ping de 1 em 1 s em paralelo, para não atrasar a amostragem."""

    def __init__(self, host):
        super().__init__(daemon=True)
        self.host, self.ultimo, self.parar = host, (None, False), threading.Event()

    def run(self):
        while not self.parar.is_set():
            t0 = time.monotonic()
            try:
                r = subprocess.run(["ping", "-c", "1", "-W", "1", self.host],
                                   capture_output=True, text=True, timeout=3)
                m = re.search(r"time[=<]([\d.]+)", r.stdout)
                self.ultimo = (float(m.group(1)) if m else None, bool(m))
            except FileNotFoundError:          # sem o comando ping: não registra perda falsa
                self.ultimo = (None, "")
                return
            except Exception:
                self.ultimo = (None, False)
            self.parar.wait(max(0.0, 1.0 - (time.monotonic() - t0)))


class Coletor:
    """
    c = Coletor("pasta/job.csv", ping_host="192.168.0.10"); c.iniciar()
    c.fase = "download"; ...; resumo = c.parar()
    """

    def __init__(self, destino_csv: str, interface: str = None, ping_host: str = None,
                 intervalo: float = 1.0, max_segundos: float = 3 * 3600):
        self.destino = destino_csv
        self.interface = interface or interface_padrao()
        self.ping_host = ping_host
        self.intervalo = intervalo
        self.max_segundos = max_segundos
        self.fase = ""
        self._parar = threading.Event()
        self._thread = None
        self._pinger = None
        self._trava = threading.Lock()   # parar() pode ser chamado por dois lados ao mesmo tempo
        self._linhas = []
        self.resumo = {}
        base = os.path.splitext(destino_csv)[0]
        self.arquivo_tc_antes = base + "_tc_antes.txt"
        self.arquivo_tc_depois = base + "_tc_depois.txt"
        self.arquivo_resumo = base + "_resumo.json"

    @property
    def ativo(self):
        return self._thread is not None and self._thread.is_alive()

    def iniciar(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.destino)), exist_ok=True)
        self._tc_antes = snapshot_tc(self.interface)
        with open(self.arquivo_tc_antes, "w") as f:
            f.write(self._tc_antes)
        if self.ping_host:
            self._pinger = _Pinger(self.ping_host)
            self._pinger.start()
        self._thread = threading.Thread(target=self._laco, daemon=True)
        self._thread.start()
        return self

    def parar(self) -> dict:
        with self._trava:
            return self._parar_uma_vez()

    def _parar_uma_vez(self) -> dict:
        if self._thread is None:
            return self.resumo
        self._parar.set()
        self._thread.join(timeout=5)
        if self._pinger:
            self._pinger.parar.set()
        tc_depois = snapshot_tc(self.interface)
        with open(self.arquivo_tc_depois, "w") as f:
            f.write(tc_depois)
        self.resumo = self._resumir(descartes_tc(tc_depois) - descartes_tc(self._tc_antes))
        with open(self.arquivo_resumo, "w") as f:
            json.dump(self.resumo, f, indent=2)
        self._thread = None
        return self.resumo

    def _laco(self):
        sc = f"/sys/class/net/{self.interface}/statistics/"
        cpu_t, cpu_o = _cpu()
        rx, tx = _ler(sc + "rx_bytes"), _ler(sc + "tx_bytes")
        txp, txd = _ler(sc + "tx_packets"), _ler(sc + "tx_dropped")
        dl, de = _disco()
        t_ant = time.monotonic()
        inicio = t_ant
        with open(self.destino, "w", newline="") as arq:
            w = csv.DictWriter(arq, fieldnames=CAMPOS)
            w.writeheader()
            while not self._parar.wait(self.intervalo):
                agora = time.monotonic()
                dt = max(1e-3, agora - t_ant)
                c_t, c_o = _cpu()
                n_rx, n_tx = _ler(sc + "rx_bytes"), _ler(sc + "tx_bytes")
                n_txp, n_txd = _ler(sc + "tx_packets"), _ler(sc + "tx_dropped")
                n_dl, n_de = _disco()
                usada, pct = _memoria()
                ping_ms, ping_ok = self._pinger.ultimo if self._pinger else (None, "")
                linha = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "timestamp_epoch": round(time.time(), 3),
                    "fase": self.fase,
                    "cpu_percent": round(100.0 * (1 - (c_o - cpu_o) / max(1, c_t - cpu_t)), 1),
                    "ram_used_MB": round(usada, 1), "ram_percent": round(pct, 1),
                    "rx_Mbps": round((n_rx - rx) * 8 / dt / 1e6, 4),
                    "tx_Mbps": round((n_tx - tx) * 8 / dt / 1e6, 4),
                    "tx_pacotes_s": round((n_txp - txp) / dt, 1),
                    "tx_descartes": n_txd - txd,
                    "ping_ms": "" if ping_ms is None else ping_ms,
                    "ping_ok": "" if ping_ok == "" else int(bool(ping_ok)),
                    "disk_read_MBps": round((n_dl - dl) / dt / 1048576, 3),
                    "disk_write_MBps": round((n_de - de) / dt / 1048576, 3),
                }
                w.writerow(linha)
                arq.flush()
                self._linhas.append(linha)
                cpu_t, cpu_o, rx, tx, txp, txd, dl, de, t_ant = c_t, c_o, n_rx, n_tx, n_txp, n_txd, n_dl, n_de, agora
                if agora - inicio > self.max_segundos:
                    break

    def _resumir(self, descartes_qdisc):
        ls = self._linhas
        if not ls:
            return {"amostras": 0}

        def media(k, filtro=None):
            v = [l[k] for l in ls if (filtro is None or l["fase"] == filtro) and l[k] != ""]
            return round(statistics.mean(v), 4) if v else None

        pings = [l for l in ls if l["ping_ok"] != ""]
        perdidos = sum(1 for l in pings if l["ping_ok"] == 0)
        oks = sorted(l["ping_ms"] for l in pings if l["ping_ok"] == 1)
        return {
            "interface": self.interface, "amostras": len(ls),
            "inicio": ls[0]["timestamp"], "fim": ls[-1]["timestamp"],
            "cpu_percent_medio": media("cpu_percent"),
            "tx_Mbps_medio": media("tx_Mbps"), "rx_Mbps_medio": media("rx_Mbps"),
            "tx_Mbps_medio_download": media("tx_Mbps", "download"),
            "tx_pacotes_s_medio": media("tx_pacotes_s"),
            "tx_descartes_interface": sum(l["tx_descartes"] for l in ls),
            "descartes_qdisc": descartes_qdisc,
            "pings": len(pings), "pings_perdidos": perdidos,
            "perda_ping_percent": round(100.0 * perdidos / len(pings), 1) if pings else None,
            "ping_mediano_ms": oks[len(oks) // 2] if oks else None,
        }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Coletor 1 Hz (CPU, RAM, rede, disco, ping, tc)")
    ap.add_argument("--segundos", type=float, default=30)
    ap.add_argument("--saida", default=f"coletor_{datetime.now():%Y%m%d_%H%M%S}.csv")
    ap.add_argument("--interface", default=None)
    ap.add_argument("--ping", default=None, help="IP do PC que recebe os relatórios")
    a = ap.parse_args(argv)
    c = Coletor(a.saida, a.interface, a.ping).iniciar()
    print(f"Coletando {a.segundos:.0f} s na interface {c.interface} -> {a.saida}")
    try:
        time.sleep(a.segundos)
    except KeyboardInterrupt:
        pass
    print(json.dumps(c.parar(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
