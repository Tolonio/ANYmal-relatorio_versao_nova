#!/usr/bin/env bash
# Liga o servidor com o venv desta pasta (Ctrl+C para parar).
#   bash iniciar.sh
PASTA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PASTA"
if [ ! -x venv/bin/python3 ]; then
    echo "venv não encontrado: rode antes  bash instalar.sh"
    exit 1
fi
exec venv/bin/python3 servidor_web.py
