"""
backtest.py - Investigacion OFFLINE walk-forward del Bot de Liquidez (usa exactamente detect_signal y la
misma triple barrera que el bot en vivo). Solo lectura de datos publicos: no necesita claves.

    python backtest.py --tf 15m --days 60 --symbols 40 --out lq_state_seed.json
    python backtest.py --synthetic 12            # autotest con datos aleatorios (sin red)

Que hace:
  1. Descarga historico de BingX (ccxt) de las monedas mas liquidas.
  2. Reproduce el detector vela a vela -> candidatas. Cada una se resuelve con triple barrera en R.
  3. Evaluacion PREQUENTIAL: el modelo solo aprende de operaciones YA resueltas en el momento de decidir
     (sin mirar al futuro) y decide con el mismo filtro del bot (coste, calidad, lcb, validacion).
  4. Compara: A) todas las candidatas · B) filtro heuristico (calidad + coste) · C) filtro completo v2.
     Muestra R neta, IC95, t-stat, PSR y la mitad temporal 1 vs 2 (estabilidad).
  5. --out guarda un estado semilla (modelo + historial) para copiarlo al volumen de Railway y acortar
     la calibracion. Ojo: el backtest NO incluye bonus de tendencia en TF superiores ni limites de cartera.

Advertencia de honestidad: un backtest con muchos parametros ajustados sobre el mismo periodo sobreestima
la ventaja (Bailey & Lopez de Prado). Cuenta cuantas configuraciones pruebas y exige que la mitad 2 aguante.
"""
import argparse
import asyncio
import json
import random
import time

import ccxt.async_support as ccxt

import liquidez_bot as lb
import lq_edge as edge


# ---------------------------------------------------------------- datos
async def download(ex, symbol, tf, days):
    tf_ms = lb.tf_seconds(tf) * 1000
    now = int(time.time() * 1000)
    since = now - days * 86400 * 1000
    rows, last = [], None
    while True:
        chunk = await ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not chunk:
            break
        for c in chunk:
            if last is None or c[0] > last:
                rows.append(c)
                last = c[0]
        nxt = chunk[-1][0] + tf_ms
        if nxt <= since or chunk[-1][0] >= now - tf_ms:
            break
        since = nxt
    return [c for c in rows if c[0] + tf_ms <= now]


def synthetic(n, seed, tf_ms):
    rnd = random.Random(seed)
    t0 = 1_700_000_000_000 - (1_700_000_000_000 % tf_ms)
    out, p, vs = [], 10.0 * (1 + seed % 7), 1.0
    for i in range(n):
        vs = 0.95 * vs + 0.05 * rnd.lognormvariate(0, 0.6)
        sg = 0.0035 * vs
        path = [p]
        for _ in range(4):
            path.append(path[-1] * (1 + rnd.gauss(0, sg)))
        h = max(path) * (1 + abs(rnd.gauss(0, sg * 0.6)))
        l = min(path) * (1 - abs(rnd.gauss(0, sg * 0.6)))
        v = 1000 * rnd.lognormvariate(0, 0.5) * (4 if rnd.random() < 0.06 else 1)
        out.append([t0 + i * tf_ms, path[0], h, l, path[-1], v])
        p = path[-1]
    return out


# ---------------------------------------------------------------- repeticion del detector
def replay(symbol, data, tf, cost_pct):
    """Candidatas de una moneda, con resultado (triple barrera) ya resuelto. Mismo criterio 'una sombra abierta'."""
    out, n, i = [], len(data), max(lb.CANDLES // 2, 210)
    maxh = lb.LQ_MAX_HOLD_BARS
    while i < n - 2:
        v = [c[5] or 0 for c in data[:i + 1]]
        base = v[i - 51:i - 1]
        avg = sum(base) / len(base) if base else 0
        if avg <= 0 or max(v[i], v[i - 1]) < lb.LQ_VOL_MULT * avg:      # el detector exige este volumen minimo
            i += 1
            continue
        window = data[max(0, i - lb.CANDLES + 1):i + 1]
        ema200 = lb.calc_ema_last([c[4] for c in window], lb.EMA_PERIOD)
        sig = lb.detect_signal(window, ema200) if ema200 else None
        if not sig:
            i += 1
            continue
        sig.update({"symbol": symbol, "tf": tf})
        sig["risk_pct"] = abs(sig["sl"] - sig["entry"]) / sig["entry"] * 100
        sig["cost_pct"] = cost_pct
        sig["cost_r"] = cost_pct / sig["risk_pct"]
        x = edge.build_features(sig, window, ema200)
        t = edge.new_track(sig, x, False, None)
        edge.advance_track(t, data[i + 1:i + 2 + maxh], maxh)
        if not t["done"]:
            break                                                        # sin futuro suficiente
        out.append({"ts": sig["ts"], "res_ts": t["last_ts"], "t": t, "quality": sig["quality"], "x": x,
                    "cost_r": sig["cost_r"], "rr": sig["rr"], "side": sig["side"], "kind": sig["kind"],
                    "symbol": symbol})
        i += max(1, t["bars"]) + 1
    return out


# ---------------------------------------------------------------- evaluacion prequential
def summarize(name, rs, wins=None):
    st = edge.r_stats(rs)
    if not rs:
        return f"{name:<34} n=0"
    w = sum(1 for r in rs if r > 0)
    lo, hi = edge.wilson(w, len(rs))
    gw = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r <= 0)
    pf = gw / gl if gl > 0 else float("inf")
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return (f"{name:<34} n={st['n']:<5} acierto {w / len(rs) * 100:4.1f}% [{lo * 100:.0f}-{hi * 100:.0f}] · "
            f"R neta {st['mean']:+.3f} [{st['lo']:+.3f},{st['hi']:+.3f}] · t={st['t']:+.2f} · PSR {st['psr'] * 100:3.0f}% · "
            f"PF {pf:.2f} · total {sum(rs):+.1f}R · DD {dd:.1f}R")


def evaluate(samples):
    samples.sort(key=lambda s: s["ts"])
    model = edge.EdgeModel(gamma=lb.LQ_MODEL_GAMMA)
    pending, hist = [], []
    A, B, C = [], [], []
    for s in samples:
        still = []
        for p in pending:                       # el modelo aprende SOLO de lo ya resuelto en este instante
            if p["res_ts"] <= s["ts"]:
                y = edge.outcome_r(p["t"], p["rr"])
                model.update(p["x"], y)
                hist.append(edge.compact(dict(p["t"], lcb=p["lcb"], pred=p["pred"]), y))
            else:
                still.append(p)
        pending = still
        mu, se, s2 = model.predict(s["x"])
        s["pred"], s["lcb"] = mu - s["cost_r"], mu - s["cost_r"] - lb.LQ_EDGE_Z * se
        t = s["t"]
        net = edge.outcome_r(t, s["rr"]) - s["cost_r"]
        A.append((s["ts"], net))
        heur = s["quality"] >= lb.LQ_MIN_QUALITY - 4 and s["cost_r"] <= lb.LQ_MAX_COST_R
        if heur:
            B.append((s["ts"], net))
            if model.neff < lb.LQ_MIN_SAMPLES:
                C.append((s["ts"], net))        # calibrando: emite como el bot (tamano base)
            else:
                phase = edge.validate_selection(hist[-1500:], lb.LQ_EDGE_MIN_R, lb.LQ_MAX_COST_R,
                                                z=lb.LQ_VALID_Z, min_n=lb.LQ_VALID_N, z_ref=lb.LQ_REFUTE_Z)[0]
                if phase != "refutado" and s["lcb"] >= lb.LQ_EDGE_MIN_R:
                    C.append((s["ts"], net))
        pending.append(s)
    for p in pending:                            # al final aprende de lo que quedo pendiente (para la semilla)
        y = edge.outcome_r(p["t"], p["rr"])
        model.update(p["x"], y)
        hist.append(edge.compact(dict(p["t"], lcb=p["lcb"], pred=p["pred"]), y))
    return model, hist, A, B, C


def halves(lst):
    h = len(lst) // 2
    return [r for _, r in lst[:h]], [r for _, r in lst[h:]]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--symbols", type=int, default=30)
    ap.add_argument("--out", default="")
    ap.add_argument("--synthetic", type=int, default=0, help="N monedas aleatorias (autotest sin red)")
    a = ap.parse_args()
    tf_ms = lb.tf_seconds(a.tf) * 1000
    samples = []
    if a.synthetic:
        lb.LQ_VOL_MULT, lb.LQ_MIN_WICK, lb.LQ_MIN_QUALITY = 1.0, 0.2, 45.0
        lb.LQ_SHADOW_MIN_QUALITY, lb.LQ_MIN_ZONE_SCORE, lb.LQ_MAX_COST_R = 30.0, 35.0, 0.6
        for j in range(a.synthetic):
            sym = f"S{j}/USDT:USDT"
            lb.SYMBOL_VOL[sym] = 8e6
            samples += replay(sym, synthetic(2600, 100 + j, tf_ms), a.tf, lb.trade_cost_pct(sym))
    else:
        ex = ccxt.bingx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        try:
            syms = await lb.get_symbols(ex)
            syms.sort(key=lambda s: -lb.SYMBOL_VOL.get(s, 0))
            syms = syms[:a.symbols]
            print(f"{len(syms)} monedas, {a.tf}, {a.days} dias")
            for k, sym in enumerate(syms, 1):
                try:
                    data = await download(ex, sym, a.tf, a.days)
                except Exception as e:
                    print(f"  {sym}: error descarga {e}")
                    continue
                got = replay(sym, data, a.tf, lb.trade_cost_pct(sym)) if len(data) > 300 else []
                samples += got
                print(f"  [{k}/{len(syms)}] {sym}: {len(data)} velas -> {len(got)} candidatas")
        finally:
            await ex.close()
    if len(samples) < 30:
        print("Muy pocas candidatas:", len(samples))
        return
    model, hist, A, B, C = evaluate(samples)
    print(f"\n=== {len(samples)} candidatas · coste medio {sum(s['cost_r'] for s in samples) / len(samples):.2f}R "
          f"· R:R medio {sum(s['rr'] for s in samples) / len(samples):.2f} ===")
    for name, lst in (("A) todas las candidatas", A), ("B) calidad + coste (heuristico)", B),
                      ("C) filtro completo v2 (modelo)", C)):
        print(summarize(name, [r for _, r in lst]))
        h1, h2 = halves(lst)
        if len(h1) > 5 and len(h2) > 5:
            print(summarize("     mitad 1", h1))
            print(summarize("     mitad 2 (fuera de muestra)", h2))
    be = edge.best_rr(hist, lb.LQ_RR_GRID, z=1.0, min_n=60)
    if be:
        print(f"\nR:R optimo empirico: {be[0]:g} (R neta {be[1]:+.3f} +- {be[2]:.3f}, n={be[3]})")
    w = model.weights()
    top = sorted(range(1, len(w)), key=lambda i: -abs(w[i]))[:5]
    print("Pesos mayores:", ", ".join(f"{edge.FEATURE_NAMES[i]} {w[i]:+.2f}" for i in top))
    if a.out:
        state = {"v": 2, "positions": {}, "cooldown": {}, "closed": [], "track": [],
                 "history": hist[-lb.LQ_HISTORY_MAX:], "model": model.to_dict(), "meta": {}}
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(state, f, separators=(",", ":"))
        print(f"Semilla guardada en {a.out} (copiala al volumen como LQ_STATE_PATH)")


if __name__ == "__main__":
    asyncio.run(main())
