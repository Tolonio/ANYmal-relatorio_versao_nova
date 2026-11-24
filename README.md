# ANYmal: relatórios de inspeção (versão nova)

Versão nova do [servidor_anymal](https://github.com/Tolonio/servidor_anymal), com compressão por ROI,
fila de relatórios e coletor de dados.

Servidor Flask que roda no ANYmal: navega pelas missões, gera o relatório traduzido
(PDF + fotos em ZIP) e entrega pelo link limitado da planta. Inclui:

* **compressão uniforme** (L/Q) e **compressão por ROI**, com a peça marcada no Roboflow
  nítida e o resto da foto suavizado;
* **fila de relatórios**: vários relatórios (missão × condição × repetição) gerados um por vez;
* **coletor CSV a 1 Hz** no robô (CPU, RAM, rede, pacotes, disco, ping até o PC e
  `tc -s qdisc` antes/depois), ligado por relatório;
* **`experimento_pc.py`**: roda a matriz de experimentos do PC (VS Code) e mede
  processamento e transferência separadamente;
* **`avaliar_roi.py`**: PSNR/SSIM na foto inteira, só na peça e só no fundo, e ΔE00 na peça.

| Arquivo | Onde roda | O que faz |
|---|---|---|
| `servidor_web.py` | robô | servidor, fila, coletor por relatório |
| `processador_relatorio.py` | robô | tradução, compressão, PDF e ZIP |
| `compressor_roi.py` | robô e PC | lê o export do Roboflow e comprime protegendo a peça |
| `configuracoes.py` | robô e PC | condições em texto: `original`, `1200/70`, `roi:1200/70` |
| `coletor.py` | robô | coletor 1 Hz (também roda sozinho) |
| `experimento_pc.py` | PC | matriz de experimentos (só biblioteca padrão) |
| `avaliar_roi.py` | PC | métricas de qualidade contra as fotos originais |
| `tests/` | PC | `python -m pytest -q tests/` (rodam sem WeasyPrint) |

---

## Condições

| Texto | O que faz |
|---|---|
| `original` | fotos originais no PDF e no ZIP, sem recompressão |
| `1200/70` | uniforme: toda foto cabe em 1200×1200, JPEG qualidade 70 |
| `roi:1200/70` | a peça marcada em 1200/70; o fundo é reduzido para 300 px (L/4), ampliado de volta e pré-quantizado com Q30. Fotos sem marcação (térmicas) saem iguais à `1200/70` |
| `roi:1600/80/400/30` | ROI explícita: lado / qualidade / fundo / qualidade do fundo (0 = sem pré-quantização) |
| `roi:1600/85+pol` | força o contorno (polígono dilatado) em todas as peças |
| `roi:1600/85+circ` | força o círculo mínimo em todas as peças |

**Forma da área protegida** (padrão automático): peças compactas (parafusos, visor) ganham
o menor círculo que as contém, com margem; peças alongadas (escala térmica), cujo círculo
teria mais de 2,5× a área da peça, ficam protegidas pelo próprio contorno, com margem.

**Térmicas:** toda foto 1158×768 tem a **escala de temperatura** (min/max/spot e a coluna
de números, faixa da direita, x de 0,86 a 1,0) protegida automaticamente, **mesmo sem
marcação e em missões novas**: a câmera desenha essa faixa sempre no mesmo lugar (confirmado
nas 25 térmicas marcadas). Para desligar: `RELATORIO_ROI_TERMICA=0`.

`1200/70` × `roi:1200/70` isola o efeito da ROI: a peça e as fotos sem marcação são
idênticas e só muda o fundo das fotos marcadas.

### Quanto comprimir com ROI: resultados preliminares

95 fotos anotadas da missão de 2026-05-20, com métricas contra as cópias do Roboflow
(~470 KB cada; **refazer com as originais de 2,6 MB para o artigo**):

| Nível | Uniforme (KB/foto) | ROI (KB/foto) | Economia | SSIM na peça |
|---|---|---|---|---|
| 600/35 | 12,5 | 8,1 | −35% | 0,661 |
| 800/45 | 23,3 | 14,2 | −39% | 0,713 |
| 1200/70 | 69,7 | 31,4 | −55% | 0,842 |
| 1600/85 | 173,8 | 61,7 | −64% | 0,931 |

* Com o fundo suavizado, o tamanho passa a depender pouco da qualidade da peça. Por isso
  a ROI rende mais nos níveis altos: `roi:1600/85` fica **menor que a uniforme 1200/70**,
  com a peça no nível da 1600/85.
* Em 600/35 e 800/45 a economia em KB é pequena, e a peça já sai degradada (a inspetora
  deu 3,3 e 4,0). A ROI não compensa nesses níveis.
* O polígono (`+pol`) economiza ~7% a mais que o círculo, até ~25% em peças grandes e
  alongadas (sílica).

Térmicas (25 fotos, métricas só na faixa da escala):

| Nível | Uniforme (KB/foto) | ROI (KB/foto) | PSNR na escala |
|---|---|---|---|
| 600/35 | 14,3 | 12,3 | 17,7 dB |
| 800/45 | 24,8 | 21,1 | 19,2 dB |
| 1200/70 | 62,2 | 45,4 | 36,9 dB |
| 1600/85 | 74,0 | 54,5 | 39,9 dB |

Em 600/35 e 800/45 a escala perde qualidade pela **redução** (1158 → 600 px), não pela
compressão: os números ainda se leem, mas serrilhados. A partir de 1200 a térmica
não é reduzida e a escala fica igual à original.

**Matriz sugerida** (5 condições × 3 repetições, missão de 169 imagens):
`original 600/35 1200/70 roi:1200/70 roi:1600/85`. Ela responde se a ROI entrega a peça
da 1200/70 e da 1600/85 com menos bytes. Se couber, `roi:1600/85+pol` também.

---

## Instalação no robô

O robô precisa de internet só nesta etapa. Um comando instala tudo: os pacotes do sistema
do WeasyPrint, o venv dentro da pasta e as bibliotecas do `requirements.txt` (as do servidor
antigo, `flask lxml weasyprint deep-translator`, mais `pillow numpy`). No fim ele confere
tudo e diz se a ROI está disponível.

```bash
mkdir -p ~/Documents/anymalnew
cd ~/Documents/anymalnew
git clone https://github.com/Tolonio/ANYmal-relatorio_versao_nova.git .
bash instalar.sh
```

Para ligar (Ctrl+C para parar) e abrir no PC em `http://<ip-do-robo>:5051`:

```bash
cd ~/Documents/anymalnew
bash iniciar.sh
```

Para atualizar depois de mudanças no GitHub:

```bash
cd ~/Documents/anymalnew
git pull
bash instalar.sh
```

Para o servidor ligar sozinho junto com o robô (cria o serviço `anymal_relatorio`, porta 5051;
o serviço antigo `servidor_anymal`, na 5050, continua intacto):

```bash
bash instalar.sh --servico
sudo systemctl status anymal_relatorio      # ver se está rodando
sudo journalctl -u anymal_relatorio -f      # ver o log
```

No PC, para o `avaliar_roi.py` e os testes: `python -m pip install -r requirements-pc.txt`.

**Anotações de ROI:** já vêm no repositório, em `anotacoes_roi/_annotations.coco.json`
(export v2 do Roboflow: 99 fotos RGB e 25 térmicas da missão de 2026-05-20; só nomes de
arquivo e coordenadas, sem fotos). Para trocar, exporte do Roboflow em **COCO**, sem
augmentation e sem Resize, e substitua esse arquivo. A ligação marcação ↔ foto das RGB
é pelo nome do arquivo. Confira antes de medir:

```bash
venv/bin/python3 compressor_roi.py verificar --anotacoes anotacoes_roi/ --imagens <pasta resources> --saida conferencia/
```

**Porta:** a versão nova usa a **5051** (a antiga usa a 5050), então as duas podem ficar
ligadas ao mesmo tempo. Para outra porta: `SERVIDOR_PORTA=8085 python3 servidor_web.py`.
Para ver o que ocupa uma porta: `sudo ss -ltnp | grep 5051`.

**Variáveis úteis:**
`RELATORIO_CONDICAO=roi:1200/70` (condição do botão "Gerar relatório"),
`RELATORIO_ROI_ANOTACOES=/caminho`, `COLETOR_INTERFACE=enp0s31f6` (padrão: a interface da rota padrão),
`COLETOR_ESPERA_DOWNLOAD_S=1800`.

---

## Interface

* **Missões**: "Gerar relatório" usa a condição padrão; "Experimento" abre a fila com
  as condições do artigo marcáveis, repetições, ordem aleatória e coletor.
* **Fila**: o estado de cada relatório (na fila / processando / pronto), download e
  link do CSV do coletor.
* **Relatórios salvos**: todos os ZIPs em `~/relatorios_gerados`.

## Diagnóstico: por que a vazão cai para ~0,06 Mbit/s com o tc

No artigo, o tbf (3 Mbit/s) e o netem (167 ms) ficam na raiz da `enp0s31f6` do computador
de navegação. Assim eles limitam **tudo** o que sai dela, inclusive o tráfego interno do
robô (LiDAR, câmeras, ROS), que na planta não passa pelo link de 3 Mbit/s. A hipótese é que
esse tráfego enche a fila (netem `limit 140`), os pacotes do relatório se perdem e o TCP cai
para cerca de 1 pacote por ida e volta (1448 B / 0,17 s ≈ 0,07 Mbit/s).

O diagnóstico mede o mesmo envio TCP (iperf3, robô → PC) mudando um fator por vez:
sem limite, limite do artigo, só taxa, só atraso, fila 1000, sem TSO/GSO e limite só para
o PC. Também mede quanto tráfego de fundo sai do robô e para quem.

No **PC** (Linux), deixe aberto:

```bash
sudo apt install -y iperf3
iperf3 -s
```

No **robô** (~6 min; nunca deixa limite aplicado no fim):

```bash
cd ~/Documents/anymalnew
sudo bash rede/diagnostico_rede.sh <IP_DO_PC>
```

Sai uma pasta `rede/diagnostico_<data>/` com tudo e um `resumo.md` com as tabelas e a
leitura automática.

**Limitar a rede** (substitui os comandos `tc` à mão):

```bash
sudo bash rede/limitar.sh artigo                # igual ao artigo: limita tudo que sai da interface
sudo bash rede/limitar.sh destino <IP_DO_PC>    # limita só o que vai para o PC (como o link da planta)
sudo bash rede/limitar.sh status
sudo bash rede/limitar.sh desligar
```

## Rodar a matriz de experimentos do PC (VS Code)

No PC ligado à rede do robô (com o `tc` aplicado no robô):

```powershell
python experimento_pc.py --robo http://<IP_DO_ROBO>:5051 `
    --missao /home/integration/.ros/reports/<pasta da missão> `
    --condicoes original 600/35 1200/70 roi:1200/70 roi:1600/85 `
    --repeticoes 3 --rotulo "tc corrigido, robô deitado" --saida experimento_01
```

* `--simular` só mostra a ordem; `--semente 42` torna a ordem reprodutível.
* Pede **um relatório por vez**: espera processar, baixa, coleta e só então pede o
  próximo (com `--pausa 10` s entre execuções). Downloads não disputam o link.
* `medicoes.csv`: tamanho, tempo de processamento (total e por etapa), tempo até o 1º
  byte, tempo de transferência, taxa, perda de ping e descartes da fila do `tc`.
* `resumo.csv`: média, desvio, mín. e máx. por condição. `coletor/`: CSV 1 Hz de cada execução.
* No robô, `~/relatorios_gerados/registro_experimentos.csv` também registra tudo.

**Tráfego de fundo** (ex.: 30 s com o robô deitado), no robô:

```bash
python3 coletor.py --segundos 30 --ping <IP_DO_PC> --saida fundo_deitado.csv
```

ou pelo PC: `POST /api/coletor/iniciar?rotulo=deitado` e depois `POST /api/coletor/parar`.

## Qualidade contra as originais (PC)

```bash
pip install numpy pillow scikit-image matplotlib
python avaliar_roi.py --imagens <pasta das 169 originais> --anotacoes anotacoes_roi/ --saida resultados_roi --workers 4
```

Por padrão compara as 4 uniformes do artigo com a ROI nos mesmos 4 níveis. Gera
`resumo.md`/`.csv`, `fig_compromisso.png` (tamanho × SSIM na peça), recortes da mesma
peça em cada condição e exemplos de foto inteira com ROI.

## API (para scripts)

| Rota | |
|---|---|
| `POST /api/fila` | `{"missao", "condicoes": [...], "repeticoes", "embaralhar", "coletor", "rotulo"}` → `{"jobs": [...]}` |
| `GET /api/fila` | estado de todos os relatórios |
| `GET /api/job/<id>` | estado, tempos por etapa, `url_download` |
| `POST /api/job/<id>/cancelar` | tira da fila |
| `GET /api/coletor/arquivos/<id>` · `GET /coletor/<arquivo>` | arquivos do coletor do relatório |
| `POST /api/coletor/iniciar` · `POST /api/coletor/parar` | coletor manual |
