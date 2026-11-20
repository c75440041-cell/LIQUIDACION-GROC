"""
Prueba de integracion offline: exchange falso con datos sinteticos + scan() real del bot.
Comprueba que el flujo completo no revienta, que aprende, persiste y restaura, y que las fases funcionan.
NO mide rentabilidad (los datos son aleatorios): solo corrección de la maquinaria.
    python test_integration.py [pasos] [simbolos]
"""
import asyncio
import math
import os
import random
import sys
import tempfile
import time as _time

import liquidez_bot as lb

STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 350
NSYM = int(sys.argv[2]) if len(sys.argv) > 2 else 10
TF = "5m"
TF_MS = 300000
random.seed(11)
NOW = [0.0]


class Shim:
    def time(self):
        return NOW[0]


lb.time = Shim()
tmp = tempfile.mkdtemp()
lb.LQ_STATE_PATH = os.path.join(tmp, "sub", "lq_state.json")      # crea el subdirectorio
lb.LQ_MIN_SAMPLES = 25.0                                          # para ver la transicion de fases rapido
lb.LQ_VALID_N = 15
lb.MIN_VOLUME_USDT = 0
lb.TELEGRAM_TOKEN, lb.TELEGRAM_CHAT_ID = 'x', 'y'      # para que send_telegram encole (no se envia nada)
# umbrales laxos SOLO para que los datos aleatorios generen suficientes candidatas
lb.LQ_VOL_MULT, lb.LQ_MIN_WICK, lb.LQ_MIN_QUALITY = 1.0, 0.2, 45.0
lb.LQ_SHADOW_MIN_QUALITY, lb.LQ_MIN_ZONE_SCORE, lb.LQ_MAX_COST_R = 30.0, 35.0, 0.6
import logging
lb.log.setLevel(logging.WARNING)


def gen_series(n, base, seed):
    rnd = random.Random(seed)
    t0 = 1_700_000_000_000 - (1_700_000_000_000 % TF_MS)
    out, p, vol_state = [], base, 1.0
    for i in range(n):
        vol_state = 0.95 * vol_state + 0.05 * rnd.lognormvariate(0, 0.6)
        sig = 0.0035 * vol_state
        o = p
        steps = [rnd.gauss(0, sig) for _ in range(4)]
        path = [o]
        for s in steps:
            path.append(path[-1] * (1 + s))
        c = path[-1]
        h = max(path) * (1 + abs(rnd.gauss(0, sig * 0.6)))
        l = min(path) * (1 - abs(rnd.gauss(0, sig * 0.6)))
        v = 1000 * rnd.lognormvariate(0, 0.5) * (4 if rnd.random() < 0.06 else 1)
        out.append([t0 + i * TF_MS, o, h, l, c, v])
        p = c
    return out


class FakeEx:
    def __init__(self, data):
        self.data, self.i = data, 0
        self.calls = 0

    async def fetch_ohlcv(self, symbol, tf, since=None, limit=500):
        self.calls += 1
        d = self.data.get(symbol)
        if d is None:
            raise RuntimeError("sin datos")
        step = {"5m": 1, "15m": 3, "1h": 12, "4h": 48, "1d": 288, "1w": 2016}[tf]
        i = self.i
        if step == 1:
            rows = d[max(0, i - limit + 1): i + 2]                  # incluye la vela en formacion (i+1)
        else:                                                       # TF superiores: agrego por bloques
            rows = []
            for k in range(i - (i % step) - step * (limit - 1), i + 1, step):
                blk = d[max(0, k): k + step]
                if len(blk) == step and k >= 0:
                    rows.append([blk[0][0], blk[0][1], max(b[2] for b in blk), min(b[3] for b in blk), blk[-1][4],
                                 sum(b[5] for b in blk)])
        return rows[-limit:]


async def run():
    syms = [f"S{j}/USDT:USDT" for j in range(NSYM)]
    data = {s: gen_series(STEPS + 600, 10 * (j + 1), 100 + j) for j, s in enumerate(syms)}
    for s in syms:
        lb.SYMBOL_VOL[s] = 5e6 * (1 + random.random() * 10)
    ex = FakeEx(data)
    lb.TG_QUEUE = asyncio.Queue(maxsize=2000)
    t0 = _time.time()
    first = 560
    phases = []
    for step in range(STEPS):
        ex.i = first + step
        NOW[0] = (data[syms[0]][ex.i][0] + TF_MS) / 1000 + 3
        await lb.scan(ex, None, TF, syms)
        if step % 50 == 0:
            phases.append((step, lb.edge_status()["phase"], round(lb.MODEL.neff, 1), len(lb.TRACK), len(lb.HISTORY)))
    dt = _time.time() - t0
    return ex, dt, phases


ex, dt, phases = asyncio.run(run())
msgs = []
while not lb.TG_QUEUE.empty():
    msgs.append(lb.TG_QUEUE.get_nowait()[1])
print(f"scan x{STEPS} con {NSYM} monedas: {dt:.1f}s · {ex.calls} llamadas · mensajes {len(msgs)}")
print("fases (paso, fase, neff, seguimiento, historial):")
for p in phases:
    print("  ", p)

assert lb.MODEL.n > 0 and len(lb.HISTORY) > 0, "no aprendio nada: ninguna candidata se resolvio"
assert all(abs(h["r"]) < 10 for h in lb.HISTORY), "R fuera de rango"
assert os.path.exists(lb.LQ_STATE_PATH), "no persistio el estado"
print(f"candidatas resueltas: {len(lb.HISTORY)} · modelo n={lb.MODEL.n} neff={lb.MODEL.neff:.1f} · "
      f"posiciones abiertas {len(lb.POSITIONS)} · cerradas sin informar {len(lb.CLOSED)}")

# ---- muestra de mensajes ----
sig_msgs = [m for m in msgs if ("LONG ·" in m or "SHORT ·" in m) and "SALIR" not in m]
exit_msgs = [m for m in msgs if "SALIR" in m]
print(f"señales {len(sig_msgs)} · salidas {len(exit_msgs)}")
if sig_msgs:
    print("---- ejemplo de señal ----\n" + sig_msgs[-1][:1800])
if exit_msgs:
    print("---- ejemplo de salida ----\n" + exit_msgs[-1])

# ---- consistencia: cada R de HISTORY coincide con el contrafactual ----
for h in lb.HISTORY[:500]:
    r = lb.edge.outcome_r(h, h["rr"])
    assert abs(r - h["r"]) < 1e-3, (h, r)

# ---- informe ----
rep = lb.build_report(lb.CLOSED[:] if lb.CLOSED else [], "20/11/2026")
print("---- informe ----\n" + rep[:2500])
assert "Modelo de ventaja" in rep

# ---- persistencia: guardar, vaciar, restaurar ----
n0, neff0, pos0, tr0, hist0 = lb.MODEL.n, lb.MODEL.neff, len(lb.POSITIONS), len(lb.TRACK), len(lb.HISTORY)
assert lb.save_state()
raw = lb.edge.load_state(lb.LQ_STATE_PATH)
lb.POSITIONS.clear(); lb.TRACK.clear(); lb.HISTORY.clear(); lb.CLOSED.clear()
lb.MODEL = lb.edge.EdgeModel()
lb.state_restore(raw)
assert (lb.MODEL.n, len(lb.POSITIONS), len(lb.TRACK), len(lb.HISTORY)) == (n0, pos0, tr0, hist0), "restauracion incompleta"
assert abs(lb.MODEL.neff - neff0) < 1e-9
print(f"persistencia OK: modelo n={lb.MODEL.n}, posiciones {pos0}, seguimiento {tr0}, historial {hist0}")

# ---- fases: forzar refutado y comprobar que calla; forzar validado y comprobar Kelly ----
bad = [{"r": -1.0, "lcb": 0.5, "cost_r": 0.1, "kind": "SOUP", "side": "LONG"}] * 80
good = [{"r": 2.0, "lcb": 0.5, "cost_r": 0.1, "kind": "SOUP", "side": "LONG"}] * 40 + \
       [{"r": -1.0, "lcb": 0.5, "cost_r": 0.1, "kind": "SOUP", "side": "LONG"}] * 40
lb.MODEL.neff = 100.0
lb.HISTORY[:] = bad
ph = lb.edge_status()
assert ph["phase"] == "refutado", ph
sig = {"quality": 90, "htf": None, "cost_r": 0.1, "lcb": 0.3}
assert lb.eligible(sig, ph) == (False, "modelo refutado")
lb.HISTORY[:] = good
ph = lb.edge_status()
assert ph["phase"] == "validado", ph
assert lb.eligible(sig, ph)[0] and lb.eligible(dict(sig, lcb=-0.1), ph) == (False, "sin ventaja")
assert lb.eligible(dict(sig, cost_r=0.9), ph) == (False, "coste/riesgo")
s2 = dict(sig, mu_net=0.4, s2=1.5)
assert lb.size_for(s2, ph) > 0 and lb.size_for(s2, {"phase": "validando"}) == lb.LQ_BASE_RISK_PCT
print("fases OK: refutado calla · validado habilita Kelly · validando usa tamaño base")
print("\nINTEGRACION OK")
