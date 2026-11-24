#!/usr/bin/env bash
# Descobre POR QUE a vazão cai para ~0,06 Mbit/s com o tbf 3 Mbit/s + netem 167 ms.
#
# No PC (Linux), deixe aberto:      iperf3 -s
# No robô:                          sudo bash rede/diagnostico_rede.sh <IP_DO_PC> [segundos]
#
# Mede o MESMO envio TCP (iperf3, robô -> PC) em várias configurações, mudando um fator
# de cada vez, e o tráfego de fundo que sai da interface (por destino). Leva ~6 min.
# Nunca deixa limite aplicado: ao terminar ou com Ctrl+C, remove o tc e religa o TSO/GSO.
#
# Configurações:
#   A_sem_limite         referência: o que o link (Wi-Fi) entrega sem tc
#   B_artigo             tbf 3mbit burst 32kbit + netem 167ms limit 140 (exatamente o do artigo)
#   C_so_taxa            só o tbf (sem atraso)
#   D_so_atraso          só o netem 167 ms (sem limite de taxa)
#   E_artigo_fila_1000   B com a fila do netem em 1000 pacotes
#   F_artigo_sem_tso     B com TSO/GSO desligados na placa
#   G_destino            3 Mbit/s + 167 ms SÓ para o IP do PC (rede/limitar.sh destino)

set -u
PC="${1:-}"
SEG="${2:-20}"
IF="${IF:-enp0s31f6}"
if [ -z "$PC" ]; then
    echo "Uso: sudo bash $0 <IP_DO_PC> [segundos]   (e no PC: iperf3 -s)"
    exit 1
fi
if [ "$(id -u)" != 0 ]; then
    echo "Rode com sudo: sudo bash $0 $*"
    exit 1
fi

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAIDA="$AQUI/diagnostico_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SAIDA"
echo "Resultados em $SAIDA"

# ---------------- dependências ----------------
falta=""
for c in iperf3 tcpdump ethtool ping tc python3; do command -v "$c" >/dev/null || falta="$falta $c"; done
if [ -n "$falta" ]; then
    echo "Instalando:$falta"
    pkgs=""
    for c in $falta; do
        case "$c" in
            ping) pkgs="$pkgs iputils-ping" ;;
            tc) pkgs="$pkgs iproute2" ;;
            *) pkgs="$pkgs $c" ;;
        esac
    done
    DEBIAN_FRONTEND=noninteractive apt-get install -y $pkgs || { echo "Falhou instalar:$pkgs"; exit 1; }
fi

# ---------------- limpeza garantida ----------------
OFFLOAD_ORIG="$(ethtool -k "$IF" 2>/dev/null | grep -E '^(tcp-segmentation-offload|generic-segmentation-offload):')"
limpar() {
    tc qdisc del dev "$IF" root 2>/dev/null || true
    ethtool -K "$IF" tso on gso on 2>/dev/null || true
}
restaurar() {
    limpar
    # volta o TSO/GSO para como estavam antes
    echo "$OFFLOAD_ORIG" | grep -q "^tcp-segmentation-offload: off" && ethtool -K "$IF" tso off 2>/dev/null
    echo "$OFFLOAD_ORIG" | grep -q "^generic-segmentation-offload: off" && ethtool -K "$IF" gso off 2>/dev/null
    echo "Limite removido da $IF."
}
trap restaurar EXIT
trap 'exit 130' INT TERM
limpar

# ---------------- informações ----------------
{
    echo "data: $(date -Is)"; echo "kernel: $(uname -r)"; echo "interface: $IF"; echo "pc: $PC"
    echo; ip -4 addr; echo; ip route; echo; ip route get "$PC"
    echo; ethtool "$IF" 2>/dev/null | grep -E "Speed|Duplex|Link detected"
    echo; ethtool -k "$IF" 2>/dev/null | grep -E "segmentation-offload|generic-receive"
    echo; sysctl net.ipv4.tcp_congestion_control net.ipv4.tcp_limit_output_bytes \
        net.core.default_qdisc net.ipv4.tcp_wmem net.ipv4.tcp_sack 2>/dev/null
    echo; tc qdisc show dev "$IF"
} > "$SAIDA/info.txt" 2>&1
echo "  rota até o PC: $(ip route get "$PC" | head -n1)"
if ! ip route get "$PC" | grep -q "dev $IF"; then
    echo "  ATENÇÃO: o tráfego para o PC NÃO sai pela $IF; o tc dela não afeta o download."
    echo "           Rode: sudo IF=<interface certa> bash rede/diagnostico_rede.sh <IP> (veja a linha acima)."
fi

echo "Conferindo o iperf3 no PC..."
if ! iperf3 -c "$PC" -t 1 >/dev/null 2>&1; then
    echo "Não conectei no iperf3 do PC ($PC:5201). No PC rode: iperf3 -s"
    echo "(se tiver firewall: sudo ufw allow 5201/tcp)"
    exit 1
fi

taxa_tx() { cat "/sys/class/net/$IF/statistics/tx_bytes"; }

# ---------------- tráfego de fundo ----------------
censo() {   # $1 = nome; mede 15 s SEM download: quanto sai e para onde
    local nome="$1"
    echo "  tráfego de fundo ($nome, 15 s, sem download)..."
    tc -s qdisc show dev "$IF" > "$SAIDA/${nome}_fundo_tc_antes.txt"
    local b0; b0=$(taxa_tx)
    # -Q out: tudo o que SAI da interface, de qualquer IP do robô (inclusive o tráfego interno)
    timeout 15 tcpdump -i "$IF" -Q out -nn -q -s 96 -l \
        > "$SAIDA/${nome}_fundo_tcpdump.txt" 2>/dev/null &
    local tpid=$!
    ping -i 1 -c 15 -W 1 "$PC" > "$SAIDA/${nome}_fundo_ping.txt" 2>&1
    wait "$tpid" 2>/dev/null
    echo "$(( $(taxa_tx) - b0 )) 15" > "$SAIDA/${nome}_fundo_tx.txt"
    tc -s qdisc show dev "$IF" > "$SAIDA/${nome}_fundo_tc_depois.txt"
}

# ---------------- uma configuração ----------------
medir() {   # $1 = nome; o tc já está aplicado
    local nome="$1"
    echo "  $nome: envio TCP de ${SEG} s..."
    tc qdisc show dev "$IF" > "$SAIDA/${nome}_tc.txt"
    tc -s qdisc show dev "$IF" > "$SAIDA/${nome}_tc_antes.txt"
    ethtool -k "$IF" 2>/dev/null | grep -E "^(tcp|generic)-segmentation-offload" > "$SAIDA/${nome}_offload.txt"
    local b0; b0=$(taxa_tx)
    ping -i 1 -c "$SEG" -W 1 "$PC" > "$SAIDA/${nome}_ping.txt" 2>&1 &
    local ppid=$!
    iperf3 -c "$PC" -t "$SEG" -J > "$SAIDA/${nome}_iperf.json" 2>&1 &
    local ipid=$!
    sleep 1
    while kill -0 "$ipid" 2>/dev/null; do
        echo "### $(date +%s.%N)" >> "$SAIDA/${nome}_ss.txt"
        ss -tin "dst $PC and dport = :5201" >> "$SAIDA/${nome}_ss.txt" 2>/dev/null
        sleep 0.5
    done
    wait "$ipid" "$ppid" 2>/dev/null
    echo "$(( $(taxa_tx) - b0 )) $SEG" > "$SAIDA/${nome}_tx.txt"
    tc -s qdisc show dev "$IF" > "$SAIDA/${nome}_tc_depois.txt"
    limpar
    sleep 3
}

artigo() {
    tc qdisc replace dev "$IF" root handle 1: tbf rate 3mbit burst 32kbit latency 400ms
    tc qdisc replace dev "$IF" parent 1:1 handle 10: netem delay 167ms limit "${1:-140}"
}

echo "Tráfego de fundo:"
censo A_sem_limite
artigo; censo B_artigo; limpar; sleep 3

echo "Envios TCP:"
medir A_sem_limite
artigo; medir B_artigo
tc qdisc replace dev "$IF" root handle 1: tbf rate 3mbit burst 32kbit latency 400ms; medir C_so_taxa
tc qdisc replace dev "$IF" root handle 1: netem delay 167ms limit 1000; medir D_so_atraso
artigo 1000; medir E_artigo_fila_1000
ethtool -K "$IF" tso off gso off 2>/dev/null; artigo; medir F_artigo_sem_tso
IF="$IF" bash "$AQUI/limitar.sh" destino "$PC" >/dev/null; medir G_destino

echo
python3 "$AQUI/analisar_diagnostico.py" "$SAIDA" | tee "$SAIDA/resumo.md"
echo
echo "Mande a pasta $SAIDA (ou só o resumo.md) para análise."
