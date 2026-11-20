# Bot de Liquidez v2 (independiente) - BingX -> Telegram

Detecta barridos de liquidez (Turtle Soup, Soup+1, Rebote) y, desde la v2, **mide su propia ventaja
estadística** antes de enviar una señal. Nada de esto garantiza beneficios: el objetivo es que el bot
solo opere cuando los números dicen que compensa, y que **calle cuando no**.

## Qué cambia en la v2 (y por qué)

| Problema de la v1 | Solución v2 |
|---|---|
| La "calidad 0-100" era una suma de pesos a mano, no una probabilidad | Modelo bayesiano online que estima la **R esperada** de cada candidata |
| Comisión fija 0,1 % sin deslizamiento: stops cortos parecían rentables | **Coste en R** = (comisión + deslizamiento) / riesgo. Se descarta si supera `LQ_MAX_COST_R` |
| Solo se aprendía de lo enviado (sesgo de selección) | Toda candidata, enviada o no ("sombra"), se sigue con triple barrera y entrena el modelo |
| Nadie comprobaba si el filtro funciona | **Validación prequential**: el filtro debe ganar con sus propias predicciones pasadas o el bot calla |
| Tamaño fijo | **Kelly fraccional** (1/4) sobre el límite inferior de la ventaja, con tope |
| Señales correlacionadas (altcoins) | Límites de cartera, un solo TF por moneda, ranking por ventaja, parada diaria |
| Estado en memoria: un reinicio borraba posiciones y resumen | Estado persistente en JSON (Volume de Railway) |

## La matemática en 6 líneas

1. Con `c = coste% / riesgo%` y objetivo `RR`: `EV_neto = p·(RR+1) − (1+c)`. Acierto mínimo: `p_be = (1+c)/(RR+1)`.
   Con un stop de 0,4 % y 0,14 % de costes, `c = 0,35R` y a RR 2 necesitas acertar >45 %, no 33 %.
2. Etiquetado de triple barrera en R (TP / SL / tiempo); SL y TP en la misma vela = SL (conservador).
3. Regresión ridge bayesiana online con olvido exponencial (γ=0,998 ≈ 500 muestras efectivas): `R_bruta ~ w·x`.
   Devuelve media y error estándar. Prior escéptico: sin datos, ventaja = 0 y se paga el coste.
4. Se envía si `mu_neto − z·se ≥ mínimo` (por defecto z = 0,25). Se ordena por ese límite inferior.
5. Tamaño: `ρ = frac · LCB / (σ² + μ²)` (Kelly aproximado sobre el límite inferior), tope `LQ_MAX_RISK_EQUITY_PCT`.
6. Honestidad estadística: IC de Wilson, t-stat y *Probabilistic Sharpe Ratio* (Bailey & López de Prado).

Características del modelo (todas locales, sin red): calidad de zona, volumen relativo, mecha, profundidad del
barrido, tipo de setup, lado, UT Bot, LinReg, **eficiencia de Kaufman** (tendencia limpia vs rango),
distancia a EMA200 en la dirección de la operación, riesgo en ATR, R:R y **cercanía a número redondo**
(los stops se agrupan junto a números redondos: Osler, *Journal of Finance* 2003).

## Fases del modelo (verás una etiqueta en cada señal)

- **Calibrando** (`< LQ_MIN_SAMPLES` muestras efectivas): envía por heurística, marcado 🧪, tamaño base 0,25 %.
- **Validando**: filtra por ventaja, tamaño base, hasta tener `LQ_VALID_N` candidatas "que habrían pasado".
- **Validado ✅**: lo que el filtro dejaba pasar ganó de verdad (media neta > 1·se). Solo aquí se usa Kelly.
- **Refutado ⛔**: perdió (media neta < −0,5·se). **El bot deja de enviar señales** y sigue aprendiendo.

## Despliegue en Railway

Segundo servicio sobre el mismo repo (como antes). Archivos nuevos: `lq_edge.py` (junto a `liquidez_bot.py`).
`requirements.txt` no cambia. **Añade un Volume** al servicio (p. ej. montado en `/data`): Railway define
`RAILWAY_VOLUME_MOUNT_PATH` y el bot guarda ahí `lq_state.json`. Sin Volume funciona, pero el estado se
pierde en cada redeploy (el mensaje de inicio lo avisa).

Variables nuevas: ver `.env.liquidez.example`. Las más importantes: `LQ_MAX_COST_R`, `LQ_EDGE_MODE`
(`gate` / `label` solo muestra / `off`), `LQ_MIN_SAMPLES`, `LQ_MAX_OPEN_SIDE`, `LQ_DAILY_STOP_R`.
Recomendado: subir `MIN_VOLUME_USDT` a 3-5 M (el deslizamiento de pares ilíquidos se come la ventaja).

## Investigación offline y pruebas

```
python test_edge.py                       # 31 pruebas del núcleo matemático (sin red)
python test_integration.py 1100 30        # scan() completo con exchange simulado
python backtest.py --tf 15m --days 60 --symbols 40 --out lq_state_seed.json
python backtest.py --synthetic 14         # autotest del backtest con datos aleatorios
```
`backtest.py` descarga histórico de BingX, reproduce el detector vela a vela y evalúa **walk-forward**:
A) todas las candidatas, B) filtro heurístico, C) filtro v2, cada una con mitad 1 vs mitad 2.
**Si C no aguanta en la mitad 2, no hay ventaja demostrada.** `--out` genera un estado semilla (cópialo al
Volume) para acortar la calibración. El backtest no incluye el bonus de TF superiores ni la cartera.

## Qué NO está verificado

- No he podido probar contra datos reales de BingX desde el entorno de desarrollo: el código está probado
  con datos sintéticos (maquinaria, persistencia, fases), no con rendimiento real. Ejecuta el backtest.
- Con datos aleatorios el sistema se comporta como debe: no encuentra ventaja, nunca llega a "validado" y
  termina callando. Con datos reales el veredicto lo dan tus números, no este documento.
- Costes: comisión 0,05 % por lado (taker, VIP 0) más deslizamiento estimado por liquidez. Ajusta
  `FEE_PCT_ROUNDTRIP` y `LQ_SLIP_*` a tu cuenta. No se modela el funding (poco relevante a ≤12 h).
- Cada parámetro que ajustes sobre el mismo periodo infla el resultado (sobreajuste). Cuenta tus pruebas.

## Setups, zonas y extras (sin cambios)

SOUP (barrido + cierre dentro), SOUP+1 (cierra fuera y la siguiente recupera), REBOTE (rechazo sin barrido,
-10 de calidad). Zonas: cúmulos de pivotes sin barrer puntuados 0-100 (toques 30 + recencia 10 + volumen 15 +
liquidaciones estimadas 30 + extremo 15). Liquidaciones estimadas por modelo de apalancamiento x volumen
(estimación, no dato real). Extras de TradingView: UT Bot, LinReg, S/R de 100-500 velas, tendencia multi-TF,
máx/mín de ayer, longevidad y panel. Gestión: SL tras el extremo del barrido (+ buffer ATR), TP en la siguiente
zona opuesta o R:R fijo; `LQ_TP_MODE=auto` mueve el TP al R:R con mejor límite inferior histórico (contrafactual
con MFE).
