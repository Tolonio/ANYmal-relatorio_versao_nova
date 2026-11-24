#!/usr/bin/env bash
# Instala tudo o que o servidor precisa no robô, num venv dentro desta pasta.
#
#   cd ~/Documents/anymalnew
#   bash instalar.sh            # instala e confere
#   bash instalar.sh --servico  # também cria o serviço que liga sozinho com o robô
#
# Pode rodar de novo quantas vezes quiser (ex.: depois de um git pull).

set -u
PASTA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PASTA"
VENV="$PASTA/venv"
PORTA="${SERVIDOR_PORTA:-5051}"

verde() { printf '\033[32m%s\033[0m\n' "$*"; }
vermelho() { printf '\033[31m%s\033[0m\n' "$*"; }
passo() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# um venv ativado de outra pasta atrapalha: usa sempre o desta pasta
if [ -n "${VIRTUAL_ENV:-}" ] && [ "$VIRTUAL_ENV" != "$VENV" ]; then
    echo "Ignorando o venv ativo ($VIRTUAL_ENV); vou usar $VENV"
fi

passo "1/4 Pacotes do sistema (pede a senha do sudo)"
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
echo "(pode levar alguns minutos; se pedir senha, os caracteres não aparecem)"
sudo apt-get update || vermelho "apt-get update falhou (sem internet?). Continuando."
# bibliotecas do weasyprint (as mesmas do README antigo) + tc e ping para o coletor
sudo apt-get install -y \
    python3-venv libcairo2 libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 \
    shared-mime-info iproute2 iputils-ping \
    || vermelho "Algum pacote do apt falhou; se o passo 3 der erro, me mande esta saída."
# no Ubuntu 24.04 o venv precisa do pacote da versão exata (python3.12-venv)
sudo apt-get install -y "python${PY_VER}-venv" >/dev/null 2>&1 || true

passo "2/4 Ambiente virtual em $VENV"
if [ -x "$VENV/bin/python3" ] && "$VENV/bin/python3" -m pip --version >/dev/null 2>&1; then
    echo "venv já existe."
else
    rm -rf "$VENV"
    python3 -m venv "$VENV" || { vermelho "Não consegui criar o venv."; exit 1; }
fi
PY="$VENV/bin/python3"

passo "3/4 Bibliotecas Python (requirements.txt)"
"$PY" -m pip install --upgrade pip -q
"$PY" -m pip install -r requirements.txt || { vermelho "pip install falhou."; exit 1; }

passo "4/4 Conferência"
"$PY" - <<'EOF' || { vermelho "Alguma biblioteca não carregou (veja acima)."; exit 1; }
import importlib
for nome in ("flask", "lxml", "weasyprint", "PIL", "numpy", "deep_translator"):
    m = importlib.import_module(nome)
    print(f"  ok  {nome} {getattr(m, '__version__', '')}")
from weasyprint import HTML
HTML(string="<p>teste</p>").write_pdf()
print("  ok  weasyprint gera PDF")
import processador_relatorio as p
roi = p.info_roi()
if roi["ativa"]:
    print(f"  ok  ROI disponível: {roi['imagens_anotadas']} fotos marcadas; padrão {roi['config']}")
else:
    print(f"  !!  ROI desligada: {roi['motivo']}")
EOF
command -v tc >/dev/null && echo "  ok  tc encontrado (coletor)" || echo "  !!  tc não encontrado (coletor anota 'tc indisponível')"

if [ "${1:-}" = "--servico" ]; then
    passo "Serviço anymal_relatorio (porta $PORTA)"
    sudo tee /etc/systemd/system/anymal_relatorio.service >/dev/null <<EOF
[Unit]
Description=Relatórios ANYmal (versão nova, ROI + fila)
After=network-online.target

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
WorkingDirectory=$PASTA
Environment="PATH=$VENV/bin:/usr/bin:/usr/sbin:/bin:/sbin"
Environment="PYTHONUNBUFFERED=1"
Environment="SERVIDOR_PORTA=$PORTA"
ExecStart=$PY $PASTA/servidor_web.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now anymal_relatorio.service
    sleep 2
    systemctl --no-pager --lines=5 status anymal_relatorio.service
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
verde "Pronto."
echo "Para ligar o servidor:   cd $PASTA && bash iniciar.sh"
echo "Depois abra no PC:       http://${IP:-<ip-do-robo>}:$PORTA"
