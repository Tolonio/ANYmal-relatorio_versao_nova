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

```bash
cd Downloads/
git clone https://github.com/Tolonio/ANYmal-relatorio_versao_nova.git
scp -r ./ANYmal-relatorio_versao_nova/* integration@anymal-<nomedoanymal>-npc:/home/integration/ANYmal-relatorio_versao_nova
ssh integration@anymal-<nomedoanymal>-npc
cd /home/integration/ANYmal-relatorio_versao_nova
python3 -m venv venv
source venv/bin/activate
sudo apt update
sudo apt install -y libcairo2 libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 shared-mime-info
python3 -m pip install flask lxml weasyprint pillow numpy
python3 servidor_web.py
```

(`deep-translator` só é necessário com `RELATORIO_TRADUCAO_ONLINE=1`.)

**Anotações de ROI:** já vêm no repositório, em `anotacoes_roi/_annotations.coco.json`
(export v2 do Roboflow: 99 fotos RGB e 25 térmicas da missão de 2026-05-20; só nomes de
arquivo e coordenadas, sem fotos). Para trocar, exporte do Roboflow em **COCO**, sem
augmentation e sem Resize, e substitua esse arquivo. A ligação marcação ↔ foto das RGB
é pelo nome do arquivo. Confira antes de medir:

```bash
python3 compressor_roi.py verificar --anotacoes anotacoes_roi/ --imagens <pasta resources> --saida conferencia/
```

**Variáveis úteis:**
`RELATORIO_CONDICAO=roi:1200/70` (condição do botão "Gerar relatório"),
`RELATORIO_ROI_ANOTACOES=/caminho`, `COLETOR_INTERFACE=enp0s31f6` (padrão: a interface da rota padrão),
`COLETOR_ESPERA_DOWNLOAD_S=1800`.

### Serviço (opcional)

```
sudo nano /etc/systemd/system/anymal_relatorio_v2.service
```

```
[Unit]
Description=Servidor Web do Tradutor ANYmal
After=network.target

[Service]
Type=simple
User=integration
Group=integration
WorkingDirectory=/home/integration/ANYmal-relatorio_versao_nova
Environment="PATH=/home/integration/ANYmal-relatorio_versao_nova/venv/bin:/usr/bin:/usr/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/integration/ANYmal-relatorio_versao_nova/venv/bin/python3 /home/integration/ANYmal-relatorio_versao_nova/servidor_web.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now anymal_relatorio_v2.service
sudo systemctl status anymal_relatorio_v2.service
```

O `PATH` do serviço inclui `/usr/sbin` e `/bin` para o coletor achar `tc` e `ping`.

---

## Interface

* **Missões**: "Gerar relatório" usa a condição padrão; "Experimento" abre a fila com
  as condições do artigo marcáveis, repetições, ordem aleatória e coletor.
* **Fila**: o estado de cada relatório (na fila / processando / pronto), download e
  link do CSV do coletor.
* **Relatórios salvos**: todos os ZIPs em `~/relatorios_gerados`.

## Rodar a matriz de experimentos do PC (VS Code)

No PC ligado à rede do robô (com o `tc` aplicado no robô):

```powershell
python experimento_pc.py --robo http://<IP_DO_ROBO>:5050 `
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
