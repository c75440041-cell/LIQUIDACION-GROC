"""Pruebas offline de lq_edge.py:  python test_edge.py"""
import math
import random

import lq_edge as edge

random.seed(7)
ok = 0


def check(cond, msg):
    global ok
    assert cond, "FALLO: " + msg
    ok += 1
    print("  ok -", msg)


print("== algebra ==")
A = [[4.0, 1.0, 0.5], [1.0, 3.0, 0.2], [0.5, 0.2, 2.0]]
Ai = edge._invert(A)
I = [[sum(A[i][k] * Ai[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
check(all(abs(I[i][j] - (1.0 if i == j else 0.0)) < 1e-9 for i in range(3) for j in range(3)), "inversa A*A^-1 = I")

print("== estadistica ==")
lo, hi = edge.wilson(50, 100)
check(0.40 < lo < 0.41 and 0.59 < hi < 0.60, f"Wilson 50/100 -> ({lo:.3f}, {hi:.3f})")
check(edge.wilson(0, 0) == (0.0, 1.0), "Wilson n=0")
rs = [2.0] * 40 + [-1.0] * 60                       # p=0.4, RR=2 -> E[R]=+0.2
s = edge.r_stats(rs)
check(abs(s["mean"] - 0.2) < 1e-9 and s["t"] > 0 and 0.5 < s["psr"] < 1, f"r_stats mean={s['mean']:.2f} t={s['t']:.2f} psr={s['psr']:.2f}")
rs0 = [random.choice([2.0, -1.0, -1.0]) for _ in range(4000)]          # sin ventaja: E[R]=0
s0 = edge.r_stats(rs0)
check(abs(s0["t"]) < 3.5, f"sin ventaja -> t={s0['t']:.2f} (|t|<3.5)")

print("== modelo: aprende una relacion lineal conocida ==")
m = edge.EdgeModel(gamma=1.0, lam=1.0, lam_const=1.0)
w_true = [0.0] * edge.D
w_true[1], w_true[10] = 0.8, -0.6                     # zona (+), ER (-)
for _ in range(3000):
    x = [1.0] + [random.random() for _ in range(edge.D - 1)]
    y = sum(a * b for a, b in zip(w_true, x)) + random.gauss(0, 1.0)
    m.update(x, y)
w = m.weights()
check(abs(w[1] - 0.8) < 0.15 and abs(w[10] + 0.6) < 0.15, f"pesos recuperados zona={w[1]:.2f} er={w[10]:.2f}")
x0 = [1.0] + [0.5] * (edge.D - 1)
mu, se, s2 = m.predict(x0)
check(se < 0.1 and 0.8 < s2 < 1.2, f"se={se:.3f} sigma2={s2:.2f}")
m2 = edge.EdgeModel(gamma=1.0)
mu2, se2, _ = m2.predict(x0)
check(se2 > se * 3 and abs(mu2) < 1e-12, f"modelo sin datos: mu=0 y mucha incertidumbre (se={se2:.2f})")

print("== modelo: ruido puro no genera 'ventaja' ==")
m3 = edge.EdgeModel(gamma=0.998)
z_hi = 0
for _ in range(800):
    x = [1.0] + [random.random() for _ in range(edge.D - 1)]
    m3.update(x, random.choice([2.0, -1.0, -1.0]))     # R aleatoria de media 0
cnt = 0
for _ in range(500):
    x = [1.0] + [random.random() for _ in range(edge.D - 1)]
    mu, se, _ = m3.predict(x)
    if mu - 0.15 - 0.25 * se >= 0:                      # coste 0.15R, z=0.25
        cnt += 1
check(cnt < 60, f"con R~0 y coste 0.15R el filtro deja pasar {cnt}/500 (<12%): por eso existe la validacion prequential")

print("== validacion prequential del filtro ==")
def mk(r, lcb=0.1, cost=0.15):
    return {"r": r, "lcb": lcb, "cost_r": cost}
good = [mk(random.choice([2.0, -1.0]) if random.random() < 0.5 else 2.0) for _ in range(200)]   # E[R] alto
bad = [mk(random.choice([2.0, -1.0, -1.0, -1.0])) for _ in range(200)]                           # E[R]=-0.25 bruto
ph, n, mean, se = edge.validate_selection(good, 0.0, 0.25)
check(ph == "validado" and n == 200, f"filtro con ventaja real -> {ph} (media {mean:+.2f})")
ph, n, mean, se = edge.validate_selection(bad, 0.0, 0.25)
check(ph == "refutado", f"filtro que pierde -> {ph} (media {mean:+.2f})")
ph, n, _, _ = edge.validate_selection(good[:10], 0.0, 0.25)
check(ph == "validando" and n == 10, "pocas muestras -> validando")
ph, n, _, _ = edge.validate_selection([mk(5.0, lcb=-0.2)] * 100, 0.0, 0.25)
check(n == 0, "solo cuenta lo que el filtro habria dejado pasar (lcb >= minimo)")
ph, n, _, _ = edge.validate_selection([mk(5.0, cost=0.9)] * 100, 0.0, 0.25)
check(n == 0, "excluye candidatas con coste/riesgo por encima del maximo")

print("== persistencia del modelo ==")
d = m.to_dict()
m4 = edge.EdgeModel.from_dict(d)
check(abs(m4.predict(x0)[0] - m.predict(x0)[0]) < 1e-9, "to_dict/from_dict conserva predicciones")
check(edge.EdgeModel.from_dict({"d": 3}).n == 0, "dimension distinta -> modelo nuevo")

print("== Kelly ==")
check(edge.kelly_risk_pct(-0.1, -0.05, 1.5, 0.25, 1.0) == 0.0, "sin ventaja -> 0%")
k = edge.kelly_risk_pct(0.10, 0.15, 1.5, 0.25, 1.0)
check(0 < k <= 1.0, f"Kelly 1/4 con lcb=0.10R -> {k:.2f}% (tope 1%)")
check(edge.kelly_risk_pct(5.0, 5.0, 1.5, 0.25, 1.0) == 1.0, "respeta el tope")

print("== rastreador triple barrera ==")
TF = 300000


def candle(i, o, h, l, c, v=100.0):
    return [i * TF, o, h, l, c, v]


sig = {"symbol": "X/USDT:USDT", "tf": "5m", "side": "LONG", "kind": "SOUP", "ts": 0, "entry": 100.0, "sl": 99.0,
       "rr": 2.0, "cost_r": 0.15}
# LONG: sube a 102.5 (MFE 2.5R) y luego cae al SL
t = edge.new_track(sig, [1.0] * edge.D, True, 0.1)
edge.advance_track(t, [candle(1, 100, 101, 99.5, 100.5), candle(2, 100.5, 102.5, 100.4, 102), candle(3, 102, 102, 98.9, 99)], 48)
check(t["done"] and t["stopped"] and abs(t["mfe"] - 2.5) < 1e-9, f"MFE={t['mfe']:.2f} y luego SL")
check(edge.outcome_r(t, 2.0) == 2.0 and edge.outcome_r(t, 3.0) == -1.0, "TP a 2R gana; TP a 3R pierde (contrafactual)")
# SL y TP en la misma vela -> SL primero
t = edge.new_track(sig, [1.0] * edge.D, True, None)
edge.advance_track(t, [candle(1, 100, 103, 98.5, 100)], 48)
check(t["stopped"] and edge.outcome_r(t, 2.0) == -1.0, "misma vela SL+TP -> SL (conservador)")
# salida por tiempo
t = edge.new_track(sig, [1.0] * edge.D, False, None)
edge.advance_track(t, [candle(i, 100, 100.6, 99.8, 100.4) for i in range(1, 6)], 5)
check(t["done"] and not t["stopped"] and abs(t["time_r"] - 0.4) < 1e-9, f"salida por tiempo R={t['time_r']:.2f}")
# avance incremental no repite velas
t = edge.new_track(sig, [1.0] * edge.D, False, None)
cs = [candle(i, 100, 100.3, 99.9, 100.1) for i in range(1, 4)]
edge.advance_track(t, cs, 48)
edge.advance_track(t, cs, 48)
check(t["bars"] == 3, "no cuenta dos veces la misma vela")
# SHORT
sigs = dict(sig, side="SHORT", sl=101.0)
t = edge.new_track(sigs, [1.0] * edge.D, True, None)
edge.advance_track(t, [candle(1, 100, 100.4, 97.9, 98), candle(2, 98, 101.2, 97.5, 101)], 48)
check(t["stopped"] and abs(t["mfe"] - 2.1) < 1e-9, f"SHORT MFE={t['mfe']:.2f}")

print("== RR optimo contrafactual ==")
recs = []
for _ in range(400):                                   # MFE exponencial: P(MFE>=k) = exp(-k/1.2)
    mfe = random.expovariate(1 / 1.2)
    recs.append({"mfe": mfe, "stopped": True, "time_r": None, "cost_r": 0.1})
br = edge.best_rr(recs, [1, 1.5, 2, 3, 4])
check(br is not None and br[0] in (1, 1.5, 2), f"mejor RR razonable con colas finas: {br[0]}")
check(edge.best_rr(recs[:10], [1, 2]) is None, "pocas muestras -> None")

print("== caracteristicas ==")
closed = [[i * TF, 100 + i * 0.01, 100.5 + i * 0.01, 99.5 + i * 0.01, 100 + i * 0.01, 10.0] for i in range(60)]
z = {"score": 70.0, "level": 100.0, "kind": "BSL"}
sg = {"zone": z, "side": "SHORT", "atr": 0.8, "entry": 100.2, "sl": 100.9, "rr": 2.0, "rv": 2.5, "wick": 0.6,
      "depth": 0.3, "kind": "SOUP", "ut": {"state": -1}, "lr": (99.0, 100.0)}
x = edge.build_features(sg, closed, 99.0)
check(len(x) == edge.D and all(-1.0001 <= v <= 1.0001 for v in x), "vector acotado de dimension D")
check(x[8] == 1.0 and x[9] == 1.0, "UT y LinReg a favor del SHORT")
check(edge.efficiency_ratio(closed[-40:] and [c[4] for c in closed]) > 0.99, "ER ~1 en tendencia lineal")
check(edge.round_prox(100.0, 1.0) == 1.0 and edge.round_prox(67432.0, 100.0) == 0.0, "proximidad a numero redondo")

print(f"\nTODO OK ({ok} comprobaciones)")
