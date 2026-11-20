"""
lq_edge.py - Nucleo matematico del Bot de Liquidez v2 (sin red; se prueba offline con test_edge.py).

Ideas, todas verificables con los datos del propio bot:
  1. Coste en R. Cada operacion paga comision + deslizamiento. Con c = coste% / riesgo%:
         EV_neto (en R) = p * (RR + 1) - (1 + c)        p = prob. de llegar al TP antes que al SL
     => acierto minimo para no perder dinero: p_be = (1 + c) / (RR + 1).
     Un stop corto con comisiones fijas destruye la ventaja: por eso se filtra por c.
  2. Etiquetado de triple barrera (TP / SL / tiempo) en unidades de R. Se registra TODA candidata
     (tambien las que no se envian = "sombra") para aprender sin sesgo de seleccion.
  3. Regresion ridge bayesiana online con olvido exponencial: estima la R bruta esperada de cada
     candidata a partir de sus caracteristicas y da media + error estandar.
  4. Filtro de ventaja: solo se opera si mu_neto - z * se >= minimo (P(ventaja neta > 0) alta).
  5. Tamano: Kelly fraccional sobre el limite inferior de la ventaja, con tope.
  6. Estadistica honesta: IC de Wilson, t-stat y Probabilistic Sharpe Ratio (Bailey & Lopez de Prado).
"""
import json
import math
import os

FEATURE_NAMES = ["const", "zona", "rvol", "mecha", "profund", "soup1", "rebote", "long",
                 "ut", "linreg", "er", "tendencia", "riesgo_atr", "rr", "redondo"]
D = len(FEATURE_NAMES)


# ======================= ESTADISTICA =======================
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def wilson(w, n, z=1.96):
    """Intervalo de Wilson para una proporcion (w aciertos de n)."""
    if n <= 0:
        return 0.0, 1.0
    p = w / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def r_stats(rs):
    """Media, IC95, t-stat y PSR (prob. de que el Sharpe por operacion sea > 0) de una lista de R."""
    n = len(rs)
    out = {"n": n, "mean": 0.0, "sd": 0.0, "se": 0.0, "t": 0.0, "psr": 0.5, "lo": 0.0, "hi": 0.0}
    if n == 0:
        return out
    m = sum(rs) / n
    out["mean"] = out["lo"] = out["hi"] = m
    if n < 3:
        return out
    var = sum((r - m) ** 2 for r in rs) / (n - 1)
    sd = math.sqrt(var)
    se = sd / math.sqrt(n)
    out.update({"sd": sd, "se": se, "t": (m / se) if se > 0 else 0.0,
                "lo": m - 1.96 * se, "hi": m + 1.96 * se})
    sdp = math.sqrt(sum((r - m) ** 2 for r in rs) / n)
    if sdp > 0:
        sr = m / sdp
        skew = sum((r - m) ** 3 for r in rs) / n / sdp ** 3
        kurt = sum((r - m) ** 4 for r in rs) / n / sdp ** 4
        den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
        if den > 0:
            out["psr"] = norm_cdf(sr * math.sqrt(n - 1) / math.sqrt(den))
    return out


# ======================= ALGEBRA PEQUENA =======================
def _invert(A):
    """Inversa por Gauss-Jordan con pivoteo parcial (A pequena, definida positiva)."""
    n = len(A)
    M = [list(row) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            raise ValueError("matriz singular")
        M[c], M[p] = M[p], M[c]
        piv = M[c][c]
        M[c] = [v / piv for v in M[c]]
        for r in range(n):
            if r != c:
                f = M[r][c]
                if f != 0.0:
                    M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [row[n:] for row in M]


# ======================= MODELO =======================
class EdgeModel:
    """
    Regresion ridge bayesiana online:  y (R bruta) = w . x + ruido,  prior w ~ N(0, s2/lam).
    Olvido exponencial gamma (cripto cambia de regimen): muestras efectivas maximas ~ 1/(1-gamma).
    Estado = sumas ponderadas (S = sum x x^T, b = sum x y, syy, neff): se guarda y se restaura tal cual.
    """

    def __init__(self, gamma=0.998, lam_const=5.0, lam=40.0, prior_var=1.5, prior_n=10.0):
        self.gamma = gamma
        self.prior_var = prior_var
        self.prior_n = prior_n
        self.lam = [lam_const] + [lam] * (D - 1)
        self.S = [[0.0] * D for _ in range(D)]
        self.b = [0.0] * D
        self.syy = 0.0
        self.neff = 0.0
        self.n = 0
        self._dirty = True
        self._w = None
        self._Ainv = None

    def update(self, x, y):
        g = self.gamma
        S = self.S
        for i in range(D):
            Si, xi = S[i], x[i]
            for j in range(D):
                Si[j] = g * Si[j] + xi * x[j]
        self.b = [g * bi + xi * y for bi, xi in zip(self.b, x)]
        self.syy = g * self.syy + y * y
        self.neff = g * self.neff + 1.0
        self.n += 1
        self._dirty = True

    def _fit(self):
        A = [[self.S[i][j] + (self.lam[i] if i == j else 0.0) for j in range(D)] for i in range(D)]
        Ainv = _invert(A)
        self._w = [sum(Ainv[i][j] * self.b[j] for j in range(D)) for i in range(D)]
        self._Ainv = Ainv
        self._dirty = False

    def weights(self):
        if self._dirty:
            self._fit()
        return list(self._w)

    def sigma2(self):
        """Varianza residual estimada (con prior para pocas muestras)."""
        if self._dirty:
            self._fit()
        w = self._w
        wb = sum(wi * bi for wi, bi in zip(w, self.b))
        wsw = sum(w[i] * sum(self.S[i][j] * w[j] for j in range(D)) for i in range(D))
        rss = max(0.0, self.syy - 2 * wb + wsw)
        return max(0.5, (rss + self.prior_n * self.prior_var) / (self.neff + self.prior_n))

    def predict(self, x):
        """(mu, se_de_la_media, sigma2): R bruta esperada, su error estandar y la varianza de una operacion."""
        if self._dirty:
            self._fit()
        mu = sum(wi * xi for wi, xi in zip(self._w, x))
        q = sum(x[i] * sum(self._Ainv[i][j] * x[j] for j in range(D)) for i in range(D))
        s2 = self.sigma2()
        return mu, math.sqrt(max(0.0, s2 * q)), s2

    def to_dict(self):
        return {"gamma": self.gamma, "prior_var": self.prior_var, "prior_n": self.prior_n, "lam": self.lam,
                "S": self.S, "b": self.b, "syy": self.syy, "neff": self.neff, "n": self.n, "d": D}

    @classmethod
    def from_dict(cls, d, gamma=None):
        m = cls(gamma=gamma if gamma is not None else (d or {}).get("gamma", 0.998))
        if not d or d.get("d") != D:      # dimension distinta (cambio de caracteristicas) => modelo nuevo
            return m
        m.prior_var, m.prior_n = d.get("prior_var", m.prior_var), d.get("prior_n", m.prior_n)
        m.lam = d.get("lam", m.lam)
        m.S, m.b = d["S"], d["b"]
        m.syy, m.neff, m.n = d["syy"], d["neff"], d["n"]
        return m


def kelly_risk_pct(lcb, mu_net, sigma2, frac, cap_pct):
    """
    Riesgo (% del capital) por operacion. Maximizar E[log(1 + rho*R)] ~ rho*m - rho^2*E[R^2]/2
    => rho* = m / E[R^2], con E[R^2] = sigma2 + m^2. Se usa el LIMITE INFERIOR de la ventaja (m = lcb)
    y una fraccion (1/4 Kelly) porque el error de estimacion de m hace que Kelly completo sobreapueste.
    """
    if lcb <= 0:
        return 0.0
    f = lcb / (sigma2 + mu_net * mu_net)
    return max(0.0, min(cap_pct, 100.0 * frac * f))


# ======================= CARACTERISTICAS =======================
def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def efficiency_ratio(closes, n=30):
    """Kaufman ER: 1 = tendencia limpia, 0 = rango. Los barridos suelen funcionar peor en tendencia limpia."""
    if len(closes) < n + 1:
        return 0.0
    seg = closes[-(n + 1):]
    path = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    return abs(seg[-1] - seg[0]) / path if path > 0 else 0.0


def round_prox(level, atr):
    """Cercania (0-1) del nivel a un numero redondo (2 cifras significativas). Los stops se agrupan alli (Osler)."""
    if level <= 0 or atr <= 0:
        return 0.0
    mag = 10 ** (math.floor(math.log10(level)) - 1)
    dist = abs(level - round(level / mag) * mag)
    return max(0.0, 1.0 - dist / atr)


def build_features(sig, closed, ema200):
    """Vector de D caracteristicas (todas acotadas, ~[-1, 1]) de una candidata. Solo datos locales (sin red)."""
    z = sig["zone"]
    long_ = sig["side"] == "LONG"
    want = 1 if long_ else -1
    atr = max(sig["atr"], 1e-12)
    entry = sig["entry"]
    risk = abs(entry - sig["sl"])
    ut, lr = sig.get("ut"), sig.get("lr")
    if ut:
        ut_f = 1.0 if ut["state"] == want else (-1.0 if ut["state"] == -want else 0.0)
    else:
        ut_f = 0.0
    if lr and lr[0] != lr[1]:
        lr_f = 1.0 if ((lr[0] > lr[1]) == long_) else -1.0
    else:
        lr_f = 0.0
    trend = _clip((entry - ema200) / atr / 15.0, -1.0, 1.0) * want
    return [1.0,
            z["score"] / 100.0,
            min(sig["rv"], 6.0) / 6.0,
            min(sig["wick"], 1.0),
            min(sig.get("depth", 0.0), 3.0) / 3.0,
            1.0 if sig["kind"] == "SOUP+1" else 0.0,
            1.0 if sig["kind"] == "REBOTE" else 0.0,
            1.0 if long_ else 0.0,
            ut_f, lr_f,
            efficiency_ratio([c[4] for c in closed[-40:]]),
            trend,
            min(risk / atr, 4.0) / 4.0,
            min(sig["rr"], 5.0) / 5.0,
            round_prox(z["level"], atr)]


# ======================= RASTREADOR (triple barrera en R) =======================
def new_track(sig, x, emitted, pred_net, lcb=None):
    entry, sl = sig["entry"], sig["sl"]
    risk = abs(entry - sl)
    return {"id": f"{sig['symbol']}|{sig['tf']}|{sig['ts']}", "symbol": sig["symbol"], "tf": sig["tf"],
            "side": sig["side"], "kind": sig["kind"], "ts": sig["ts"], "last_ts": sig["ts"],
            "entry": entry, "sl": sl, "rr": round(sig["rr"], 4), "risk": risk,
            "cost_r": round(sig["cost_r"], 4), "x": [round(v, 5) for v in x],
            "emitted": bool(emitted), "pred": None if pred_net is None else round(pred_net, 4),
            "lcb": None if lcb is None else round(lcb, 4),
            "bars": 0, "mfe": 0.0, "stopped": False, "time_r": None, "done": False}


def advance_track(t, closed, max_hold):
    """
    Avanza el rastreador con las velas cerradas nuevas. Sigue hasta SL o tiempo maximo (NO se detiene en TP):
    guarda la excursion favorable maxima (MFE) antes del SL, asi se puede evaluar cualquier TP a posteriori.
    Si SL y objetivo caen en la misma vela se asume SL primero (conservador).
    """
    if t["done"]:
        return
    long_ = t["side"] == "LONG"
    entry, sl, risk = t["entry"], t["sl"], t["risk"]
    for c in closed:
        if c[0] <= t["last_ts"]:
            continue
        t["bars"] += 1
        t["last_ts"] = c[0]
        if (c[3] <= sl) if long_ else (c[2] >= sl):
            t["stopped"] = True
            t["done"] = True
            return
        fav = ((c[2] - entry) if long_ else (entry - c[3])) / risk
        if fav > t["mfe"]:
            t["mfe"] = fav
        if t["bars"] >= max_hold:
            t["time_r"] = ((c[4] - entry) if long_ else (entry - c[4])) / risk
            t["done"] = True
            return


def outcome_r(t, k):
    """R bruta (sin costes) si el objetivo hubiera estado a k*R. Sirve con rastreadores y con registros compactos."""
    if t["mfe"] >= k - 1e-9:
        return float(k)
    if t["stopped"]:
        return -1.0
    return float(t["time_r"] or 0.0)


def compact(t, y):
    """Registro compacto de una operacion cerrada (para informes, calibracion y RR optimo)."""
    return {"ts": t["last_ts"], "symbol": t["symbol"], "tf": t["tf"], "side": t["side"], "kind": t["kind"],
            "emitted": t["emitted"], "rr": t["rr"], "mfe": round(t["mfe"], 3), "stopped": t["stopped"],
            "time_r": None if t["time_r"] is None else round(t["time_r"], 3), "cost_r": t["cost_r"],
            "r": round(y, 4), "pred": t["pred"], "lcb": t.get("lcb")}


def best_rr(records, grid, z=1.0, min_n=60):
    """
    RR de salida con mejor limite inferior de R neta (contrafactual con MFE: el SL es el mismo,
    solo cambia donde esta el TP). Devuelve (rr, media_neta, se, n) o None si hay pocas muestras.
    """
    n = len(records)
    if n < min_n:
        return None
    cost = sum(r["cost_r"] for r in records) / n
    best = None
    for k in grid:
        rs = [outcome_r(r, k) for r in records]
        m = sum(rs) / n
        sd = math.sqrt(sum((v - m) ** 2 for v in rs) / (n - 1))
        se = sd / math.sqrt(n)
        lcb = m - cost - z * se
        if best is None or lcb > best[0]:
            best = (lcb, k, m - cost, se, n)
    return best[1], best[2], best[3], best[4]


def validate_selection(hist, min_lcb, max_cost_r, z=1.0, min_n=40, z_ref=0.5):
    """
    Validacion prequential del filtro: de las candidatas que el modelo HABRIA dejado pasar en su momento
    (lcb guardado ANTES de conocer el resultado; incluye 'sombra'), cuanto rindio realmente en R neta?
      validado : media > z*se      -> el filtro gana dinero fuera de muestra (exigente: z=1)
      refutado : media < -z_ref*se -> el filtro pierde: el bot debe callar (rapido: z_ref=0.5)
      validando: aun no concluyente
    Devuelve (fase, n, media_neta, se).
    """
    sel = [h["r"] - h["cost_r"] for h in hist
           if h.get("lcb") is not None and h["lcb"] >= min_lcb and h["cost_r"] <= max_cost_r]
    n = len(sel)
    if n < min_n:
        return "validando", n, (sum(sel) / n if n else 0.0), 0.0
    s = r_stats(sel)
    if s["mean"] > z * s["se"]:
        return "validado", n, s["mean"], s["se"]
    if s["mean"] < -z_ref * s["se"]:
        return "refutado", n, s["mean"], s["se"]
    return "validando", n, s["mean"], s["se"]


# ======================= PERSISTENCIA =======================
def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:                      # archivo corrupto: se arranca limpio pero se avisa
        return {"_error": str(e)}


def save_state(path, state):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, path)
