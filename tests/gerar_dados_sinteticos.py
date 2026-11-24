"""
Gera fotos sintéticas 3840×2160 parecidas com as do ANYmal (textura de ferrugem,
grade metálica, parafusos, visor) e uma exportação COCO no formato do Roboflow
(nomes '..._jpg.rf.<hash>.jpg', imagens 640×640 'Stretch').

Serve SÓ para testar o código. Os resultados do artigo devem vir das fotos reais.
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def ruido_fractal(h, w, rng, oitavas=6):
    total = np.zeros((h, w), np.float32)
    amp = 1.0
    for o in range(oitavas):
        s = 2 ** (o + 2)
        pequeno = rng.standard_normal((max(2, h * s // w), s)).astype(np.float32)
        img = Image.fromarray(pequeno).resize((w, h), Image.BICUBIC)
        total += amp * np.asarray(img)
        amp *= 0.6
    total -= total.min()
    return total / total.max()


def foto(seed, w=3840, h=2160):
    rng = np.random.default_rng(seed)
    cores = [(70, 110, 130), (200, 170, 60), (140, 80, 50), (90, 90, 95)]
    base = np.array(cores[seed % 4], np.float32)
    tex = ruido_fractal(h, w, rng)
    img = base[None, None, :] * (0.55 + 0.8 * tex[..., None])
    img += rng.normal(0, 6, (h, w, 3))           # ruído de sensor
    im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)

    # grade metálica (detalhe fino caro no JPEG)
    if seed % 2 == 0:
        x0, y0 = int(w * 0.05), int(h * 0.1)
        for i in range(0, int(w * 0.45), 22):
            d.line([(x0 + i, y0), (x0 + i, y0 + int(h * 0.8))], fill=(60, 50, 45), width=5)
        for j in range(0, int(h * 0.8), 22):
            d.line([(x0, y0 + j), (x0 + int(w * 0.45), y0 + j)], fill=(60, 50, 45), width=5)
    # tubos/estrutura
    for k in range(5):
        x = int(rng.uniform(0, w))
        d.rectangle([x, 0, x + int(rng.uniform(40, 160)), h], fill=tuple(int(c) for c in rng.uniform(40, 200, 3)))

    regioes = []
    # peça 1: visor de óleo (retângulo com marcas finas)
    cx, cy = int(w * rng.uniform(0.55, 0.8)), int(h * rng.uniform(0.3, 0.6))
    vw, vh = int(rng.uniform(60, 110)), int(rng.uniform(220, 380))
    d.rounded_rectangle([cx - vw, cy - vh // 2, cx + vw, cy + vh // 2], 20, fill=(30, 30, 30), outline=(220, 220, 220), width=6)
    nivel = cy + int(rng.uniform(-0.2, 0.3) * vh)
    d.rectangle([cx - vw + 12, nivel, cx + vw - 12, cy + vh // 2 - 12], fill=(170, 120, 20))
    for t in range(cy - vh // 2 + 20, cy + vh // 2 - 10, 18):
        d.line([(cx - vw + 12, t), (cx - vw + 40, t)], fill=(240, 240, 240), width=3)
    regioes.append([(cx - vw, cy - vh // 2), (cx + vw, cy - vh // 2), (cx + vw, cy + vh // 2), (cx - vw, cy + vh // 2)])

    # peças 2..n: parafusos em arco
    ax, ay, raio = int(w * 0.3), int(h * 0.65), int(h * 0.18)
    for a in np.linspace(0.2, 2.2, 6):
        bx, by = ax + raio * np.cos(a), ay + raio * np.sin(a)
        r = 26
        d.ellipse([bx - r, by - r, bx + r, by + r], fill=(150, 150, 150), outline=(40, 40, 40), width=4)
        d.regular_polygon((bx, by, 15), 6, fill=(110, 110, 110), outline=(30, 30, 30))
        regioes.append([(bx + r * np.cos(t), by + r * np.sin(t)) for t in np.linspace(0, 2 * np.pi, 12, endpoint=False)])

    im = im.filter(ImageFilter.UnsharpMask(2, 60, 2))
    return im, regioes


def main(destino):
    fotos = os.path.join(destino, "fotos")
    exp = os.path.join(destino, "roboflow_export", "train")
    os.makedirs(fotos, exist_ok=True)
    os.makedirs(exp, exist_ok=True)
    coco = {"images": [], "annotations": [], "categories": [{"id": 0, "name": "pecas"}, {"id": 1, "name": "peca"}]}
    aid = 1
    for i in range(6):
        nome = f"2026-05-20-15-{10 + i:02d}-07-000{i}"
        im, regs = foto(i)
        im.save(os.path.join(fotos, nome + ".jpg"), quality=92)
        W, H = im.size
        coco["images"].append({"id": i, "file_name": f"{nome}_jpg.rf.{i:08x}abcdef.jpg", "width": 640, "height": 640})
        for pol in regs:
            seg = []
            for x, y in pol:
                seg += [x * 640 / W, y * 640 / H]
            xs, ys = seg[0::2], seg[1::2]
            coco["annotations"].append({"id": aid, "image_id": i, "category_id": 1, "segmentation": [seg],
                                        "bbox": [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]})
            aid += 1
    # uma foto sem anotação (como as térmicas)
    im, _ = foto(7)
    im.resize((1158, 768)).save(os.path.join(fotos, "termica_sem_roi.jpg"), quality=92)
    with open(os.path.join(exp, "_annotations.coco.json"), "w") as f:
        json.dump(coco, f)
    print("ok", destino)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sint")
