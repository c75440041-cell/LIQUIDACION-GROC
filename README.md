# Bot de Liquidez v2 - BingX → Telegram

Bot independiente que detecta **zonas de liquidez importantes** (BSL/SSL + liquidaciones estimadas) y genera señales de alta probabilidad:

- **SOUP** (Turtle Soup): barrido de la zona + cierre de vuelta dentro
- **SOUP+1** (Plus One): barrido, cierra fuera y la siguiente vela recupera
- **REBOTE**: rechazo limpio en zona fuerte (solo zonas ≥70)

## Mejoras v2 (vs versión original)

| Mejora | Detalle |
|--------|---------|
| Zonas más selectivas | Score mínimo 58 · penaliza internas · más peso a liquidaciones |
| MAX_SWEEP ajustado | 1.8 ATR (antes 3.0) → menos rupturas falsas |
| Calidad de vela | Filtro de ubicación del cierre + mecha mínima 0.42 |
| Trailing a Break-Even | Tras 1R mueve SL a entrada + buffer |
| EMA200 filter ON | Opera más a favor de tendencia |
| HTF más exigente | Mínimo 1 timeframe superior a favor |
| Cooldown por zona | No re-entra en el mismo nivel |
| Filtro de spikes | Ignora velas de noticias (>2.8× ATR) |

## Requisitos

- Python 3.12+
- Cuenta de Telegram + Bot Token
- Railway (o cualquier servidor) para 24/7

## Instalación rápida

1. Clona el repo
2. Crea un servicio en Railway
3. Añade las variables de entorno (copia de `.env.liquidez.example`)
4. Start Command: `python liquidez_bot.py`

```bash
pip install -r requirements.txt
python liquidez_bot.py
```

## Variables importantes

```bash
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
TIMEFRAMES=5m,15m
MIN_VOLUME_USDT=1500000
LQ_MIN_ZONE_SCORE=58
LQ_MIN_QUALITY=72
LQ_MAX_SWEEP_ATR=1.8
LQ_USE_EMA_FILTER=true
LQ_TRAIL_BE=true
LQ_HTF_MIN_ALIGNED=1
```

Ver `.env.liquidez.example` para todas las opciones.

## Gestión de riesgo

- SL más allá del extremo del barrido + buffer ATR
- TP: siguiente zona de liquidez opuesta de calidad o R:R fijo
- Trailing a BE después de 1R
- Salida por tiempo máximo (36 velas por defecto)
- Resumen diario automático

## Aviso

Este bot es una herramienta de análisis. Opera con capital que puedas permitirte perder. Las liquidaciones son un modelo estimado, no datos reales de exchange.
