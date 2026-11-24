"""
Condições de compressão de um relatório, escritas como texto curto.

    original              fotos originais, sem recompressão (PDF e ZIP)
    1200/70               uniforme: toda foto cabe em 1200×1200, JPEG qualidade 70
    roi:1200/70           ROI em degraus:
                            peça  -> 1200/70 (nítida)
                            anel  -> faixa em volta da peça (mais um raio dela), resolução
                                     cheia e JPEG q50: mostra o entorno
                            fundo -> metade da resolução (600 px) e JPEG q30
                          Fotos sem marcação saem iguais ao 1200/70; nas térmicas, a escala
                          de temperatura é a "peça".
    roi:1600/85/400/30    ROI explícita: lado/qualidade/fundo/qualidade do fundo
    roi:1600/85+pol       força o contorno (polígono dilatado) em todas as peças
    roi:1600/85+circ      força o círculo em todas as peças
    roi:1200/70+semanel   sem o anel (peça nítida direto no fundo)
    roi:1200/70/300/30+semanel   a ROI da primeira versão (fundo 1/4, sem anel)
                          (padrão: círculo nas peças compactas, contorno nas alongadas)

A comparação "1200/70" × "roi:1200/70" isola o efeito da ROI: a peça e as fotos
sem marcação ficam idênticas; só o fundo das fotos marcadas muda.

Este arquivo não depende de nada (roda no robô e no PC).
"""

from __future__ import annotations

import re

# matriz sugerida para o artigo (ver README)
CONDICOES_ARTIGO = ["original", "600/35", "800/45", "1200/70", "1600/85",
                    "roi:600/35", "roi:800/45", "roi:1200/70", "roi:1600/85"]

FUNDO_FRACAO = 2          # fundo = lado / 2 (a primeira versão usava lado / 4: borrado demais)
QUALIDADE_FUNDO = 30
ANEL = 1.0                # largura do anel em raios da peça (0 = sem anel)

_RE = re.compile(r"^(?:(?P<roi>roi):)?(?P<nums>\d+(?:/\d+){1,3})(?P<suf>(?:\+(?:pol|circ|semanel))*)$")


def ler_condicao(texto: str) -> dict:
    """'roi:1200/70' -> dicionário de configuração. Erro claro se o texto for inválido."""
    t = (texto or "").strip().lower().replace(" ", "")
    if t in ("original", "orig", "sem"):
        return {"modo": "original", "condicao": "original"}
    m = _RE.match(t)
    if not m:
        raise ValueError(f"condição inválida: {texto!r} (ex.: original, 1200/70, roi:1200/70)")
    nums = [int(v) for v in m.group("nums").split("/")]
    lado, qualidade = nums[0], nums[1]
    if not (64 <= lado <= 8000 and 1 <= qualidade <= 100):
        raise ValueError(f"lado/qualidade fora da faixa em {texto!r}")
    if not m.group("roi"):
        if len(nums) != 2 or m.group("suf"):
            raise ValueError(f"condição uniforme é só L/Q: {texto!r}")
        return {"modo": "uniforme", "lado": lado, "qualidade": qualidade, "condicao": f"{lado}/{qualidade}"}

    fundo = nums[2] if len(nums) > 2 else max(64, lado // FUNDO_FRACAO)
    qf = nums[3] if len(nums) > 3 else QUALIDADE_FUNDO
    cfg = {"modo": "roi", "lado": lado, "qualidade": qualidade,
           "roi_lado": lado, "roi_qualidade": qualidade,
           "roi_fundo": fundo, "roi_qualidade_fundo": qf,
           "roi_modo": "poligono" if "+pol" in m.group("suf") else "circulo" if "+circ" in m.group("suf") else "auto",
           "roi_anel": 0.0 if "+semanel" in m.group("suf") else ANEL}
    cfg["condicao"] = nome_curto(cfg)
    return cfg


def nome_curto(cfg: dict) -> str:
    """Forma canônica e curta, usada no CSV e na interface."""
    if cfg["modo"] == "original":
        return "original"
    if cfg["modo"] == "uniforme":
        return f"{cfg['lado']}/{cfg['qualidade']}"
    s = f"roi:{cfg['roi_lado']}/{cfg['roi_qualidade']}"
    padrao_fundo = max(64, cfg["roi_lado"] // FUNDO_FRACAO)
    if cfg["roi_fundo"] != padrao_fundo or cfg["roi_qualidade_fundo"] != QUALIDADE_FUNDO:
        s += f"/{cfg['roi_fundo']}/{cfg['roi_qualidade_fundo']}"
    s += {"poligono": "+pol", "circulo": "+circ"}.get(cfg.get("roi_modo"), "")
    if not cfg.get("roi_anel"):
        s += "+semanel"
    return s


# qualidade que acompanha cada lado no artigo: só aparece no nome se for diferente
QUALIDADE_DO_LADO = {600: 35, 800: 45, 1200: 70, 1600: 85}


def rotulo_download(cfg: dict) -> str:
    """Começo do nome do ZIP: '600x600', '600x600-ROI', '600x600-q50-ROI', 'original'."""
    if cfg["modo"] == "original":
        return "original"
    roi = cfg["modo"] == "roi"
    lado = cfg["roi_lado"] if roi else cfg["lado"]
    qual = cfg["roi_qualidade"] if roi else cfg["qualidade"]
    s = f"{lado}x{lado}"
    if QUALIDADE_DO_LADO.get(lado) != qual:
        s += f"-q{qual}"
    if roi:
        s += "-ROI"
        if cfg["roi_fundo"] != max(64, cfg["roi_lado"] // FUNDO_FRACAO) or cfg["roi_qualidade_fundo"] != QUALIDADE_FUNDO:
            s += f"-f{cfg['roi_fundo']}-qf{cfg['roi_qualidade_fundo']}"
        s += {"poligono": "-pol", "circulo": "-circ"}.get(cfg.get("roi_modo"), "")
        if not cfg.get("roi_anel"):
            s += "-semanel"
    return s


def nome_arquivo(cfg: dict) -> str:
    """Forma segura para nome de arquivo: 'roi1200q70f300qf30', '1200x1200_q70', 'original'."""
    if cfg["modo"] == "original":
        return "original"
    base = f"{cfg['lado']}x{cfg['lado']}_q{cfg['qualidade']}"
    if cfg["modo"] == "uniforme":
        return base
    s = f"roi{cfg['roi_lado']}q{cfg['roi_qualidade']}f{cfg['roi_fundo']}"
    if cfg["roi_qualidade_fundo"]:
        s += f"qf{cfg['roi_qualidade_fundo']}"
    s += {"poligono": "pol", "circulo": "circ"}.get(cfg.get("roi_modo"), "")
    if not cfg.get("roi_anel"):
        s += "semanel"
    if (cfg["lado"], cfg["qualidade"]) != (cfg["roi_lado"], cfg["roi_qualidade"]):
        s = base + "_" + s
    return s
