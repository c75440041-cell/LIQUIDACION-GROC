"""
BOT DE LIQUIDEZ (independiente) - BingX -> Telegram
Entra solo cuando el precio esta sobre una zona de liquidez IMPORTANTE y muestra fuerza:
  - SOUP   : Turtle Soup (barrido de la zona + cierre de vuelta dentro, en la misma vela)
  - SOUP+1 : Turtle Soup Plus One (barrido, cierra fuera, y la vela siguiente recupera el nivel)
  - REBOTE : rechazo en la zona sin barrido (mecha larga + volumen)
Zonas: cumulos de pivotes (maximos/minimos iguales) puntuados 0-100, con confluencia de
liquidaciones estimadas (modelo de apalancamiento x volumen). Sirve arriba, abajo e interna (medio).

v2 - VENTAJA MATEMATICA (ver lq_edge.py):
  * Coste en R: comision + deslizamiento / riesgo. Se descarta lo que el coste se come.
  * Cada candidata (enviada o "sombra") se sigue con triple barrera y entrena un modelo bayesiano online.
  * Solo se envia si el limite inferior de la ventaja neta estimada es positivo; ranking por ventaja.
  * Tamano por Kelly fraccional, limites de cartera, parada diaria y estado persistente.
"""
import asyncio
import os
import re
import time
import logging
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import ccxt.async_support as ccxt

import lq_edge as edge
from collections import Counter


def env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "si", "on")


def env_floats(name, default):
    return [float(x) for x in os.getenv(name, default).split(",") if x.strip()]


# ======================= CONFIG =======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TIMEFRAMES = [t.strip() for t in os.getenv("TIMEFRAMES", "5m,15m").split(",") if t.strip()]
CANDLES = int(os.getenv("CANDLES", "500"))
EMA_PERIOD = int(os.getenv("EMA_PERIOD", "200"))
MIN_VOLUME_USDT = float(os.getenv("MIN_VOLUME_USDT", "1000000"))   # sin liquidez real no hay "zonas"
CONCURRENCY = int(os.getenv("CONCURRENCY", "8"))
CLOSE_DELAY_SEC = int(os.getenv("CLOSE_DELAY_SEC", "3"))
MARKET_TYPE = os.getenv("MARKET_TYPE", "swap")

# Solo cripto
EXCLUDE_NON_CRYPTO = env_bool("EXCLUDE_NON_CRYPTO", True)
_DEFAULT_EXCLUDE = ("PAXG,XAUT,AAPLX,TSLAX,NVDAX,MSFTX,GOOGLX,AMZNX,METAX,COINX,MSTRX,HOODX,CRCLX,"
                    "SPYX,QQQX,NFLXX,AVGOX,JPMX,LLYX,WMTX,XOMX,ORCLX,AMDX,PLTRX,INTCX,IBMX,MCDX,GMEX")
EXCLUDE_BASES = {x.strip().upper() for x in os.getenv("EXCLUDE_BASES", _DEFAULT_EXCLUDE).split(",") if x.strip()}
NON_CRYPTO_RE = re.compile(r"^NC(CO|SK|SI|FX)")
EXCLUDED = []

# Zonas de liquidez
LQ_PIVOT = int(os.getenv("LQ_PIVOT", "5"))                 # velas a cada lado para un pivote
LQ_LOOKBACK = int(os.getenv("LQ_LOOKBACK", "300"))         # velas analizadas para zonas
LQ_TOL_ATR = float(os.getenv("LQ_TOL_ATR", "0.25"))        # tolerancia de "maximos iguales" en ATR
LQ_EXTREME_N = int(os.getenv("LQ_EXTREME_N", "20"))        # extremo de N velas (Turtle Soup original = 20)
LQ_MIN_ZONE_SCORE = float(os.getenv("LQ_MIN_ZONE_SCORE", "50"))   # importancia minima de la zona (0-100)

# Liquidaciones estimadas (modelo tipo heatmap)
LQ_LEVERAGES = env_floats("LQ_LEVERAGES", "10,25,50,100")
LQ_LEV_WEIGHTS = env_floats("LQ_LEV_WEIGHTS", "0.3,0.3,0.25,0.15")
LQ_LONG_SHARE = float(os.getenv("LQ_LONG_SHARE", "0.6"))   # % de longs en velas alcistas (y shorts en bajistas)
LQ_LIQ_RANGE_PCT = float(os.getenv("LQ_LIQ_RANGE_PCT", "10"))

# Senal / fuerza
LQ_ENABLE_SOUP = env_bool("LQ_ENABLE_SOUP", True)
LQ_ENABLE_PLUS1 = env_bool("LQ_ENABLE_PLUS1", True)
LQ_ENABLE_BOUNCE = env_bool("LQ_ENABLE_BOUNCE", True)
LQ_MIN_SWEEP_ATR = float(os.getenv("LQ_MIN_SWEEP_ATR", "0.05"))   # penetracion minima
LQ_MAX_SWEEP_ATR = float(os.getenv("LQ_MAX_SWEEP_ATR", "3.0"))    # mas que esto = ruptura real, no barrido
LQ_VOL_MULT = float(os.getenv("LQ_VOL_MULT", "1.5"))              # volumen minimo vs media 50
LQ_MIN_WICK = float(os.getenv("LQ_MIN_WICK", "0.35"))             # mecha de rechazo minima (% del rango)
LQ_MIN_QUALITY = float(os.getenv("LQ_MIN_QUALITY", "65"))
LQ_USE_EMA_FILTER = env_bool("LQ_USE_EMA_FILTER", False)          # exigir tendencia EMA200 a favor

# Gestion (SL / TP / salida)
LQ_SL_ATR_BUFFER = float(os.getenv("LQ_SL_ATR_BUFFER", "0.1"))
LQ_MIN_RR = float(os.getenv("LQ_MIN_RR", "1.5"))
LQ_DEFAULT_RR = float(os.getenv("LQ_DEFAULT_RR", "2.0"))
LQ_MAX_RR = float(os.getenv("LQ_MAX_RR", "4.0"))
LQ_MIN_TP_ZONE_SCORE = float(os.getenv("LQ_MIN_TP_ZONE_SCORE", "40"))
LQ_MAX_RISK_PCT = float(os.getenv("LQ_MAX_RISK_PCT", "3.0"))
LQ_MAX_HOLD_BARS = int(os.getenv("LQ_MAX_HOLD_BARS", "48"))
LQ_COOLDOWN_BARS = int(os.getenv("LQ_COOLDOWN_BARS", "6"))

# Resumen diario
REPORT_TZ = os.getenv("REPORT_TZ", "Europe/Madrid")
REPORT_TIME = os.getenv("REPORT_TIME", "23:59")
FEE_PCT_ROUNDTRIP = float(os.getenv("FEE_PCT_ROUNDTRIP", "0.1"))

# Extras traidos del indicador de TradingView (UT Bot + LinReg + S/R + panel)
LQ_USE_UT = env_bool("LQ_USE_UT", True)                    # UT Bot (trailing stop ATR) como confirmacion
UT_ATR_PERIOD = int(os.getenv("UT_ATR_PERIOD", "10"))
UT_MULT = float(os.getenv("UT_MULT", "2"))
LQ_UT_REQUIRE = env_bool("LQ_UT_REQUIRE", False)           # exigir UT a favor (filtro duro)
LQ_USE_LINREG = env_bool("LQ_USE_LINREG", True)            # velas LinReg + linea de senal
LINREG_LEN = int(os.getenv("LINREG_LEN", "11"))
LINREG_SIGNAL = int(os.getenv("LINREG_SIGNAL", "7"))
LQ_USE_SR = env_bool("LQ_USE_SR", True)                    # coincidencia con maximos/minimos de 100-500 velas
LQ_USE_HTF = env_bool("LQ_USE_HTF", True)                  # tendencia EMA9/21 en TF superiores + max/min de ayer
LQ_HTF_MIN_ALIGNED = int(os.getenv("LQ_HTF_MIN_ALIGNED", "0"))   # TF superiores a favor minimos (0 = solo puntua)
LQ_SHOW_PANEL = env_bool("LQ_SHOW_PANEL", True)            # panel de mercado en el mensaje

# ===== v2: ventaja matematica (coste en R, modelo bayesiano online, Kelly fraccional, cartera) =====
LQ_STATE_PATH = os.getenv("LQ_STATE_PATH", os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "lq_state.json"))
LQ_SLIP_LOW = float(os.getenv("LQ_SLIP_LOW", "0.08"))          # deslizamiento % por lado, par poco liquido
LQ_SLIP_MID = float(os.getenv("LQ_SLIP_MID", "0.04"))
LQ_SLIP_HIGH = float(os.getenv("LQ_SLIP_HIGH", "0.02"))
LQ_VOL_TIER1 = float(os.getenv("LQ_VOL_TIER1", "3000000"))     # volumen 24h (USDT) que separa LOW de MID
LQ_VOL_TIER2 = float(os.getenv("LQ_VOL_TIER2", "20000000"))    # y MID de HIGH
LQ_MAX_COST_R = float(os.getenv("LQ_MAX_COST_R", "0.25"))      # coste maximo en R (coste% / riesgo%)
LQ_EDGE_MODE = os.getenv("LQ_EDGE_MODE", "gate").strip().lower()   # gate | label | off
LQ_EDGE_Z = float(os.getenv("LQ_EDGE_Z", "0.25"))              # exigencia: mu_neto - z*se >= minimo
LQ_EDGE_MIN_R = float(os.getenv("LQ_EDGE_MIN_R", "0.0"))
LQ_MIN_SAMPLES = float(os.getenv("LQ_MIN_SAMPLES", "80"))      # muestras efectivas antes de fiarse del modelo
LQ_WARMUP_EMIT = env_bool("LQ_WARMUP_EMIT", True)              # enviar (marcado "calibrando") mientras aprende
LQ_MODEL_GAMMA = float(os.getenv("LQ_MODEL_GAMMA", "0.998"))   # olvido exponencial (~500 muestras efectivas)
LQ_SHADOW_MIN_QUALITY = float(os.getenv("LQ_SHADOW_MIN_QUALITY", "40"))   # candidatas "sombra" desde esta calidad
LQ_BASE_RISK_PCT = float(os.getenv("LQ_BASE_RISK_PCT", "0.25"))           # riesgo sugerido mientras calibra
LQ_KELLY_FRAC = float(os.getenv("LQ_KELLY_FRAC", "0.25"))
LQ_MAX_RISK_EQUITY_PCT = float(os.getenv("LQ_MAX_RISK_EQUITY_PCT", "1.0"))
LQ_MAX_OPEN = int(os.getenv("LQ_MAX_OPEN", "8"))
LQ_MAX_OPEN_SIDE = int(os.getenv("LQ_MAX_OPEN_SIDE", "5"))     # las altcoins estan muy correlacionadas
LQ_MAX_PER_SCAN = int(os.getenv("LQ_MAX_PER_SCAN", "4"))
LQ_DAILY_STOP_R = float(os.getenv("LQ_DAILY_STOP_R", "6"))     # parada diaria (R realizadas, dia UTC)
LQ_TP_MODE = os.getenv("LQ_TP_MODE", "zone").strip().lower()   # zone | auto (RR optimo empirico)
LQ_RR_GRID = env_floats("LQ_RR_GRID", "1,1.5,2,2.5,3,4")
LQ_RR_MIN_N = int(os.getenv("LQ_RR_MIN_N", "60"))
LQ_REQUIRE_VALIDATION = env_bool("LQ_REQUIRE_VALIDATION", True)   # el filtro debe ganar fuera de muestra o el bot calla
LQ_VALID_N = int(os.getenv("LQ_VALID_N", "40"))                   # candidatas "que habrian pasado" para validar
LQ_VALID_Z = float(os.getenv("LQ_VALID_Z", "1.0"))                # validado: media neta > z*se
LQ_REFUTE_Z = float(os.getenv("LQ_REFUTE_Z", "0.5"))              # refutado: media neta < -z*se
LQ_TRACK_MAX = int(os.getenv("LQ_TRACK_MAX", "3000"))
LQ_HISTORY_MAX = int(os.getenv("LQ_HISTORY_MAX", "4000"))
HTF_BONUS_MAX = 8     # maximo que pueden sumar despues TF superiores (+6) y max/min de ayer (+2)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("liquidez-bot")

POSITIONS = {}   # (symbol, tf) -> posicion virtual abierta (la que se avisa por Telegram)
COOLDOWN = {}    # (symbol, tf) -> ts de vela hasta la que no se vuelve a operar
CLOSED = []      # operaciones cerradas desde el ultimo resumen
TRACK = []       # candidatas (enviadas y "sombra") en seguimiento para aprender
HISTORY = []     # candidatas ya resueltas (registro compacto)
META = {}        # dia UTC y R del dia (parada diaria), avisos
SYMBOL_VOL = {}  # volumen 24h por par (para estimar deslizamiento)
MODEL = edge.EdgeModel(gamma=LQ_MODEL_GAMMA)


# ======================= UTILIDADES =======================
def tf_seconds(tf):
    return int(tf[:-1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[tf[-1]]


def calc_ema_last(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
    return prev


def is_non_crypto(m):
    base = (m.get("base") or "").upper()
    mid = (m.get("id") or "").upper()
    return bool(NON_CRYPTO_RE.match(base) or NON_CRYPTO_RE.match(mid) or base in EXCLUDE_BASES)


def fmt_link(symbol):
    base = symbol.split("/")[0]
    if MARKET_TYPE == "swap":
        return f"https://bingx.com/en/perpetual/{base}-USDT/"
    return f"https://bingx.com/en/spot/{base}USDT/"


def pretty(x):
    return f"{x:.8g}"


def fmt_age(bars, tf):
    secs = bars * tf_seconds(tf)
    if secs >= 86400:
        return f"{secs / 86400:.1f} d"
    if secs >= 3600:
        return f"{secs / 3600:.0f} h"
    return f"{secs / 60:.0f} min"


# ======================= INDICADORES (del Pine: UT Bot, LinReg, S/R, panel) =======================
def atr_series(c, period):
    """ATR de Wilder (como ta.atr). Lista alineada con c; None mientras no hay datos."""
    out = [None] * len(c)
    if len(c) <= period:
        return out
    trs = [c[0][2] - c[0][3]]
    for i in range(1, len(c)):
        trs.append(max(c[i][2] - c[i][3], abs(c[i][2] - c[i - 1][4]), abs(c[i][3] - c[i - 1][4])))
    prev = sum(trs[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(c)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def ut_bot(c, mult, period):
    """UT Bot: trailing stop por ATR sobre el cierre. state=1 alcista, -1 bajista."""
    atr = atr_series(c, period)
    trail = prev_src = None
    state, flip_i = 0, None
    for i, x in enumerate(c):
        if atr[i] is None:
            continue
        src, nl = x[4], mult * atr[i]
        if trail is None:
            new = src - nl
        elif src > trail and prev_src > trail:
            new = max(trail, src - nl)
        elif src < trail and prev_src < trail:
            new = min(trail, src + nl)
        else:
            new = src - nl if src > trail else src + nl
        trail = new
        st = 1 if src > trail else (-1 if src < trail else state)
        if st != state:
            flip_i = i
        state, prev_src = st, src
    if trail is None:
        return None
    return {"state": state, "trail": trail,
            "since_flip": (len(c) - 1 - flip_i) if flip_i is not None else 999}


def linreg_last(vals, length):
    y = vals[-length:]
    n = len(y)
    if n < length or n < 2:
        return None
    xm, ym = (n - 1) / 2, sum(y) / n
    den = sum((i - xm) ** 2 for i in range(n))
    slope = sum((i - xm) * (v - ym) for i, v in enumerate(y)) / den
    return ym + slope * ((n - 1) - xm)


def linreg_signal(closes, length, sig_len):
    """(cierre LinReg actual, media de senal). Cierre > senal = impulso alcista."""
    pts = []
    for k in range(sig_len):
        end = len(closes) - (sig_len - 1 - k)
        v = linreg_last(closes[max(0, end - length):end], length)
        if v is None:
            return None
        pts.append(v)
    return pts[-1], sum(pts) / sig_len


def sr_levels(c):
    """Maximos/minimos de 10..1000 velas (lineas S/R del Pine): [(ventana, max, min)]."""
    n = len(c)
    return [(w, max(x[2] for x in c[-w:]), min(x[3] for x in c[-w:]))
            for w in (10, 50, 100, 250, 500, 1000) if n >= w]


def ema_series(vals, period):
    if len(vals) < period:
        return []
    k = 2 / (period + 1)
    prev = sum(vals[:period]) / period
    out = [prev]
    for v in vals[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi_last(closes, period=14):
    if len(closes) <= period:
        return None
    ag = sum(max(closes[i] - closes[i - 1], 0) for i in range(1, period + 1)) / period
    al = sum(max(closes[i - 1] - closes[i], 0) for i in range(1, period + 1)) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def stoch_kd(c, length=14, sk=3, sd=3):
    raw = []
    for i in range(length - 1, len(c)):
        w = c[i - length + 1:i + 1]
        hh, ll = max(x[2] for x in w), min(x[3] for x in w)
        raw.append(50.0 if hh == ll else 100 * (c[i][4] - ll) / (hh - ll))
    ks = [sum(raw[j - sk + 1:j + 1]) / sk for j in range(sk - 1, len(raw))]
    ds = [sum(ks[j - sd + 1:j + 1]) / sd for j in range(sd - 1, len(ks))]
    return (ks[-1], ds[-1]) if ks and ds else None


# ======================= LIQUIDACIONES ESTIMADAS =======================
def liquidation_levels(c, price):
    """
    Modelo tipo heatmap: cada vela abre posiciones (peso = volumen en USDT) con reparto de
    apalancamientos; precio de liquidacion = entrada*(1 -/+ 0.95/L). Se descartan los niveles
    ya 'consumidos' (el precio paso por ellos despues). Devuelve niveles pendientes por lado:
    'S' = liquidacion de shorts (por encima), 'L' = liquidacion de longs (por debajo).
    """
    n = len(c)
    start = max(0, n - LQ_LOOKBACK)
    sufmin = [float("inf")] * (n + 1)
    sufmax = [float("-inf")] * (n + 1)
    for i in range(n - 1, -1, -1):
        sufmin[i] = min(c[i][3], sufmin[i + 1])
        sufmax[i] = max(c[i][2], sufmax[i + 1])
    lo_lim, hi_lim = price * (1 - LQ_LIQ_RANGE_PCT / 100), price * (1 + LQ_LIQ_RANGE_PCT / 100)
    out = {"S": [], "L": []}
    for i in range(start, n):
        o, h, l, cl, v = c[i][1], c[i][2], c[i][3], c[i][4], c[i][5] or 0
        entry = (h + l + cl) / 3
        w = v * entry
        long_share = LQ_LONG_SHARE if cl >= o else 1 - LQ_LONG_SHARE
        for lev, wt in zip(LQ_LEVERAGES, LQ_LEV_WEIGHTS):
            pl = entry * (1 - 0.95 / lev)
            if sufmin[i + 1] > pl and pl >= lo_lim:
                out["L"].append((pl, w * long_share * wt))
            ps = entry * (1 + 0.95 / lev)
            if sufmax[i + 1] < ps and ps <= hi_lim:
                out["S"].append((ps, w * (1 - long_share) * wt))
    stats = {}
    for side in ("S", "L"):
        lv = out[side]
        if lv:
            tot = sum(w for _, w in lv)
            span = max(p for p, _ in lv) - min(p for p, _ in lv)
        else:
            tot, span = 0.0, 0.0
        stats[side] = (lv, tot, span)
    return stats


# ======================= ZONAS DE LIQUIDEZ =======================
def analyze_zones(c):
    """Zonas de liquidez sin barrer, puntuadas 0-100, calculadas con las velas c (sin la vela de senal)."""
    n = len(c)
    k = LQ_PIVOT
    if n < 120:
        return None
    opens = [x[1] for x in c]
    highs = [x[2] for x in c]
    lows = [x[3] for x in c]
    closes = [x[4] for x in c]
    vols = [x[5] or 0 for x in c]
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(n - 14, n)]
    atr = sum(trs) / 14
    avgvol = sum(vols[-50:]) / 50
    if atr <= 0 or avgvol <= 0:
        return None
    tol = LQ_TOL_ATR * atr
    price = closes[-1]
    start = max(1, n - LQ_LOOKBACK)
    rng_hi, rng_lo = max(highs[-100:]), min(lows[-100:])
    span = max(rng_hi - rng_lo, 1e-12)
    ext_hi, ext_lo = max(highs[-LQ_EXTREME_N:]), min(lows[-LQ_EXTREME_N:])
    liq = liquidation_levels(c, price)

    zones = []
    for kind, series, extreme in (("BSL", highs, ext_hi), ("SSL", lows, ext_lo)):
        piv = []
        for i in range(start + k, n - k):
            w = series[i - k:i + k + 1]
            if (kind == "BSL" and series[i] == max(w)) or (kind == "SSL" and series[i] == min(w)):
                piv.append(i)
        piv.sort(key=lambda i: series[i])
        groups, cur = [], []
        for i in piv:
            if cur and abs(series[i] - series[cur[-1]]) > tol:
                groups.append(cur)
                cur = []
            cur.append(i)
        if cur:
            groups.append(cur)

        lv, tot, lspan = liq["S" if kind == "BSL" else "L"]
        for g in groups:
            last_p = max(g)
            level = max(series[i] for i in g) if kind == "BSL" else min(series[i] for i in g)
            after = series[last_p + 1:]
            if kind == "BSL" and after and max(after) > level + tol:
                continue    # ya barrida
            if kind == "SSL" and after and min(after) < level - tol:
                continue
            touches = len(g)
            age = n - last_p
            rvp = sum(vols[i] for i in g) / len(g) / avgvol
            dens = sum(w for p, w in lv if abs(p - level) <= tol)
            expected = tot / max(1.0, lspan / (2 * tol)) if tot > 0 else 0
            ratio = dens / expected if expected > 0 else 0.0
            pos = (level - rng_lo) / span
            ztype = "externa alta" if pos >= 0.67 else ("externa baja" if pos <= 0.33 else "interna")
            is_ext = abs(level - extreme) <= tol
            ext_pts = 15 if is_ext else (8 if ztype != "interna" else 0)
            score = (30 * min(touches, 4) / 4
                     + 10 * max(0.0, 1 - age / LQ_LOOKBACK)
                     + 15 * min(rvp / 3, 1)
                     + 30 * min(max(ratio - 1, 0) / 4, 1)
                     + ext_pts)
            zones.append({"kind": kind, "level": level, "score": score, "touches": touches,
                          "age": age, "ratio": ratio, "ext20": is_ext, "ztype": ztype})
    return zones, atr, avgvol, tol


# ======================= DETECCION DE SENALES =======================
def detect_signal(closed, ema200):
    """Evalua la ultima vela cerrada. Zonas calculadas SIN las dos ultimas velas (sin sesgo de futuro)."""
    n = len(closed)
    if n < 160:
        return None
    za = analyze_zones(closed[:-2])
    if not za:
        return None
    zones, atr, avgvol, tol = za
    c2, c1 = closed[-2], closed[-1]
    min_sw, max_sw = LQ_MIN_SWEEP_ATR * atr, LQ_MAX_SWEEP_ATR * atr
    best = None
    ut = ut_bot(closed, UT_MULT, UT_ATR_PERIOD) if LQ_USE_UT else None
    lrs = linreg_signal([x[4] for x in closed], LINREG_LEN, LINREG_SIGNAL) if LQ_USE_LINREG else None
    srl = sr_levels(closed[:-2]) if LQ_USE_SR else []
    late_max = 8 if LQ_USE_HTF else 0     # bonus maximo que se suma despues (TF superiores + ayer)

    for z in zones:
        if z["score"] < LQ_MIN_ZONE_SCORE:
            continue
        L = z["level"]
        bsl = z["kind"] == "BSL"
        side = "SHORT" if bsl else "LONG"

        def pen(c):   # penetracion de la vela mas alla del nivel
            return (c[2] - L) if bsl else (L - c[3])

        def wick(c):
            rng = c[2] - c[3]
            if rng <= 0:
                return 0.0
            return (c[2] - max(c[1], c[4])) / rng if bsl else (min(c[1], c[4]) - c[3]) / rng

        def back_inside(c):
            return c[4] < L if bsl else c[4] > L

        def against_dir(c):   # vela en la direccion de la operacion
            return c[4] < c[1] if bsl else c[4] > c[1]

        p2, p1 = pen(c2), pen(c1)
        kind = None
        rv = wk = depth = 0.0
        extreme = None
        if LQ_ENABLE_SOUP and p2 <= min_sw and min_sw <= p1 <= max_sw and back_inside(c1):
            kind, rv, wk, depth = "SOUP", c1[5] / avgvol, wick(c1), p1 / atr
            extreme = c1[2] if bsl else c1[3]
        elif (LQ_ENABLE_PLUS1 and min_sw <= p2 <= max_sw and p1 <= max_sw and back_inside(c1)
              and ((c2[4] >= L) if bsl else (c2[4] <= L)) and against_dir(c1)):
            kind, rv, wk, depth = "SOUP+1", max(c2[5], c1[5]) / avgvol, wick(c2), p2 / atr
            extreme = max(c2[2], c1[2]) if bsl else min(c2[3], c1[3])
        elif (LQ_ENABLE_BOUNCE and p2 <= min_sw and -tol <= p1 <= min_sw and back_inside(c1)
              and against_dir(c1) and wick(c1) >= max(LQ_MIN_WICK, 0.5)):
            kind, rv, wk, depth = "REBOTE", c1[5] / avgvol, wick(c1), 0.0
            extreme = c1[2] if bsl else c1[3]
        if not kind:
            continue
        if rv < LQ_VOL_MULT or (kind != "SOUP+1" and wk < LQ_MIN_WICK):
            continue

        entry = c1[4]
        buf = LQ_SL_ATR_BUFFER * atr
        sl = extreme + buf if bsl else extreme - buf
        risk = abs(sl - entry)
        if risk <= 0 or risk / entry * 100 > LQ_MAX_RISK_PCT:
            continue
        # objetivo: siguiente zona de liquidez importante en sentido contrario
        tp, tp_zone = None, None
        opp = [o for o in zones if o["kind"] == ("SSL" if bsl else "BSL") and o["score"] >= LQ_MIN_TP_ZONE_SCORE]
        if bsl:
            cand = [o for o in opp if o["level"] < entry - risk]
            if cand:
                tp_zone = max(cand, key=lambda o: o["level"])
                tp = tp_zone["level"] + 0.1 * atr
        else:
            cand = [o for o in opp if o["level"] > entry + risk]
            if cand:
                tp_zone = min(cand, key=lambda o: o["level"])
                tp = tp_zone["level"] - 0.1 * atr
        if tp is None:
            tp = entry - LQ_DEFAULT_RR * risk if bsl else entry + LQ_DEFAULT_RR * risk
        reward = abs(tp - entry)
        if reward > LQ_MAX_RR * risk:
            reward = LQ_MAX_RR * risk
            tp = entry - reward if bsl else entry + reward
        rr = reward / risk
        if rr < LQ_MIN_RR:
            continue
        aligned = (entry < ema200) if bsl else (entry > ema200)
        if LQ_USE_EMA_FILTER and not aligned:
            continue

        depth_pts = 0 if kind == "REBOTE" else (10 if 0.1 <= depth <= 1.0 else 5)
        quality = (40 * z["score"] / 100
                   + 20 * min(max(rv - 1, 0) / 2, 1)
                   + 15 * min(wk / 0.7, 1)
                   + depth_pts
                   + (10 if aligned else 0)
                   + (5 if rr >= 2 else 0)
                   - (10 if kind == "REBOTE" else 0))
        # --- extras importados del Pine ---
        want = 1 if side == "LONG" else -1
        ut_pts = lr_pts = sr_pts = 0
        sr_txt = None
        if ut:
            ut_ok = ut["state"] == want
            if LQ_UT_REQUIRE and not ut_ok:
                continue
            ut_pts = (4 if ut_ok else 0) + (2 if ut_ok and ut["since_flip"] <= 3 else 0)
        if lrs:
            lr_ok = (lrs[0] > lrs[1]) if want == 1 else (lrs[0] < lrs[1])
            lr_pts = 3 if lr_ok else 0
        for w, hi, lo in reversed(srl):
            lvl = hi if bsl else lo
            if w >= 100 and abs(lvl - L) <= tol:
                sr_pts = 3
                sr_txt = f"coincide con {'máx' if bsl else 'mín'} de {w} velas"
                break
        quality += ut_pts + lr_pts + sr_pts
        if quality < min(LQ_MIN_QUALITY - late_max, LQ_SHADOW_MIN_QUALITY):
            continue   # por debajo del suelo de 'sombra' ni se sigue
        cand_sig = {"side": side, "kind": kind, "zone": z, "entry": entry, "sl": sl, "tp": tp,
                    "rr": rr, "quality": quality, "rv": rv, "wick": wk, "tp_zone": tp_zone,
                    "aligned": aligned, "atr": atr, "ts": c1[0], "tol": tol, "depth": depth,
                    "ut": ut, "lr": lrs, "sr_txt": sr_txt,
                    "extras": {"UT": ut_pts, "LinReg": lr_pts, "S/R": sr_pts}}
        if best is None or quality > best["quality"]:
            best = cand_sig
    return best


def manage_position(pos, closed):
    """Revisa velas posteriores a la entrada. Devuelve (precio_salida, motivo, velas) o None."""
    bars = 0
    for c in closed:
        if c[0] <= pos["ts"]:
            continue
        bars += 1
        if pos["side"] == "LONG":
            hit_sl, hit_tp = c[3] <= pos["sl"], c[2] >= pos["tp"]
        else:
            hit_sl, hit_tp = c[2] >= pos["sl"], c[3] <= pos["tp"]
        if hit_sl:   # si SL y TP caen en la misma vela, se asume SL (conservador)
            return pos["sl"], "SL", bars
        if hit_tp:
            return pos["tp"], "TP", bars
        if bars >= LQ_MAX_HOLD_BARS:
            return c[4], "TIEMPO", bars
    return None


# ======================= TELEGRAM =======================
# Cola con ritmo controlado: Telegram permite ~1 msg/s por chat (20/min en grupos) y, si se excede,
# responde 429 con un "retry_after" que puede ser de HORAS. Seguir enviando durante el bloqueo lo alarga.
TG_MIN_INTERVAL = float(os.getenv("TG_MIN_INTERVAL", "3.2"))   # seg entre mensajes
TG_MAX_AGE = float(os.getenv("TG_MAX_AGE_SEC", "900"))         # una senal con mas de 15 min ya no sirve
TG_QUEUE_MAX = int(os.getenv("TG_QUEUE_MAX", "100"))
TG_QUEUE = None
TG_BLOCKED_UNTIL = 0.0


async def send_telegram(session, text):
    """Encola el mensaje (no bloquea el escaneo). Lo envia telegram_worker respetando limites."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
        return
    if TG_QUEUE is None:
        return
    if TG_QUEUE.full():
        try:
            TG_QUEUE.get_nowait()  # descarta el mas antiguo
            log.warning("Cola de Telegram llena: descartado el mensaje mas antiguo")
        except asyncio.QueueEmpty:
            pass
    TG_QUEUE.put_nowait((time.time(), text))


async def telegram_worker(session):
    global TG_BLOCKED_UNTIL
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    while True:
        ts, text = await TG_QUEUE.get()
        plain = False
        while True:
            now = time.time()
            if now - ts > TG_MAX_AGE:
                log.warning("Mensaje descartado por antiguo (%.0f min en cola)", (now - ts) / 60)
                break
            if now < TG_BLOCKED_UNTIL:
                await asyncio.sleep(min(60.0, TG_BLOCKED_UNTIL - now))
                continue
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
            if not plain:
                payload["parse_mode"] = "HTML"
            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    body = await r.text()
                    if r.status == 200:
                        await asyncio.sleep(TG_MIN_INTERVAL)
                        break
                    if r.status == 429:
                        m = re.search(r'"retry_after"\s*:\s*(\d+)', body)
                        wait = int(m.group(1)) if m else 60
                        if time.time() >= TG_BLOCKED_UNTIL:  # loguea una sola vez por bloqueo
                            log.error("Telegram 429: bloqueado %d s (~%.1f h). Pauso envios.", wait, wait / 3600)
                        TG_BLOCKED_UNTIL = time.time() + wait + 1
                        continue
                    if r.status == 400 and "parse" in body.lower() and not plain:
                        plain = True  # HTML mal formado: reenviar como texto plano
                        text = re.sub(r"<[^>]+>", "", text)
                        continue
                    log.error("Telegram %s: %s", r.status, body[:200])
                    break
            except Exception as e:
                log.error("Error Telegram: %s", e)
                await asyncio.sleep(5)
                break


async def send_long(session, text):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            await send_telegram(session, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await send_telegram(session, chunk)


# ======================= MERCADOS =======================
async def get_symbols(ex):
    await ex.load_markets(True)
    symbols, excluded = [], []
    for s, m in ex.markets.items():
        if not m.get("active", True) or m.get("quote") != "USDT":
            continue
        if MARKET_TYPE == "swap" and not (m.get("swap") and m.get("linear")):
            continue
        if MARKET_TYPE == "spot" and not m.get("spot"):
            continue
        if EXCLUDE_NON_CRYPTO and is_non_crypto(m):
            excluded.append(m.get("base") or s)
            continue
        symbols.append(s)
    EXCLUDED[:] = sorted(excluded)
    try:
        tickers = await ex.fetch_tickers(symbols)
        for s in symbols:
            SYMBOL_VOL[s] = tickers.get(s, {}).get("quoteVolume") or 0
        if MIN_VOLUME_USDT > 0:
            symbols = [s for s in symbols if SYMBOL_VOL.get(s, 0) >= MIN_VOLUME_USDT]
    except Exception as e:
        log.warning("No se pudo filtrar por volumen: %s", e)
    return symbols


# ======================= ESTADO / PERSISTENCIA / COSTES / RASTREADORES (v2) =======================
def _pk(key):
    return f"{key[0]}|{key[1]}"


def _uk(s):
    a, b = s.rsplit("|", 1)
    return (a, b)


def state_dump():
    return {"v": 2, "positions": {_pk(k): v for k, v in POSITIONS.items()},
            "cooldown": {_pk(k): v for k, v in COOLDOWN.items()},
            "closed": CLOSED, "track": TRACK, "history": HISTORY[-LQ_HISTORY_MAX:],
            "model": MODEL.to_dict(), "meta": META}


def state_restore(raw):
    global MODEL
    POSITIONS.clear()
    POSITIONS.update({_uk(k): v for k, v in (raw.get("positions") or {}).items()})
    COOLDOWN.clear()
    COOLDOWN.update({_uk(k): v for k, v in (raw.get("cooldown") or {}).items()})
    CLOSED[:] = raw.get("closed") or []
    TRACK[:] = raw.get("track") or []
    HISTORY[:] = raw.get("history") or []
    META.clear()
    META.update(raw.get("meta") or {})
    MODEL = edge.EdgeModel.from_dict(raw.get("model"), gamma=LQ_MODEL_GAMMA)


def save_state():
    """Guarda el estado (atomico). Si falla solo avisa una vez: el bot sigue funcionando."""
    try:
        edge.save_state(LQ_STATE_PATH, state_dump())
        META["_save_err"] = False
        return True
    except Exception as e:
        if not META.get("_save_err"):
            log.error("No se pudo guardar el estado en %s: %s", LQ_STATE_PATH, e)
        META["_save_err"] = True
        return False


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def day_r_add(r):
    if META.get("day") != _today():
        META["day"], META["day_r"] = _today(), 0.0
    META["day_r"] = META.get("day_r", 0.0) + r


def day_r_now():
    return META.get("day_r", 0.0) if META.get("day") == _today() else 0.0


def slip_side_pct(symbol):
    """Deslizamiento estimado por lado (%), segun liquidez 24h del par."""
    v = SYMBOL_VOL.get(symbol)
    if v is None:
        return LQ_SLIP_MID
    return LQ_SLIP_LOW if v < LQ_VOL_TIER1 else (LQ_SLIP_MID if v < LQ_VOL_TIER2 else LQ_SLIP_HIGH)


def trade_cost_pct(symbol):
    """Coste de ida y vuelta (%): comisiones + deslizamiento en entrada y salida."""
    return FEE_PCT_ROUNDTRIP + 2 * slip_side_pct(symbol)


def advance_trackers(symbol, tf, closed):
    """Avanza las candidatas en seguimiento de esta moneda/TF; las resueltas entrenan el modelo."""
    finished, gaps = [], []
    tf_ms = tf_seconds(tf) * 1000
    for t in TRACK:
        if t["symbol"] == symbol and t["tf"] == tf:
            if closed[0][0] > t["last_ts"] + tf_ms:      # faltan velas (bot caido): no se puede seguir sin sesgo
                gaps.append(t)
                continue
            edge.advance_track(t, closed, LQ_MAX_HOLD_BARS)
            if t["done"]:
                finished.append(t)
    for t in gaps:
        TRACK.remove(t)
    for t in finished:
        TRACK.remove(t)
        y = edge.outcome_r(t, t["rr"])
        MODEL.update(t["x"], y)
        HISTORY.append(edge.compact(t, y))
    if len(HISTORY) > LQ_HISTORY_MAX:
        del HISTORY[:len(HISTORY) - LQ_HISTORY_MAX]


def prune_trackers():
    """Descarta seguimientos huerfanos (moneda retirada o sin datos desde hace dias)."""
    lim = int(time.time() * 1000) - 3 * 86400 * 1000
    TRACK[:] = [t for t in TRACK if t["last_ts"] >= lim]


def add_tracker(sig, emitted):
    tid = sig["id"]
    if any(t["id"] == tid for t in TRACK) or len(TRACK) >= LQ_TRACK_MAX:
        return
    if not emitted and any(t["symbol"] == sig["symbol"] and t["tf"] == sig["tf"] for t in TRACK):
        return      # una sola sombra abierta por moneda/TF (evita contar la misma zona varias veces)
    TRACK.append(edge.new_track(sig, sig["x"], emitted, sig["mu_net"], sig["lcb"]))


def apply_empirical_tp(sig):
    """LQ_TP_MODE=auto: mueve el TP al RR con mejor limite inferior de R neta (contrafactual con MFE)."""
    pool = [h for h in HISTORY if h["kind"] == sig["kind"] and h["side"] == sig["side"]]
    if len(pool) < LQ_RR_MIN_N:
        pool = [h for h in HISTORY if h["kind"] == sig["kind"]]
    r = edge.best_rr(pool[-1500:], LQ_RR_GRID, z=1.0, min_n=LQ_RR_MIN_N)
    if not r:
        return
    k = r[0]
    risk = abs(sig["entry"] - sig["sl"])
    sig["tp"] = sig["entry"] - k * risk if sig["side"] == "SHORT" else sig["entry"] + k * risk
    sig["rr"], sig["tp_zone"], sig["tp_auto"] = k, None, True


# ======================= ANALISIS POR MONEDA =======================
async def analyze(ex, session, sem, symbol, tf):
    """Gestiona posiciones y seguimientos de la moneda. Devuelve (mensajes_enviados, candidata | None)."""
    async with sem:
        try:
            ohlcv = await ex.fetch_ohlcv(symbol, tf, limit=CANDLES)
        except Exception as e:
            log.debug("%s %s error: %s", symbol, tf, e)
            return 0, None
    now_ms = int(time.time() * 1000)
    tf_ms = tf_seconds(tf) * 1000
    closed = [c for c in ohlcv if c[0] + tf_ms <= now_ms]
    if len(closed) < max(EMA_PERIOD + 2, 160):
        return 0, None
    last = closed[-1]
    key = (symbol, tf)
    link = f'<a href="{fmt_link(symbol)}">Abrir en BingX</a>'
    sent = 0

    # 0) aprendizaje: avanzar seguimientos (enviadas y sombra) y actualizar el modelo
    advance_trackers(symbol, tf, closed)

    # 1) gestionar posicion virtual abierta (cuando salir)
    pos = POSITIONS.get(key)
    if pos:
        res = manage_position(pos, closed)
        if res:
            exit_price, reason, bars = res
            long_ = pos["side"] == "LONG"
            move = (exit_price - pos["entry"]) if long_ else (pos["entry"] - exit_price)
            risk_abs = abs(pos["entry"] - pos["sl"])
            cost = pos.get("cost_pct", FEE_PCT_ROUNDTRIP)
            risk_pct = pos.get("risk_pct") or (risk_abs / pos["entry"] * 100)
            r_gross = move / risk_abs if risk_abs > 0 else 0.0
            r_net = r_gross - cost / risk_pct
            pnl = move / pos["entry"] * 100 - cost
            icon = "✅ GANADA" if pnl > 0 else "❌ PERDIDA"
            motivo = {"TP": "Objetivo alcanzado 🎯", "SL": "Stop alcanzado 🛑",
                      "TIEMPO": f"Salida por tiempo ({LQ_MAX_HOLD_BARS} velas) ⏱"}[reason]
            await send_telegram(session,
                f"🏁 <b>SALIR {pos['side']}</b> · {tf} · {pos['kind']}\n<b>{symbol}</b>\n{motivo}\n"
                f"Entrada: <code>{pretty(pos['entry'])}</code> → Salida: <code>{pretty(exit_price)}</code>\n"
                f"{icon}: <b>{pnl:+.2f}%</b> = <b>{r_net:+.2f}R</b> (neto de costes) · {bars} velas\n{link}")
            CLOSED.append({"symbol": symbol, "tf": tf, "side": pos["side"], "kind": pos["kind"],
                           "pnl": pnl, "r_gross": r_gross, "r_net": r_net,
                           "risk_eq": pos.get("risk_eq", 0.0), "reason": reason, "ts": time.time()})
            day_r_add(r_net)
            del POSITIONS[key]
            COOLDOWN[key] = last[0] + LQ_COOLDOWN_BARS * tf_ms
            sent += 1
        return sent, None

    # 2) buscar senal nueva (candidata): el filtrado final y la cartera se deciden en dispatch()
    if last[0] < COOLDOWN.get(key, 0):
        return 0, None
    ema200 = calc_ema_last([c[4] for c in closed], EMA_PERIOD)
    if ema200 is None:
        return 0, None
    sig = detect_signal(closed, ema200)
    if not sig:
        return 0, None
    sig.update({"symbol": symbol, "tf": tf, "_closed": closed, "id": f"{symbol}|{tf}|{sig['ts']}"})
    sig["risk_pct"] = abs(sig["sl"] - sig["entry"]) / sig["entry"] * 100
    sig["cost_pct"] = trade_cost_pct(symbol)
    sig["tp_auto"] = False
    if LQ_TP_MODE == "auto":
        apply_empirical_tp(sig)
    sig["cost_r"] = sig["cost_pct"] / sig["risk_pct"]
    sig["x"] = edge.build_features(sig, closed, ema200)
    mu, se, s2 = MODEL.predict(sig["x"])
    sig.update({"mu": mu, "se": se, "s2": s2, "mu_net": mu - sig["cost_r"],
                "lcb": mu - sig["cost_r"] - LQ_EDGE_Z * se})

    htf = None
    if LQ_USE_HTF and sig["quality"] + HTF_BONUS_MAX >= LQ_MIN_QUALITY:   # solo si aun podria emitirse
        try:
            htf = await get_htf_context(ex, symbol, tf, sig)
        except Exception as e:
            log.debug("%s HTF error: %s", symbol, e)
        if htf:
            sig["extras"]["HTF"] = htf["bonus"]
            sig["extras"]["Ayer"] = htf["pd_bonus"]
            sig["quality"] += htf["bonus"] + htf["pd_bonus"]
    sig["htf"] = htf
    sig["quality"] = min(100.0, sig["quality"])
    return 0, sig


def z_name(z):
    return f"{'BSL' if z['kind'] == 'BSL' else 'SSL'} {pretty(z['level'])} ({z['ztype']}, {z['score']:.0f} pts)"


HTF_MAP = {"1m": ["5m", "15m", "1h"], "3m": ["15m", "1h", "4h"], "5m": ["15m", "1h", "4h"],
           "15m": ["1h", "4h", "1d"], "30m": ["1h", "4h", "1d"], "1h": ["4h", "1d", "1w"]}


def htf_list(tf):
    if tf in HTF_MAP:
        return HTF_MAP[tf]
    base = tf_seconds(tf)
    return [t for t in ("15m", "1h", "4h", "1d", "1w") if tf_seconds(t) > base][:3] or ["1d"]


async def get_htf_context(ex, symbol, tf, sig):
    """Tendencia EMA9/21 en 3 TF superiores (como el panel del Pine) + max/min de ayer."""
    tfs = htf_list(tf)
    need = list(dict.fromkeys(tfs + ["1d"]))
    res = await asyncio.gather(*[ex.fetch_ohlcv(symbol, t, limit=60) for t in need], return_exceptions=True)
    data = {t: r for t, r in zip(need, res) if not isinstance(r, Exception) and r}
    want = 1 if sig["side"] == "LONG" else -1
    trends, aligned, opposed = [], 0, 0
    for t in tfs:
        d = data.get(t)
        if not d or len(d) < 25:
            continue
        cl = [x[4] for x in d]
        e9, e21 = calc_ema_last(cl, 9), calc_ema_last(cl, 21)
        tr = 1 if e9 > e21 else (-1 if e9 < e21 else 0)
        trends.append((t, tr))
        aligned += tr == want
        opposed += tr == -want
    pdh = pdl = None
    pd_bonus = 0
    d1 = data.get("1d")
    if d1 and len(d1) >= 2:
        pdh, pdl = d1[-2][2], d1[-2][3]
        lvl = pdh if sig["zone"]["kind"] == "BSL" else pdl
        if abs(lvl - sig["zone"]["level"]) <= sig["tol"]:
            pd_bonus = 2
    if not trends and pdh is None:
        return None
    return {"trends": trends, "aligned": aligned, "bonus": max(-4, min(6, 2 * aligned - opposed)),
            "pd_bonus": pd_bonus, "pdh": pdh, "pdl": pdl}


def build_panel(closed, sig, htf):
    """Panel de mercado (version Telegram del dashboard del Pine). Termina en salto de linea."""
    ic = lambda ok: "🟢" if ok else "🔴"
    cl = [x[4] for x in closed]
    out = ["", "📊 <b>Panel</b>"]
    a = [v for v in atr_series(closed[-250:], 14) if v is not None]
    if len(a) >= 20:
        w = a[-20:]
        sma = sum(w) / 20
        sd = math.sqrt(sum((v - sma) ** 2 for v in w) / 20)
        top, bot = sma + 2 * sd, sma - 2 * sd
        vol = 30 + 40 * ((a[-1] - bot) / max(1e-10, top - bot))
        st = "alta 🟢" if a[-1] > sma * 1.2 else ("baja 🔴" if a[-1] < sma * 0.8 else "media ➖")
        out.append(f"Volatilidad {vol:.0f}% · ATR {st}")
    vols = [x[5] or 0 for x in closed]
    vma = sum(vols[-20:]) / 20
    if vma > 0:
        out.append(f"Volumen {vols[-1] / vma:.1f}x media20 {ic(vols[-1] > vma)}")
    day0 = closed[-1][0] - closed[-1][0] % 86400000
    dv = [(x[4], x[5] or 0) for x in closed if x[0] >= day0]
    tv = sum(v for _, v in dv)
    if tv > 0 and len(dv) >= 3:
        vw = sum(p * v for p, v in dv) / tv
        out.append(f"VWAP del día: precio {'por encima' if cl[-1] > vw else 'por debajo'} {ic(cl[-1] > vw)}")
    parts = []
    c3 = cl[-300:]
    e12, e26 = ema_series(c3, 12), ema_series(c3, 26)
    if e26:
        m = [e12[j + 14] - e26[j] for j in range(len(e26))]
        s9 = ema_series(m, 9)
        if s9:
            parts.append(f"MACD {ic(m[-1] > s9[-1])} {'alcista' if m[-1] > s9[-1] else 'bajista'}")
    kd = stoch_kd(closed[-80:])
    if kd:
        k, d = kd
        parts.append(f"Stoch {ic(k > d)} {'sobrecompra' if k > 80 else 'sobreventa' if k < 20 else 'neutral'}")
    rsi = rsi_last(cl[-200:])
    if rsi is not None:
        parts.append(f"RSI {rsi:.0f}")
    if parts:
        out.append(" · ".join(parts))
    ut = sig.get("ut")
    if ut:
        flip = f" (giro hace {ut['since_flip']} velas)" if ut["since_flip"] <= 3 else ""
        out.append(f"UT Bot: {'🟢 alcista' if ut['state'] == 1 else '🔴 bajista'} · trail <code>{pretty(ut['trail'])}</code>{flip}")
    lr = sig.get("lr")
    if lr:
        out.append(f"LinReg: {ic(lr[0] > lr[1])} {'alcista' if lr[0] > lr[1] else 'bajista'}")
    if htf and htf["trends"]:
        sym = {1: "🟢", -1: "🔴", 0: "➖"}
        out.append("Tendencia EMA9/21: " + " · ".join(f"{t} {sym[v]}" for t, v in htf["trends"]))
    if htf and htf["pdh"] is not None:
        hit = " ← la zona coincide" if htf["pd_bonus"] else ""
        out.append(f"Ayer: máx <code>{pretty(htf['pdh'])}</code> · mín <code>{pretty(htf['pdl'])}</code>{hit}")
    return "\n".join(out) + "\n"


# ======================= DECISION: VENTAJA + CARTERA =======================
def edge_status():
    """
    Fase del modelo:  off | calibrando (pocas muestras) | validando | validado | refutado.
    'validado/refutado' sale de comparar lo que el filtro HABRIA dejado pasar con lo que rindio de verdad
    (prequential, incluye candidatas sombra). Solo 'validado' habilita el tamano Kelly.
    """
    if LQ_EDGE_MODE == "off":
        return {"phase": "off", "n": 0, "mean": 0.0, "se": 0.0}
    if MODEL.neff < LQ_MIN_SAMPLES:
        return {"phase": "calibrando", "n": 0, "mean": 0.0, "se": 0.0}
    phase, n, mean, se = edge.validate_selection(HISTORY[-1500:], LQ_EDGE_MIN_R, LQ_MAX_COST_R,
                                                 z=LQ_VALID_Z, min_n=LQ_VALID_N, z_ref=LQ_REFUTE_Z)
    if not LQ_REQUIRE_VALIDATION and phase == "refutado":
        phase = "validando"
    return {"phase": phase, "n": n, "mean": mean, "se": se}


def eligible(sig, ph):
    """Filtros que no dependen de la cartera. Devuelve (ok, motivo_rechazo)."""
    if sig["quality"] < LQ_MIN_QUALITY:
        return False, "calidad"
    htf = sig.get("htf")
    if htf and htf["aligned"] < LQ_HTF_MIN_ALIGNED:
        return False, "tendencia TF"
    if sig["cost_r"] > LQ_MAX_COST_R:
        return False, "coste/riesgo"
    if LQ_EDGE_MODE == "gate":
        if ph["phase"] == "calibrando":
            if not LQ_WARMUP_EMIT:
                return False, "calibrando"
        elif ph["phase"] == "refutado":
            return False, "modelo refutado"
        elif sig["lcb"] < LQ_EDGE_MIN_R:
            return False, "sin ventaja"
    return True, ""


def size_for(sig, ph):
    """% del capital a arriesgar: Kelly fraccional (solo si el modelo esta validado); si no, tamano base."""
    if ph["phase"] == "validado" and LQ_EDGE_MODE != "off":
        return edge.kelly_risk_pct(sig["lcb"], sig["mu_net"], sig["s2"], LQ_KELLY_FRAC, LQ_MAX_RISK_EQUITY_PCT)
    return LQ_BASE_RISK_PCT


def edge_lines(sig, risk_eq, ph):
    c, rr = sig["cost_r"], sig["rr"]
    be = (1 + c) / (rr + 1) * 100          # acierto minimo: p*(RR+1) = 1 + c
    out = []
    phase = ph["phase"]
    if phase == "validado":
        out.append(f"🧮 Edge: E[R] neto <b>{sig['mu_net']:+.2f}R</b> (límite inf. {sig['lcb']:+.2f}R) · filtro validado ✅")
    elif phase == "validando":
        if ph["n"] < LQ_VALID_N:
            vtxt = f"🧪 validando filtro (n={ph['n']}/{LQ_VALID_N}), tamaño base"
        else:
            vtxt = (f"🧪 filtro aún no concluyente (n={ph['n']}: {ph['mean']:+.2f}R ± {ph['se']:.2f}), tamaño base")
        out.append(f"🧮 Edge: E[R] neto {sig['mu_net']:+.2f}R (límite inf. {sig['lcb']:+.2f}R) · {vtxt}")
    elif phase == "calibrando":
        out.append(f"🧪 Calibrando modelo ({MODEL.neff:.0f}/{LQ_MIN_SAMPLES:.0f} muestras): ventaja aún no demostrada")
    out.append(f"Coste {sig['cost_pct']:.2f}% = {c:.2f}R · acierto mínimo {be:.0f}% con R:R {rr:.1f}")
    if risk_eq > 0:
        out.append(f"Riesgo sugerido: <b>{risk_eq:.2f}%</b> del capital")
    else:
        out.append("Riesgo sugerido: 0% (sin ventaja estimada)")
    return "\n".join(out) + "\n"


async def send_signal(session, sig, risk_eq, ph):
    symbol, tf = sig["symbol"], sig["tf"]
    z, closed, htf = sig["zone"], sig["_closed"], sig.get("htf")
    last = closed[-1]
    key = (symbol, tf)
    link = f'<a href="{fmt_link(symbol)}">Abrir en BingX</a>'
    risk_pct = sig["risk_pct"]
    rew_pct = abs(sig["tp"] - sig["entry"]) / sig["entry"] * 100
    zname = "BSL (liquidez arriba)" if z["kind"] == "BSL" else "SSL (liquidez abajo)"
    extra = " · extremo 20 velas" if z["ext20"] else ""
    if sig["tp_zone"]:
        tp_txt = z_name(sig["tp_zone"])
    else:
        tp_txt = "R:R empírico (auto)" if sig.get("tp_auto") else f"R:R fijo {LQ_DEFAULT_RR:g}"
    kind_txt = {"SOUP": "TURTLE SOUP", "SOUP+1": "TURTLE SOUP PLUS ONE", "REBOTE": "REBOTE EN LIQUIDEZ"}[sig["kind"]]
    emoji = "🟢" if sig["side"] == "LONG" else "🔴"
    hora = datetime.fromtimestamp(last[0] / 1000, tz=timezone.utc).strftime("%H:%M UTC")
    extras_txt = " · ".join(f"{k} {int(v):+d}" for k, v in sig["extras"].items() if v)
    extras_line = f"Extras: {extras_txt}\n" if extras_txt else ""
    sr_extra = f" · {sig['sr_txt']}" if sig["sr_txt"] else ""
    age_line = f"Longevidad: sin barrer hace {fmt_age(z['age'], tf)} ({z['age']} velas){sr_extra}\n"
    panel = build_panel(closed, sig, htf) if LQ_SHOW_PANEL else ""
    await send_telegram(session,
        f"{emoji} <b>{sig['side']} · {kind_txt}</b> · {tf}\n<b>{symbol}</b>\n"
        f"Zona: {zname} <code>{pretty(z['level'])}</code>{extra}\n"
        f"Zona {z['ztype']} · importancia <b>{z['score']:.0f}/100</b> "
        f"(toques {z['touches']} · liquidaciones x{z['ratio']:.1f})\n"
        f"{age_line}"
        f"Calidad: <b>{sig['quality']:.0f}/100</b> · RVOL {sig['rv']:.1f}x · mecha {sig['wick'] * 100:.0f}%\n"
        f"{extras_line}"
        f"Entrada: <code>{pretty(sig['entry'])}</code>\n"
        f"SL: <code>{pretty(sig['sl'])}</code> (-{risk_pct:.2f}%)\n"
        f"TP: <code>{pretty(sig['tp'])}</code> (+{rew_pct:.2f}%) → R:R <b>{sig['rr']:.1f}</b>\n"
        f"Objetivo: {tp_txt}\n"
        f"{edge_lines(sig, risk_eq, ph)}"
        f"EMA{EMA_PERIOD}: {'a favor ✅' if sig['aligned'] else 'en contra ⚠️'}\n"
        f"{panel}"
        f"Salir: TP, SL o a las {LQ_MAX_HOLD_BARS} velas · Vela {hora}\n{link}")
    POSITIONS[key] = {"side": sig["side"], "kind": sig["kind"], "entry": sig["entry"],
                      "sl": sig["sl"], "tp": sig["tp"], "ts": sig["ts"],
                      "cost_pct": sig["cost_pct"], "risk_pct": risk_pct, "risk_eq": risk_eq}


async def dispatch(session, tf, cands):
    """Elige que candidatas se envian: filtros + ranking por ventaja + limites de cartera. Registra todas para aprender."""
    if not cands:
        return 0, 0, Counter()
    ph = edge_status()
    ranked_by_edge = ph["phase"] in ("validando", "validado", "refutado")
    ok_list, why_cnt = [], Counter()
    for s in cands:
        ok, why = eligible(s, ph)
        if ok:
            ok_list.append(s)
        else:
            why_cnt[why] += 1
    ok_list.sort(key=(lambda s: s["lcb"]) if ranked_by_edge else (lambda s: s["quality"]), reverse=True)
    emitted, sent = set(), 0
    day_stop = day_r_now() <= -LQ_DAILY_STOP_R
    if day_stop and META.get("stop_msg") != _today():
        META["stop_msg"] = _today()
        await send_telegram(session, f"⛔ <b>Parada diaria</b>: {day_r_now():+.1f}R realizado hoy (UTC). "
                                     f"No se envían señales nuevas hasta mañana; el aprendizaje sigue.")
    for s in ok_list:
        sym = s["symbol"]
        if day_stop:
            why = "parada diaria"
        elif (sym, s["tf"]) in POSITIONS or any(k[0] == sym for k in POSITIONS):
            why = "moneda ya abierta"
        elif len(POSITIONS) >= LQ_MAX_OPEN:
            why = "máx. abiertas"
        elif sum(1 for p in POSITIONS.values() if p["side"] == s["side"]) >= LQ_MAX_OPEN_SIDE:
            why = "máx. por lado"
        elif sent >= LQ_MAX_PER_SCAN:
            why = "máx. por scan"
        else:
            why = None
        if why:
            why_cnt[why] += 1
            continue
        await send_signal(session, s, size_for(s, ph), ph)
        emitted.add(s["id"])
        sent += 1
    for s in cands:
        add_tracker(s, s["id"] in emitted)
    return sent, len(cands), why_cnt


async def scan(ex, session, tf, symbols):
    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.time()
    res = await asyncio.gather(*[analyze(ex, session, sem, s, tf) for s in symbols], return_exceptions=True)
    errs = [r for r in res if isinstance(r, Exception)]
    if errs:
        log.error("Errores en scan %s: %r", tf, errs[0])
    ok = [r for r in res if isinstance(r, tuple)]
    exits = sum(r[0] for r in ok)
    sent, ncand, why = await dispatch(session, tf, [r[1] for r in ok if r[1]])
    prune_trackers()
    save_state()
    log.info("[%s] %d monedas · %d salidas · %d candidatas → %d enviadas %s · abiertas %d · seguimiento %d · "
             "modelo %.0f muestras · %.1fs", tf, len(symbols), exits, ncand, sent, dict(why),
             len(POSITIONS), len(TRACK), MODEL.neff, time.time() - t0)


def model_block():
    """Estado del modelo y pruebas de que la ventaja es real (o no): fase, calibracion, IC, t-stat, PSR, RR optimo."""
    ph = edge_status()
    fase = {"off": "desactivado", "calibrando": "CALIBRANDO", "validando": "validando el filtro",
            "validado": "VALIDADO ✅", "refutado": "REFUTADO ⛔ (el bot no envía señales)"}[ph["phase"]]
    out = ["", "🧮 <b>Modelo de ventaja</b>",
           f"Muestras efectivas {MODEL.neff:.0f}/{LQ_MIN_SAMPLES:.0f} ({MODEL.n} totales) · modo {LQ_EDGE_MODE} · {fase}"]
    if ph["phase"] in ("validando", "validado", "refutado"):
        out.append(f"Filtro (lo que habría pasado, n={ph['n']}): R neta {ph['mean']:+.3f} ± {ph['se']:.3f}")
    hist = HISTORY[-1500:]
    if len(hist) < 20:
        return "\n".join(out)
    net = [h["r"] - h["cost_r"] for h in hist]
    s = edge.r_stats(net)
    out.append(f"Todas las candidatas (n={s['n']}): R neta media {s['mean']:+.3f} (IC95 {s['lo']:+.3f} a {s['hi']:+.3f})")
    em = [h["r"] - h["cost_r"] for h in hist if h["emitted"]]
    if len(em) >= 10:
        se_ = edge.r_stats(em)
        out.append(f"Enviadas (n={se_['n']}): {se_['mean']:+.3f}R · t={se_['t']:.1f} · "
                   f"prob. de ventaja real (PSR) {se_['psr'] * 100:.0f}%")
    pr = [h for h in hist if h["pred"] is not None]
    if len(pr) >= 40:
        pr.sort(key=lambda h: h["pred"])
        half = len(pr) // 2
        mean_r = lambda g: sum(h["r"] - h["cost_r"] for h in g) / len(g)
        pm = sum(h["pred"] for h in pr) / len(pr)
        out.append(f"Calibración (n={len(pr)}): predicho {pm:+.3f}R · real {mean_r(pr):+.3f}R · "
                   f"mitad alta {mean_r(pr[half:]):+.3f} vs baja {mean_r(pr[:half]):+.3f}")
    w = MODEL.weights()
    top = sorted(range(1, len(w)), key=lambda i: -abs(w[i]))[:4]
    out.append("Pesos mayores: " + ", ".join(f"{edge.FEATURE_NAMES[i]} {w[i]:+.2f}" for i in top))
    br = edge.best_rr(hist, LQ_RR_GRID, z=1.0, min_n=LQ_RR_MIN_N)
    if br:
        out.append(f"R:R óptimo empírico: {br[0]:g} (R neta {br[1]:+.3f} ± {br[2]:.3f}, n={br[3]})")
    return "\n".join(out)


# ======================= RESUMEN DIARIO =======================
def build_report(trades, date_str):
    open_n = len(POSITIONS)
    if not trades:
        return "\n".join([f"📊 <b>Liquidez · resumen {date_str}</b>", "Sin operaciones cerradas hoy.",
                          f"Posiciones abiertas: {open_n}", model_block()])
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    n = len(trades)
    total = sum(t["pnl"] for t in trades)
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    pf = (gw / gl) if gl > 0 else float("inf")
    avg_w = gw / len(wins) if wins else 0
    avg_l = -gl / len(losses) if losses else 0
    expect = total / n
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda t: t["ts"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
    rs = [t["r_net"] for t in trades if "r_net" in t]
    st = edge.r_stats(rs)
    wlo, whi = edge.wilson(len(wins), n)
    veq = vpeak = 1.0
    vdd = 0.0
    for t in sorted(trades, key=lambda t: t["ts"]):
        veq *= 1 + t.get("risk_eq", 0.0) / 100 * t.get("r_net", 0.0)
        vpeak = max(vpeak, veq)
        vdd = max(vdd, 1 - veq / vpeak)
    lines = [
        f"📊 <b>Liquidez · resumen {date_str}</b> (neto de costes)",
        f"Operaciones: <b>{n}</b> · ✅ {len(wins)} ({len(wins) / n * 100:.0f}%, IC95 {wlo * 100:.0f}-{whi * 100:.0f}%) · ❌ {len(losses)}",
        f"Media ganadora {avg_w:+.2f}% · media perdedora {avg_l:+.2f}%",
        f"Esperanza por operación: <b>{expect:+.2f}%</b> = <b>{st['mean']:+.2f}R</b> · Profit factor: <b>{pf_txt}</b>",
        f"Resultado total (suma de %): <b>{total:+.2f}%</b> · Drawdown máx: {dd:.2f}%",
        f"Curva virtual con el tamaño sugerido: <b>{(veq - 1) * 100:+.2f}%</b> del capital · DD {vdd * 100:.2f}%",
        f"Posiciones abiertas: {open_n}", "", "<b>Por tipo</b>"]
    for k in ("SOUP", "SOUP+1", "REBOTE"):
        tt = [t for t in trades if t["kind"] == k]
        if tt:
            w = sum(1 for t in tt if t["pnl"] > 0)
            lines.append(f"{k}: {len(tt)} ops · {w}G/{len(tt) - w}P · {sum(t['pnl'] for t in tt):+.2f}%")
    lines += ["", "<b>Por timeframe y lado</b>"]
    for tf in sorted({t["tf"] for t in trades}, key=tf_seconds):
        for sd in ("LONG", "SHORT"):
            tt = [t for t in trades if t["tf"] == tf and t["side"] == sd]
            if tt:
                w = sum(1 for t in tt if t["pnl"] > 0)
                lines.append(f"{tf} {sd}: {len(tt)} ops · {w}G/{len(tt) - w}P · {sum(t['pnl'] for t in tt):+.2f}%")
    best = sorted(trades, key=lambda t: -t["pnl"])[:5]
    worst = sorted(trades, key=lambda t: t["pnl"])[:5]
    fmt = lambda t: f"{'✅' if t['pnl'] > 0 else '❌'} {t['symbol'].split(':')[0]} {t['side']} {t['tf']} {t['kind']} <b>{t['pnl']:+.2f}%</b>"
    lines += ["", "<b>Mejores</b>"] + [fmt(t) for t in best] + ["", "<b>Peores</b>"] + [fmt(t) for t in worst]
    lines.append(model_block())
    return "\n".join(lines)


async def daily_report_loop(session):
    try:
        tz = ZoneInfo(REPORT_TZ)
    except Exception:
        tz = timezone.utc
    hh, mm = (int(x) for x in REPORT_TIME.split(":"))
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        trades = CLOSED[:]
        CLOSED.clear()
        try:
            await send_long(session, build_report(trades, target.strftime("%d/%m/%Y")))
        except Exception as e:
            log.error("Error resumen: %s", e)
        save_state()


# ======================= BUCLES =======================
async def tf_loop(ex, session, tf, symbols_ref):
    secs = tf_seconds(tf)
    while True:
        now = time.time()
        await asyncio.sleep((now // secs + 1) * secs + CLOSE_DELAY_SEC - now)
        try:
            await scan(ex, session, tf, symbols_ref["list"])
        except Exception as e:
            log.error("Error en scan %s: %s", tf, e)


async def refresh_symbols(ex, symbols_ref):
    while True:
        await asyncio.sleep(3600)
        try:
            symbols_ref["list"] = await get_symbols(ex)
            log.info("Lista actualizada: %d monedas", len(symbols_ref["list"]))
        except Exception as e:
            log.error("Error actualizando lista: %s", e)


async def main():
    global TG_QUEUE
    raw = edge.load_state(LQ_STATE_PATH)
    if raw.get("_error"):
        log.error("Estado corrupto en %s (%s): se arranca limpio", LQ_STATE_PATH, raw["_error"])
        raw = {}
    state_restore(raw)
    persist_ok = save_state()
    restored = bool(raw)
    ex = ccxt.bingx({"enableRateLimit": True,
                     "options": {"defaultType": "swap" if MARKET_TYPE == "swap" else "spot"}})
    TG_QUEUE = asyncio.Queue(maxsize=TG_QUEUE_MAX)
    async with aiohttp.ClientSession() as session:
        tg_task = asyncio.create_task(telegram_worker(session))
        try:
            symbols = await get_symbols(ex)
            log.info("Monedas cripto: %d (excluidas %d)", len(symbols), len(EXCLUDED))
            symbols_ref = {"list": symbols}
            tipos = [x for x, on in (("Turtle Soup", LQ_ENABLE_SOUP), ("Soup+1", LQ_ENABLE_PLUS1),
                                     ("Rebote", LQ_ENABLE_BOUNCE)) if on]
            if not persist_ok:
                pers = f"⚠️ sin persistencia ({LQ_STATE_PATH}): se pierde al reiniciar. Añade un Volume en Railway"
            elif restored:
                pers = f"estado recuperado ({len(POSITIONS)} posiciones, {len(TRACK)} en seguimiento)"
            else:
                pers = f"estado nuevo en {LQ_STATE_PATH}"
            await send_telegram(session,
                f"✅ <b>Bot de Liquidez v2</b> iniciado\nTF: {', '.join(TIMEFRAMES)} · Monedas: {len(symbols)}\n"
                f"Setups: {', '.join(tipos)}\n"
                f"Zona mín. {LQ_MIN_ZONE_SCORE:.0f}/100 · Calidad mín. {LQ_MIN_QUALITY:.0f}/100 · R:R mín. {LQ_MIN_RR:g}\n"
                f"Modelo de ventaja: {LQ_EDGE_MODE} · {MODEL.neff:.0f}/{LQ_MIN_SAMPLES:.0f} muestras"
                f"{' (calibrando)' if MODEL.neff < LQ_MIN_SAMPLES else ''} · coste máx. {LQ_MAX_COST_R:g}R\n"
                f"Persistencia: {pers}\n"
                f"Resumen diario: {REPORT_TIME} ({REPORT_TZ})")
            await asyncio.gather(refresh_symbols(ex, symbols_ref), daily_report_loop(session),
                                 *[tf_loop(ex, session, tf, symbols_ref) for tf in TIMEFRAMES])
        finally:
            save_state()
            tg_task.cancel()
            await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
