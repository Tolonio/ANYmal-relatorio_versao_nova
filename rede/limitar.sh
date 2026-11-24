#!/usr/bin/env bash
# Limita a rede do robô para emular o link da planta (3 Mbit/s, 167 ms).
#
#   sudo bash rede/limitar.sh artigo                 # exatamente como no artigo (TUDO que sai da interface)
#   sudo bash rede/limitar.sh destino 192.168.0.50   # só o tráfego que vai para esses IPs (vírgula separa vários)
#   sudo bash rede/limitar.sh status
#   sudo bash rede/limitar.sh desligar
#
# Variáveis: IF=enp0s31f6  TAXA=3mbit  ATRASO=167ms  FILA=140 (pacotes, igual ao artigo)
#
# Por que "destino": o modo do artigo põe o tbf na raiz da interface do computador de
# navegação, então limita também o tráfego interno do robô (LiDAR, câmeras, ROS entre os
# computadores de bordo), que na planta NÃO passa pelo link de 3 Mbit/s. No diagnóstico de
# 24/11/2026 eram ~4.300 pacotes/s pequenos do NPC (192.168.0.169) para o LPC, o computador de
# locomoção (192.168.0.168, anymal-d021-lpc): a fila do
# netem (140 PACOTES) ficava sempre cheia deles (138/140) e recusava os pacotes do relatório
# ainda dentro do robô, então o TCP quase não enviava (0,08 Mbit/s, sem retransmissões).
# O modo "destino" limita só o que vai para o PC (e para outros IPs da planta que você
# listar), como o link real: 2,74 Mbit/s no mesmo diagnóstico.

set -u
IF="${IF:-enp0s31f6}"
TAXA="${TAXA:-3mbit}"
ATRASO="${ATRASO:-167ms}"
FILA="${FILA:-140}"   # 1000 dava 2,7 Mbit/s mas RTT de até 5 s (fila funda demais)
MODO="${1:-status}"

if [ "$(id -u)" != 0 ] && [ "$MODO" != status ]; then
    echo "Rode com sudo: sudo bash $0 $*"
    exit 1
fi

limpar() { tc qdisc del dev "$IF" root 2>/dev/null || true; }

case "$MODO" in
  artigo)
    limpar
    tc qdisc replace dev "$IF" root handle 1: tbf rate "$TAXA" burst 32kbit latency 400ms
    tc qdisc replace dev "$IF" parent 1:1 handle 10: netem delay "$ATRASO" limit 140
    ;;
  destino)
    IPS="${2:-}"
    if [ -z "$IPS" ]; then echo "Informe o IP do PC: sudo bash $0 destino 192.168.0.50"; exit 1; fi
    limpar
    # raiz HTB: classe 1:10 = link da planta (3 Mbit/s + atraso); 1:20 = resto, sem limite prático
    tc qdisc add dev "$IF" root handle 1: htb default 20
    tc class add dev "$IF" parent 1: classid 1:10 htb rate "$TAXA" ceil "$TAXA" burst 15k cburst 15k
    tc class add dev "$IF" parent 1: classid 1:20 htb rate 1gbit ceil 1gbit quantum 60000
    tc qdisc add dev "$IF" parent 1:10 handle 10: netem delay "$ATRASO" limit "$FILA"
    tc qdisc add dev "$IF" parent 1:20 handle 20: fq_codel
    for ip in ${IPS//,/ }; do
        tc filter add dev "$IF" parent 1: protocol ip prio 1 u32 match ip dst "$ip/32" flowid 1:10
    done
    ;;
  desligar)
    limpar
    ;;
  status)
    ;;
  *)
    echo "Modos: artigo | destino <IPs> | desligar | status"
    exit 1
    ;;
esac

echo "Interface $IF:"
tc qdisc show dev "$IF"
tc filter show dev "$IF" 2>/dev/null | grep -E "match|flowid" | head -n 12
