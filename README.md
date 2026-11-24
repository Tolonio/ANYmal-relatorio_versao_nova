# Relatórios de inspeção do ANYmal: versão nova

Servidor web que roda **no robô ANYmal** e transforma uma inspeção gravada num
**relatório em português (PDF + fotos, em ZIP)**, pronto para baixar pelo link limitado da
planta. Projeto do Instituto Tecnológico Vale (ITV) com a Vale.

É a versão nova do [servidor_anymal](https://github.com/Tolonio/servidor_anymal) /
[Anymal-Tradu-o](https://github.com/Tolonio/Anymal-Tradu-o). A antiga continua funcionando
(porta 5050); esta usa a porta **5051** e as duas podem ficar ligadas ao mesmo tempo.

**O que há de novo**

* **Compressão por ROI**: a peça marcada no Roboflow fica nítida e o resto da foto é
  borrado, o que deixa o pacote bem menor. Nas térmicas, a escala de temperatura fica
  sempre nítida, mesmo sem marcação.
* **Experimento**: gera vários relatórios da mesma inspeção com compressões diferentes,
  para comparar.
* **Coletor CSV**: grava CPU, memória, rede e perdas a cada segundo durante cada relatório.
* **Ferramentas para o artigo**: medição de tempos a partir do PC, avaliação de qualidade
  das imagens e diagnóstico da rede limitada.

## Sumário

1. [Como funciona](#1-como-funciona)
2. [Instalação no robô](#2-instalação-no-robô)
3. [Usando a página](#3-usando-a-página)
4. [Compressão: condições e ROI](#4-compressão-condições-e-roi)
5. [Resultados preliminares](#5-resultados-preliminares)
6. [Experimentos do artigo](#6-experimentos-do-artigo)
7. [Arquivos do projeto](#7-arquivos-do-projeto)
8. [Configurações](#8-configurações)
9. [Problemas comuns](#9-problemas-comuns)
10. [API e testes](#10-api-e-testes)

---

## 1. Como funciona

```
inspeção gravada no robô              servidor (porta 5051)                        PC
/home/integration/.ros/reports/...    ─────────────────────                       ──
  report.xml  ─────────────────────►  1. traduz as mensagens para o português
  resources/*.jpg  ────────────────►  2. comprime cada foto (uniforme ou ROI)
                                      3. monta o PDF com as fotos comprimidas
                                      4. empacota PDF + fotos num ZIP  ─────────►  download
```

* Os pedidos entram numa **fila** e o robô gera **um relatório de cada vez**.
* Cada foto é comprimida **uma vez só**, e a mesma versão vai no PDF e no ZIP.
* Tudo é registrado em `~/relatorios_gerados/registro_experimentos.csv`, com os tempos de
  cada etapa separados do tempo de download.

## 2. Instalação no robô

O robô precisa de internet **só durante a instalação**.

### 2.1 Primeira vez

```bash
mkdir -p ~/Documents/anymalnew
cd ~/Documents/anymalnew
git clone https://github.com/Tolonio/ANYmal-relatorio_versao_nova.git .
bash instalar.sh
```

O ponto no fim do `git clone` coloca os arquivos direto dentro de `anymalnew` (a pasta
precisa estar vazia). O `instalar.sh` pede a senha do `sudo` e faz quatro passos:

1. instala os pacotes do sistema: as bibliotecas do WeasyPrint (as mesmas da versão
   antiga), o `venv` do Python e o `tc`/`ping` usados pelo coletor;
2. cria o ambiente virtual `venv/` **dentro da pasta**;
3. instala as bibliotecas do [`requirements.txt`](requirements.txt): `flask lxml
   weasyprint deep-translator` (as da versão antiga) e `pillow numpy` (novas);
4. confere tudo, gera um PDF de teste e informa se a ROI está disponível:

```
  ok  weasyprint gera PDF
  ok  ROI disponível: 122 fotos marcadas; padrão roi:1600/80
Pronto.
```

> Se a pasta já tiver um `venv` criado antes do clone, use
> `git init -b main && git remote add origin https://github.com/Tolonio/ANYmal-relatorio_versao_nova.git && git pull origin main && git branch --set-upstream-to=origin/main main`
> no lugar do `git clone`.

### 2.2 Ligar o servidor

```bash
cd ~/Documents/anymalnew
bash iniciar.sh
```

No PC, abra **`http://<IP-do-robô>:5051`** (no laboratório: `http://192.168.0.169:5051`).
Deixe o terminal aberto; `Ctrl+C` desliga.

### 2.3 Atualizar

```bash
cd ~/Documents/anymalnew
git pull
bash instalar.sh          # só instala o que faltar
```

Depois **reinicie o servidor** (`Ctrl+C` e `bash iniciar.sh`): ele só lê as páginas e o
código novos ao ligar.

### 2.4 Ligar sozinho junto com o robô (opcional)

```bash
bash instalar.sh --servico
sudo systemctl status anymal_relatorio      # está rodando?
sudo journalctl -u anymal_relatorio -f      # log ao vivo
sudo systemctl restart anymal_relatorio     # depois de um git pull
sudo systemctl disable --now anymal_relatorio   # desfaz
```

O serviço antigo (`servidor_anymal`, porta 5050) não é alterado.

## 3. Usando a página

### Inspeções
Lista as inspeções gravadas no robô. Cada uma tem dois botões:

* **Gerar relatório**: gera o relatório com a **condição padrão** (`roi:1600/80`: peça
  marcada em 1600/80, fotos sem marcação em 800/45), mostra o progresso e baixa o ZIP
  sozinho no fim.
* **Experimento**: abre a página para escolher a compressão (ver abaixo).

O quadro no topo diz se a ROI está disponível e, se não estiver, **por quê**.

### Experimento
1. Marque uma ou mais **condições** (seção 4). **Cada condição marcada vira um relatório
   completo.**
2. **Repetições**: quantas vezes cada condição é gerada. Use 1 para só obter o relatório;
   use 3 ou mais para medir tempos e tirar média.
3. **Coletor CSV** (opcional): grava a cada segundo CPU, memória, rede, ping até o PC e
   perdas da fila de rede, do início do processamento até o fim do download.
4. **Gerar relatórios**: a página mostra quantos relatórios serão gerados
   (condições × repetições) e a lista acompanha cada um.

Só quer **um** relatório com ROI? Marque uma condição `roi:` e deixe 1 repetição.

### Fila
Todos os relatórios pedidos desde que o servidor ligou: na fila → processando → pronto,
com tamanho, tempo de processamento, botão **Baixar** e o **CSV** do coletor. "tirar"
remove da fila um que ainda não começou.

### Relatórios salvos
Todos os ZIPs já gerados (ficam em `~/relatorios_gerados` no robô), com data, condição e
tamanho. Se um download cair, use "Retomar" no gerenciador de downloads do navegador: ele
continua de onde parou.

### Nome do ZIP
Começa pela condição, para dar para identificar na lista de downloads do navegador:

| Condição | Nome |
|---|---|
| `600/35` | `600x600_<inspeção>_<AAAAmmdd-HHMMSS>.zip` |
| `roi:600/35` | `600x600-ROI_<inspeção>_<AAAAmmdd-HHMMSS>.zip` |
| `original` | `original_<inspeção>_<AAAAmmdd-HHMMSS>.zip` |

A data e a hora são as do pedido. A qualidade só aparece quando foge do par do artigo
(600/35, 800/45, 1200/70, 1600/85), por exemplo `600x600-q50-ROI`. Com mais de uma
repetição, o nome termina em `_r1`, `_r2`…

### O que vem no ZIP
Tudo fica dentro de **uma pasta com o nome da condição**, então extrair vários ZIPs no
mesmo lugar não mistura nem sobrescreve nada:

```
600x600-ROI_2026-05-20/
├── Relatorio_600x600-ROI_2026-05-20.pdf   relatório traduzido, com as fotos comprimidas
├── mapa_imagens.csv                        cada foto: nome original, tamanho antes/depois, resolução, ROI
└── imagens/                                fotos separadas, renomeadas pelo equipamento e horário
    ├── Visor_de_oleo_15-10-07.jpg
    └── Termica_Motor_15-20-30.jpg
```

O `mapa_imagens.csv` abre no Excel e já responde "quanto cada foto tinha e quanto ficou".

### Tradução
As mensagens do `report.xml` são traduzidas por um **dicionário embutido**
(`processador_relatorio.py`) e por um **cache** (`cache_traducoes.json`). O que não estiver
neles fica em inglês. Para consultar o Google Tradutor nas mensagens novas (o robô precisa
de internet), ligue com:

```bash
RELATORIO_TRADUCAO_ONLINE=1 bash iniciar.sh
```

O que for traduzido online vai para o cache e depois funciona sem internet.

## 4. Compressão: condições e ROI

### 4.1 Condições

Uma **condição** diz como as fotos são comprimidas. Ela é escrita como texto curto:

| Condição | O que faz |
|---|---|
| `original` | fotos sem recompressão (o maior pacote) |
| `1200/70` | **uniforme**: toda foto reduzida para caber em 1200×1200 px e salva em JPEG qualidade 70 |
| `roi:1200/70` | **ROI**: a peça marcada fica em 1200/70 e o resto da foto é borrado; nas térmicas, a escala fica nítida; fotos sem marcação saem iguais à `1200/70` |
| `roi:1600/80/400/30` | ROI com todos os parâmetros: lado / qualidade / resolução do fundo / qualidade do fundo |
| `roi:1200/70+pol` | força a proteção pelo contorno da peça |
| `roi:1200/70+circ` | força a proteção por círculo |

O primeiro número é o **lado máximo em pixels** (600, 800, 1200, 1600) e o segundo é a
**qualidade JPEG** (35, 45, 70, 85): quanto maiores, melhor a foto e maior o arquivo.
As quatro do artigo são 600/35, 800/45, 1200/70 e 1600/85.

Comparar `1200/70` com `roi:1200/70` isola o efeito da ROI: a peça e as fotos sem marcação
saem idênticas, e só o fundo das fotos marcadas muda.

### 4.2 Como a ROI funciona

1. A foto é reduzida para o lado `L`; essa é a resolução da peça.
2. Em volta de cada marcação vai uma **área protegida**, que fica 100% nítida:
   * peças compactas (parafusos, visor, sílica): o **menor círculo** que contém a peça,
     com 15% de folga;
   * peças alongadas (como a escala térmica, quando o círculo teria mais de 2,5× a área
     da peça): o **próprio contorno**, com uma pequena margem.
3. O resto da foto vira **fundo**: é reduzido para `L/4` e ampliado de volta (fica
   borrado), depois passa por um JPEG de qualidade 30 alinhado aos blocos do JPEG final.
   Assim o fundo quase não ocupa bytes.
4. Uma borda suave liga a peça ao fundo, e sai **um JPEG comum**: o PDF não muda nada.

### 4.3 Térmicas
Toda foto **1158×768** (câmera térmica) tem a **escala de temperatura** (min/max/spot e
a coluna de números, na faixa da direita) protegida automaticamente, **mesmo sem marcação
e em inspeções novas**. A câmera desenha essa faixa sempre no mesmo lugar: x de 0,86 a 1,0
nas 25 térmicas marcadas. Para desligar: `RELATORIO_ROI_TERMICA=0`.

### 4.4 Marcações do Roboflow
Ficam em [`anotacoes_roi/_annotations.coco.json`](anotacoes_roi/): export **v2** do
projeto "ANYmal ROI", com 99 fotos RGB e 25 térmicas da inspeção de **2026-05-20**. O
arquivo tem só nomes de arquivo e coordenadas, sem fotos.

A ligação marcação ↔ foto é **pelo nome do arquivo**, então as marcações das RGB valem só
para as fotos que foram marcadas. Em inspeções novas, as RGB saem com compressão uniforme
(as térmicas continuam com a escala protegida).

**Para atualizar as marcações:** no Roboflow, *Versions → Create New Version*, com só
*Auto-Orient*, **sem Resize** e **sem Augmentation**; depois *Download Dataset → COCO →
Download zip*. Substitua o `_annotations.coco.json` pelo novo e confira:

```bash
venv/bin/python3 compressor_roi.py verificar --anotacoes anotacoes_roi/ \
    --imagens /home/integration/.ros/reports/<inspeção>/resources --saida conferencia/
```

Ele informa quantas fotos casaram ("com ROI: 122 …") e salva em `conferencia/` cada foto
com a marcação (amarelo), a área protegida (ciano) e o resto escurecido.

## 5. Resultados preliminares

Métricas contra as cópias do Roboflow (~470 KB por foto), **não** contra as originais de
2,6 MB. Para o artigo, refaça com `avaliar_roi.py` sobre as originais (seção 6.5).

**Fotos RGB (95 fotos, métricas dentro da peça):**

| Nível | Uniforme (KB/foto) | ROI (KB/foto) | Economia | SSIM na peça |
|---|---|---|---|---|
| 600/35 | 12,5 | 8,1 | −35% | 0,661 |
| 800/45 | 23,3 | 14,2 | −39% | 0,713 |
| 1200/70 | 69,7 | 31,4 | −55% | 0,842 |
| 1600/85 | 173,8 | 61,7 | −64% | 0,931 |

**Térmicas (25 fotos, métricas dentro da faixa da escala):**

| Nível | Uniforme (KB/foto) | ROI (KB/foto) | PSNR na escala |
|---|---|---|---|
| 600/35 | 14,3 | 12,3 | 17,7 dB |
| 800/45 | 24,8 | 21,1 | 19,2 dB |
| 1200/70 | 62,2 | 45,4 | 36,9 dB |
| 1600/85 | 74,0 | 54,5 | 39,9 dB |

* A peça fica **idêntica** à da uniforme do mesmo nível; só o fundo muda.
* A ROI rende mais nos níveis altos: `roi:1600/85` fica **menor que a uniforme 1200/70**,
  com a peça no nível da 1600/85.
* Em 600/35 e 800/45 a peça já sai degradada (a inspetora deu 3,3 e 4,0), e a economia em
  KB é pequena.
* Nas térmicas, em 600/35 e 800/45 os números ficam serrilhados por causa da **redução**
  (1158 → 600 px), não da compressão. A partir de 1200 a térmica não é reduzida e a escala
  fica igual à original.

**Matriz sugerida para o artigo** (×3 repetições): `original 600/35 1200/70 roi:1200/70 roi:1600/85`.

## 6. Experimentos do artigo

### 6.1 Limitar a rede

Substitui os comandos `tc` digitados à mão:

```bash
sudo bash rede/limitar.sh artigo                # igual ao artigo: limita TUDO que sai da interface
sudo bash rede/limitar.sh destino <IP_DO_PC>    # limita só o que vai para o PC (como o link da planta)
sudo bash rede/limitar.sh status                # mostra o que está aplicado
sudo bash rede/limitar.sh desligar              # remove o limite
```

* `artigo` aplica exatamente o que foi usado no artigo: `tbf rate 3mbit burst 32kbit
  latency 400ms` + `netem delay 167ms limit 140` na raiz da `enp0s31f6`.
* `destino` aplica 3 Mbit/s + 167 ms **só ao tráfego para os IPs informados** (vírgula
  separa vários) e deixa o resto livre.
* Variáveis: `IF=enp0s31f6 TAXA=3mbit ATRASO=167ms FILA=1000`.

### 6.2 Diagnóstico: por que a vazão cai para ~0,06 Mbit/s

No artigo, a entrega ficou entre 44 e 58 vezes abaixo dos 3 Mbit/s. A hipótese principal é
que o modo `artigo` limita também o **tráfego interno do robô** (LiDAR, câmeras, ROS), que
na planta não passaria pelo link de 3 Mbit/s. Esse tráfego enche a fila de 140 pacotes, os
pacotes do relatório se perdem e o TCP cai para cerca de um pacote por ida e volta:
1448 B / 0,17 s ≈ 0,07 Mbit/s.

O diagnóstico faz o mesmo envio TCP (iperf3, robô → PC, 20 s) mudando **um fator por vez**:

| Configuração | O que responde |
|---|---|
| sem limite | quanto o Wi-Fi entrega sozinho |
| artigo | reproduz o problema |
| só taxa (tbf) | o limite de 3 Mbit/s sozinho derruba? |
| só atraso (netem) | os 167 ms sozinhos derrubam? |
| artigo com fila 1000 | a fila de 140 é a culpada? |
| artigo sem TSO/GSO | o agrupamento de pacotes da placa atrapalha? |
| só para o PC (`destino`) | limitar só o tráfego que iria para a planta resolve? |

Ele também mede, sem download, **quanto tráfego sai do robô e para quais IPs**.

No **PC** (Linux), deixe aberto:

```bash
sudo apt install -y iperf3
iperf3 -s
```

No **robô** (~6 min):

```bash
cd ~/Documents/anymalnew
sudo bash rede/diagnostico_rede.sh <IP_DO_PC>
```

* Instala sozinho o que faltar (iperf3, tcpdump).
* Ao terminar, ou com `Ctrl+C`, **remove o limite** e religa o TSO/GSO.
* Se avisar que o tráfego para o PC não sai pela `enp0s31f6`, rode com
  `sudo IF=<interface> bash rede/diagnostico_rede.sh <IP_DO_PC>`.
* Gera `rede/diagnostico_<data>/` com os dados brutos e um **`resumo.md`** com as tabelas
  e uma leitura automática do que causou a queda.

### 6.3 Medir tempos de entrega a partir do PC

O `experimento_pc.py` roda no PC (VS Code) e não precisa de bibliotecas extras. Para cada
condição e repetição, ele:

1. pede o relatório ao robô, com o coletor ligado;
2. espera o processamento (o robô informa o tempo de cada etapa);
3. baixa o ZIP medindo o tempo até o primeiro byte e o tempo de transferência;
4. baixa os arquivos do coletor e **só então** pede o próximo, para nenhum download disputar
   o link com outro.

```powershell
python experimento_pc.py --robo http://192.168.0.169:5051 `
    --missao /home/integration/.ros/reports/2026-05-20 `
    --condicoes original 600/35 1200/70 roi:1200/70 roi:1600/85 `
    --repeticoes 3 --rotulo "limite destino" --saida experimento_01
```

(No Linux, troque a crase do fim de linha por `\`.)

* Por padrão, dentro de cada repetição as condições rodam em **ordem aleatória**. Isso
  evita que uma piora da rede ao longo do tempo (Wi-Fi, outro usuário) caia sempre na
  mesma condição. `--semente 42` torna a ordem reproduzível; `--sem-embaralhar` usa sempre
  a ordem digitada.
* `--simular` só mostra a ordem das execuções; `--salvar-zips` guarda os ZIPs e confere se
  abrem.
* Saídas em `--saida`:
  * `medicoes.csv`: uma linha por relatório, com tamanho, tempo de processamento (total e
    por etapa), tempo até o 1º byte, tempo de transferência, taxa, perda de ping e
    descartes da fila do `tc`;
  * `resumo.csv`: média, desvio, mínimo e máximo por condição;
  * `coletor/`: o CSV de 1 Hz de cada relatório, mais `tc -s qdisc` antes/depois e um
    resumo.

### 6.4 Coletor sozinho (tráfego de fundo)

Para medir o robô parado, sem relatório (ex.: 30 s deitado):

```bash
venv/bin/python3 coletor.py --segundos 30 --ping <IP_DO_PC> --saida fundo_deitado.csv
```

Colunas: `timestamp, fase, cpu_percent, ram_used_MB, ram_percent, rx_Mbps, tx_Mbps,
tx_pacotes_s, tx_descartes, ping_ms, ping_ok, disk_read_MBps, disk_write_MBps`.

### 6.5 Qualidade das imagens

No PC, com as fotos **originais** da inspeção:

```bash
python -m pip install -r requirements-pc.txt
python avaliar_roi.py --imagens <pasta com as originais> --anotacoes anotacoes_roi/ \
    --saida resultados_roi --workers 4
```

Compara as 4 uniformes do artigo com a ROI nos mesmos 4 níveis. Calcula PSNR e SSIM na foto
inteira, **só na peça** e **só no fundo** (em cinza, com a foto ampliada de volta ao
tamanho original por Lanczos, como no artigo), e o **ΔE00** (diferença de cor) na peça.
Gera `resumo.md`/`resumo.csv`, `fig_compromisso.png` (tamanho × SSIM na peça), `recortes/`
(a mesma peça em cada condição) e exemplos de foto inteira com ROI.

### 6.6 Comparar os relatórios baixados com as originais

Depois de baixar os ZIPs e copiar a inspeção original do robô, no PC:

```bash
# fotos originais da inspeção (uma vez)
scp -r integration@anymal-d021-npc:/home/integration/.ros/reports/2026-05-20 ~/inspecoes_anymal/

python -m pip install -r requirements-pc.txt
python comparar_imagens.py --originais ~/inspecoes_anymal/2026-05-20 --zips ~/Downloads
```

Ele lê os ZIPs direto (não precisa extrair), separa os da mesma inspeção e, se a mesma
condição foi baixada mais de uma vez, usa a mais recente (`--todos` mantém todas). Gera em
`comparacao_<inspeção>/`:

* `imagens.csv`: cada foto em cada condição: tamanho antes e depois, redução (%) e
  "vezes menor", resolução antes e depois, PSNR e SSIM na foto inteira e na peça;
* `resumo.csv` / `resumo.md`: por condição: total original e comprimido, redução, KB por
  foto, tamanho do ZIP e do PDF, SSIM e PSNR médios;
* `comparacoes/`: para 8 fotos (`--exemplos N`), a foto inteira em HD (original + cada
  condição, com KB e SSIM na legenda) e um `_detalhe.jpg` com o zoom na peça.

Os CSVs saem com `;` e vírgula decimal, para abrir direto no Excel em português. Nos ZIPs
antigos, sem `mapa_imagens.csv`, o script reconhece cada foto pela imagem. As métricas
levam alguns minutos para 169 fotos; `--rapido` calcula em meia resolução e
`--sem-metricas` faz só os tamanhos, em segundos.

## 7. Arquivos do projeto

| Arquivo | Onde roda | Para que serve |
|---|---|---|
| `servidor_web.py` | robô | o site: inspeções, fila, downloads, coletor por relatório |
| `processador_relatorio.py` | robô | gera o relatório: tradução, compressão, PDF e ZIP |
| `compressor_roi.py` | robô e PC | compressão por ROI; também `verificar` e `comprimir` pela linha de comando |
| `configuracoes.py` | robô e PC | traduz `1200/70`, `roi:1200/70` etc. em parâmetros |
| `coletor.py` | robô | coletor de 1 Hz (usado pelo servidor ou sozinho) |
| `instalar.sh` / `iniciar.sh` | robô | instalação com um comando / ligar o servidor |
| `rede/limitar.sh` | robô | aplica, mostra e remove o limite de rede |
| `rede/diagnostico_rede.sh` | robô | diagnóstico da queda de vazão (seção 6.2) |
| `rede/analisar_diagnostico.py` | robô ou PC | resume a pasta do diagnóstico |
| `experimento_pc.py` | PC | matriz de experimentos e tempos de entrega |
| `avaliar_roi.py` | PC | métricas de qualidade de cada condição, recomprimindo as originais |
| `comparar_imagens.py` | PC | compara os ZIPs baixados com as originais (tamanhos, métricas, figuras HD) |
| `requirements.txt` | robô | bibliotecas do servidor |
| `requirements-pc.txt` | PC | bibliotecas da avaliação e dos testes |

| Pasta | Conteúdo |
|---|---|
| `templates/` | as páginas (HTML) |
| `static/` | logos do ITV, da Vale e do ANYmal |
| `anotacoes_roi/` | marcações do Roboflow |
| `rede/` | scripts de rede |
| `tests/` | testes automáticos (não são usados no robô) |

No robô, o servidor grava em `~/relatorios_gerados/`: os ZIPs, o
`registro_experimentos.csv`, o `servidor.log` e a pasta `coletor/`.

## 8. Configurações

Variáveis de ambiente, usadas na frente do comando (ex.: `SERVIDOR_PORTA=8085 bash iniciar.sh`):

| Variável | Padrão | Para que serve |
|---|---|---|
| `SERVIDOR_PORTA` | `5051` | porta do site |
| `RELATORIO_CONDICAO` | `roi:1600/80` | condição do botão "Gerar relatório" (ex.: `roi:1200/70`) |
| `RELATORIO_LADO` / `RELATORIO_QUALIDADE` | `800` / `45` | fotos sem marcação na condição padrão |
| `RELATORIO_ROI` | `1` | `0` desliga a ROI |
| `RELATORIO_ROI_ANOTACOES` | `anotacoes_roi/` | outro arquivo/pasta de marcações |
| `RELATORIO_ROI_TERMICA` | `1` | `0` desliga a escala protegida nas térmicas |
| `RELATORIO_ROI_MODO` | `auto` | `circulo` ou `poligono` forçam a forma |
| `RELATORIO_IMAGENS_SEPARADAS` | `1` | `0`: o ZIP leva só o PDF |
| `RELATORIO_TRADUCAO_ONLINE` | `0` | `1` consulta o Google Tradutor |
| `COLETOR_INTERFACE` | rota padrão | interface medida pelo coletor (ex.: `enp0s31f6`) |
| `COLETOR_ESPERA_DOWNLOAD_S` | `1800` | o coletor para se ninguém baixar o relatório nesse tempo |

## 9. Problemas comuns

| Sintoma | Solução |
|---|---|
| **"ROI desligada"** na página | o motivo aparece ao lado. Normalmente falta biblioteca: rode `bash instalar.sh` |
| `error: externally-managed-environment` no `pip` | o `pip` está fora do venv. Use `bash instalar.sh`, que cria e usa o `venv/` da pasta |
| `ensurepip is not available` | `sudo apt install -y python3.12-venv` e rode `bash instalar.sh` de novo |
| a página não mostra as mudanças do `git pull` | reinicie o servidor (`Ctrl+C` e `bash iniciar.sh`) |
| porta em uso | veja quem usa: `sudo ss -ltnp \| grep 5051`; ou use `SERVIDOR_PORTA=5052 bash iniciar.sh` |
| `verificar` mostra 0 fotos com marcação | os nomes das fotos no robô não batem com os do Roboflow; confira os nomes em `resources/` |
| a rede continua lenta depois do teste | o limite ficou aplicado: `sudo bash rede/limitar.sh desligar` |
| `apt-get update` demora | normal no ANYmal (muitos repositórios); se pedir senha, os caracteres não aparecem |

## 10. API e testes

**API** (usada pelo `experimento_pc.py`; útil para scripts próprios):

| Rota | |
|---|---|
| `POST /api/fila` | `{"missao", "condicoes": [...], "repeticoes", "embaralhar", "coletor", "rotulo"}` → `{"jobs": [...]}` |
| `GET /api/fila` | estado de todos os relatórios |
| `GET /api/job/<id>` | estado, tempos por etapa, `url_download` |
| `POST /api/job/<id>/cancelar` | tira da fila |
| `GET /api/coletor/arquivos/<id>` · `GET /coletor/<arquivo>` | arquivos do coletor de um relatório |
| `POST /api/coletor/iniciar` · `POST /api/coletor/parar` | coletor manual |

**Testes** (no PC; não precisam do WeasyPrint nem do robô):

```bash
python -m pip install -r requirements-pc.txt
python -m pytest -q tests/
```
