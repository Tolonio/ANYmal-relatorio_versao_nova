"""
Avaliação para o artigo: compressão UNIFORME (L/Q do artigo) x compressão por ROI.

Para cada foto anotada e cada configuração mede:
  * tamanho do JPEG (KB) e tempo de codificação (s)
  * PSNR e SSIM na foto inteira       (mesma metodologia do artigo: ampliada de
                                       volta ao tamanho original com Lanczos, em cinza)
  * PSNR e SSIM DENTRO da peça anotada (polígono do Roboflow)  <- o que o inspetor usa
  * PSNR e SSIM no FUNDO (fora da peça)
  * ΔE00 médio dentro da peça (fidelidade de COR, ex.: tom da sílica)

Saídas em --saida:
  por_imagem.csv      uma linha por (foto, configuração)
  resumo.csv          médias por configuração + projeção do tempo de entrega
  resumo.md           a mesma tabela em Markdown
  fig_compromisso.png tamanho por foto x SSIM na peça
  recortes/*.png      a mesma peça em todas as configurações (estilo Fig. 4 do artigo)
  exemplo_roi_*.png   foto inteira com ROI (mostra o fundo suavizado)

Exemplo:
  python avaliar_roi.py --imagens fotos_missao_169/ --anotacoes anotacoes_roi/ \
      --saida resultados_roi/ --workers 4

Por padrão compara as 4 uniformes do artigo (600/35 ... 1600/85) com a ROI nos
MESMOS 4 níveis (roi:600/35 ... roi:1600/85): a peça fica igual, só o fundo muda.

Dependências (no PC, não no robô): numpy, Pillow, scikit-image, matplotlib.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from PIL import Image

import compressor_roi as cr

UNIFORMES_PADRAO = "600/35,800/45,1200/70,1600/85"
ROI_PADRAO = "600/35,800/45,1200/70,1600/85"


# ============================================================
# CONFIGURAÇÕES
# ============================================================

def ler_configs(uniformes: str, rois: str, modo: str, margem: float):
    """
    uniformes: "600/35,800/45"           rois: "600/35,1600/85/400/30,1200/70+pol"
    A ROI 'L/Q' usa fundo L/4 com pré-quantização Q30 (igual ao robô: configuracoes.py).
    """
    import configuracoes
    configs = []
    for item in filter(None, (x.strip() for x in uniformes.split(","))):
        c = configuracoes.ler_condicao(item)
        configs.append({"nome": c["condicao"], "tipo": "uniforme", "lado": c["lado"], "qualidade": c["qualidade"]})
    for item in filter(None, (x.strip() for x in rois.split(","))):
        item = item.replace(":poligono", "+pol").replace(":circulo", "+circ")
        if "+" not in item and modo != "auto":
            item += {"poligono": "+pol", "circulo": "+circ"}[modo]
        c = configuracoes.ler_condicao("roi:" + item)
        configs.append({"nome": c["condicao"], "tipo": "roi", "lado": c["roi_lado"], "qualidade": c["roi_qualidade"],
                        "fundo": c["roi_fundo"], "qualidade_fundo": c["roi_qualidade_fundo"],
                        "modo": c["roi_modo"], "margem": margem})
    return configs


def comprimir(caminho, regioes, cfg):
    """Exatamente o caminho usado no robô (abrir com draft + comprimir)."""
    t0 = time.perf_counter()
    img = cr.abrir_imagem(caminho, cfg["lado"])
    if cfg["tipo"] == "uniforme":
        dados = cr.comprimir_uniforme(img, cfg["lado"], cfg["qualidade"])
    else:
        dados, _ = cr.comprimir_roi(img, regioes, cfg["lado"], cfg["qualidade"], cfg["fundo"],
                                    cfg["qualidade_fundo"], modo=cfg["modo"], margem=cfg["margem"])
    return dados, time.perf_counter() - t0


# ============================================================
# MÉTRICAS
# ============================================================

def psnr(erro2):
    mse = float(erro2.mean()) if erro2.size else 0.0
    return float("inf") if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def avaliar_imagem(args):
    caminho, regioes, configs, saida_recortes = args
    from skimage.color import deltaE_ciede2000, rgb2lab
    from skimage.metrics import structural_similarity

    original = cr.abrir_imagem(caminho)
    W, H = original.size
    cinza_o = np.asarray(original.convert("L"), dtype=np.float64)
    peca = np.asarray(cr.mascara_poligonos(regioes, W, H)) > 127
    fundo = ~peca

    # recorte em volta da maior peça, para ΔE00 e para as figuras
    ys, xs = np.nonzero(peca)
    if ys.size == 0:
        return [], None
    pad = int(0.04 * min(W, H))
    y0, y1 = max(0, ys.min() - pad), min(H, ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(W, xs.max() + pad + 1)
    lab_o = rgb2lab(np.asarray(original)[y0:y1, x0:x1])
    peca_rec = peca[y0:y1, x0:x1]

    linhas, recortes = [], {}
    for cfg in configs:
        dados, t_cod = comprimir(caminho, regioes, cfg)
        comp = Image.open(io.BytesIO(dados)).convert("RGB")
        recortes[cfg["nome"]] = (comp, comp.size)
        ampliada = comp.resize((W, H), cr.LANCZOS)
        cinza_c = np.asarray(ampliada.convert("L"), dtype=np.float64)

        _, mapa = structural_similarity(cinza_o, cinza_c, data_range=255, full=True)
        e2 = (cinza_o - cinza_c) ** 2
        de = deltaE_ciede2000(lab_o, rgb2lab(np.asarray(ampliada)[y0:y1, x0:x1]))

        linhas.append({
            "imagem": os.path.basename(caminho), "config": cfg["nome"], "tipo": cfg["tipo"],
            "tamanho_kb": len(dados) / 1024, "t_codificacao_s": t_cod,
            "largura": comp.size[0], "altura": comp.size[1],
            "fracao_peca": float(peca.mean()),
            "psnr_total": psnr(e2), "ssim_total": float(mapa.mean()),
            "psnr_peca": psnr(e2[peca]), "ssim_peca": float(mapa[peca].mean()),
            "psnr_fundo": psnr(e2[fundo]), "ssim_fundo": float(mapa[fundo].mean()),
            "de00_peca": float(de[peca_rec].mean()),
        })

    if saida_recortes:
        salvar_recortes(caminho, original, recortes, caixa_maior_peca(regioes, W, H), configs, saida_recortes)
    return linhas, None


# ============================================================
# FIGURAS
# ============================================================

def caixa_maior_peca(regioes, W, H, folga=0.6):
    """Caixa em volta da MAIOR peça anotada (com folga), para o recorte da figura."""
    def area(pol):
        return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pol, pol[1:] + pol[:1]))) / 2
    pol = max(regioes, key=lambda r: area(r["poligono"]))["poligono"]
    xs, ys = [x * W for x, _ in pol], [y * H for _, y in pol]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    meio = max(max(xs) - min(xs), max(ys) - min(ys), 0.03 * min(W, H)) * (1 + folga) / 2
    return (int(max(0, cx - meio)), int(max(0, cy - meio)), int(min(W, cx + meio)), int(min(H, cy + meio)))


def salvar_recortes(caminho, original, recortes, caixa, configs, pasta):
    """Mesma região em cada configuração, na resolução dela, ampliada sem interpolação."""
    from PIL import ImageDraw
    W, H = original.size
    x0, y0, x1, y1 = caixa
    tam = 320
    escala = tam / max(x1 - x0, y1 - y0)
    cw, ch = int((x1 - x0) * escala), int((y1 - y0) * escala)
    nomes = ["original"] + [c["nome"] for c in configs]
    painel = Image.new("RGB", (len(nomes) * (cw + 8) + 8, ch + 40), "white")
    d = ImageDraw.Draw(painel)
    for i, nome in enumerate(nomes):
        if nome == "original":
            rec = original.crop(caixa)
        else:
            img, (w, h) = recortes[nome]
            fx, fy = w / W, h / H
            rec = img.crop((int(x0 * fx), int(y0 * fy), int(x1 * fx), int(y1 * fy)))
        rec = rec.resize((cw, ch), cr._RS.NEAREST)
        painel.paste(rec, (8 + i * (cw + 8), 8))
        d.text((8 + i * (cw + 8), ch + 14), nome, fill=(30, 30, 30))
    os.makedirs(pasta, exist_ok=True)
    painel.save(os.path.join(pasta, os.path.splitext(os.path.basename(caminho))[0] + ".png"))


def figura_compromisso(resumo, destino):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    estilos = {"uniforme": ("#2a78d6", "o", "Uniforme (L/Q)"), "roi": ("#eb6834", "s", "ROI")}
    fig, ax = plt.subplots(figsize=(3.5, 2.6), dpi=200)     # 1 coluna IEEE
    for tipo, (cor, marca, rotulo) in estilos.items():
        pts = sorted((r["kb_medio"], r["ssim_peca"], r["config"]) for r in resumo if r["tipo"] == tipo)
        if not pts:
            continue
        ax.plot([p[0] for p in pts], [p[1] for p in pts], color=cor, lw=1.5, marker=marca,
                ms=5, mec="white", mew=1, label=rotulo, zorder=3)
        for i, (x, y, nome) in enumerate(pts):
            rot = nome.replace("roi:", "").replace("+pol", " pol").replace("+circ", " circ") \
                + ("*" if nome.endswith("/0") else "")
            if tipo == "roi":   # acima/abaixo alternado para não encavalar
                xy, ha = ((0, 6) if i % 2 == 0 else (0, -11)), "center"
            else:
                xy, ha = (5, -8), "left"
            ax.annotate(rot, (x, y), xytext=xy, textcoords="offset points", ha=ha,
                        fontsize=5.5, color="#444")
    ax.set_xscale("log")
    from matplotlib.ticker import FixedLocator, NullFormatter, ScalarFormatter
    xs = [r["kb_medio"] for r in resumo]
    marcas = [v for v in (5, 10, 20, 50, 100, 200, 500) if min(xs) / 1.5 <= v <= max(xs) * 1.5]
    ax.xaxis.set_major_locator(FixedLocator(marcas))
    ax.xaxis.set_major_formatter(ScalarFormatter())
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel("Tamanho médio por foto (KB, escala log)", fontsize=7)
    ax.set_ylabel("SSIM na peça anotada", fontsize=7)
    ax.tick_params(labelsize=6, color="#999")
    ax.grid(True, which="major", color="#e5e5e5", lw=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#999")
    ax.legend(fontsize=6, frameon=False, loc="lower right")
    if any(r["config"].endswith("/0") for r in resumo):
        ax.text(0.99, 0.02, "* sem pré-quantização do fundo", transform=ax.transAxes,
                fontsize=5, color="#666", ha="right", va="bottom")
        ax.legend(fontsize=6, frameon=False, loc="lower right", bbox_to_anchor=(1, 0.06))
    fig.tight_layout()
    fig.savefig(destino)
    plt.close(fig)


def exemplo_roi(caminho, regioes, cfg, destino):
    """Foto inteira comprimida com ROI + versão com a máscara destacada."""
    dados, _ = comprimir(caminho, regioes, cfg)
    comp = Image.open(io.BytesIO(dados)).convert("RGB")
    conf = cr.desenhar_conferencia(cr.abrir_imagem(caminho, cfg["lado"]), regioes, comp.size[0],
                                   modo=cfg["modo"], margem=cfg["margem"])
    w, h = comp.size
    painel = Image.new("RGB", (w * 2 + 10, h), "white")
    painel.paste(comp, (0, 0))
    painel.paste(conf.resize((w, h)), (w + 10, 0))
    painel.thumbnail((2400, 2400))
    painel.save(destino)


# ============================================================
# RESUMO
# ============================================================

def resumir(linhas, configs, n_fotos, copias, taxa_efetiva, taxa_link):
    resumo = []
    for cfg in configs:
        ls = [l for l in linhas if l["config"] == cfg["nome"]]
        if not ls:
            continue

        def media(k):
            v = np.array([l[k] for l in ls], dtype=float)
            v = v[np.isfinite(v)]
            return float(v.mean()) if v.size else float("nan")

        kb = media("tamanho_kb")
        mb_total = kb * n_fotos * copias / 1024
        resumo.append({
            "config": cfg["nome"], "tipo": cfg["tipo"], "n": len(ls),
            "kb_medio": kb, "kb_mediana": float(np.median([l["tamanho_kb"] for l in ls])),
            "psnr_total": media("psnr_total"), "ssim_total": media("ssim_total"),
            "psnr_peca": media("psnr_peca"), "ssim_peca": media("ssim_peca"),
            "psnr_fundo": media("psnr_fundo"), "ssim_fundo": media("ssim_fundo"),
            "de00_peca": media("de00_peca"), "t_codificacao_s": media("t_codificacao_s"),
            "mb_fotos_missao": mb_total,
            "min_taxa_efetiva": mb_total * 8 * 1.048576 / taxa_efetiva / 60,
            "min_taxa_link": mb_total * 8 * 1.048576 / taxa_link / 60,
        })
    return resumo


def salvar_csv(linhas, destino):
    if not linhas:
        return
    with open(destino, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()))
        w.writeheader()
        for l in linhas:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in l.items()})


def salvar_md(resumo, destino, args):
    cab = ("| Config. | KB/foto | SSIM total | SSIM peça | SSIM fundo | PSNR peça (dB) | ΔE00 peça "
           f"| MB ({args.n_fotos} fotos ×{args.copias}) | min @ {args.taxa_efetiva} Mbit/s | min @ {args.taxa_link} Mbit/s |")
    linhas = [cab, "|" + "---|" * 10]
    for r in resumo:
        linhas.append(f"| {r['config']} | {r['kb_medio']:.1f} | {r['ssim_total']:.3f} | {r['ssim_peca']:.3f} "
                      f"| {r['ssim_fundo']:.3f} | {r['psnr_peca']:.1f} | {r['de00_peca']:.2f} "
                      f"| {r['mb_fotos_missao']:.2f} | {r['min_taxa_efetiva']:.1f} | {r['min_taxa_link']:.2f} |")
    linhas += ["", "Tempos projetados = só o volume das fotos dividido pela taxa (estimativa, não medição).",
               "SSIM/PSNR em cinza, com a foto comprimida ampliada ao tamanho original (Lanczos), como no artigo."]
    with open(destino, "w", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")
    return "\n".join(linhas)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--imagens", required=True)
    ap.add_argument("--anotacoes", required=True)
    ap.add_argument("--saida", required=True)
    ap.add_argument("--uniformes", default=UNIFORMES_PADRAO, help="lista L/Q")
    ap.add_argument("--roi", default=ROI_PADRAO, help="lista L/Q (fundo L/4, Q30) ou L/Q/F/QF; '+pol' usa o polígono")
    ap.add_argument("--modo", default="auto", choices=["auto", "circulo", "poligono"])
    ap.add_argument("--margem", type=float, default=cr.PADRAO["margem"])
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--limite", type=int, default=0, help="usa só as N primeiras fotos (teste rápido)")
    ap.add_argument("--recortes", type=int, default=12, help="quantas fotos geram figura de recorte")
    ap.add_argument("--n-fotos", type=int, default=141, help="fotos RGB da missão para a projeção")
    ap.add_argument("--copias", type=int, default=2, help="cada foto vai no PDF e no ZIP (=2)")
    ap.add_argument("--taxa-efetiva", type=float, default=0.06, help="Mbit/s medido no artigo")
    ap.add_argument("--taxa-link", type=float, default=3.0)
    args = ap.parse_args(argv)

    anot = cr.AnotacoesROI(args.anotacoes)
    print(anot.resumo())
    for a in anot.avisos:
        print("AVISO:", a)
    configs = ler_configs(args.uniformes, args.roi, args.modo, args.margem)
    fotos = [(p, anot.regioes_para(p)) for p in cr.listar_imagens(args.imagens)]
    fotos = [(p, r) for p, r in fotos if r]
    if args.limite:
        fotos = fotos[:args.limite]
    if not fotos:
        print("Nenhuma foto da pasta tem anotação. Rode 'compressor_roi.py verificar' para conferir os nomes.")
        return 1
    os.makedirs(args.saida, exist_ok=True)
    pasta_rec = os.path.join(args.saida, "recortes")
    tarefas = [(p, r, configs, pasta_rec if i < args.recortes else None) for i, (p, r) in enumerate(fotos)]

    print(f"{len(fotos)} fotos × {len(configs)} configurações, {args.workers} processos...")
    linhas, t0 = [], time.time()
    if args.workers > 1:
        with ProcessPoolExecutor(args.workers) as ex:
            for i, (ls, _) in enumerate(ex.map(avaliar_imagem, tarefas), 1):
                linhas += ls
                print(f"  {i}/{len(fotos)}  ({time.time() - t0:.0f} s)", end="\r")
    else:
        for i, t in enumerate(tarefas, 1):
            linhas += avaliar_imagem(t)[0]
            print(f"  {i}/{len(fotos)}  ({time.time() - t0:.0f} s)", end="\r")
    print()

    salvar_csv(linhas, os.path.join(args.saida, "por_imagem.csv"))
    resumo = resumir(linhas, configs, args.n_fotos, args.copias, args.taxa_efetiva, args.taxa_link)
    salvar_csv(resumo, os.path.join(args.saida, "resumo.csv"))
    print(salvar_md(resumo, os.path.join(args.saida, "resumo.md"), args))
    figura_compromisso(resumo, os.path.join(args.saida, "fig_compromisso.png"))
    roi_cfgs = [c for c in configs if c["tipo"] == "roi"]
    if roi_cfgs:
        for p, r in fotos[:3]:
            exemplo_roi(p, r, roi_cfgs[0], os.path.join(
                args.saida, "exemplo_roi_" + os.path.splitext(os.path.basename(p))[0] + ".png"))
    print("Resultados em", os.path.abspath(args.saida))
    return 0


if __name__ == "__main__":
    sys.exit(main())
