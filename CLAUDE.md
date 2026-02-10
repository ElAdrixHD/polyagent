# Polyagent - Arbitrage Bot for Polymarket

## Objetivo
Bot que ejecuta **arbitraje matemático** en Polymarket: comprar YES + NO cuando la suma de asks < $1.00, garantizando profit sin riesgo direccional. Un LLM (vía OpenRouter) valida cada mercado antes de ejecutar.

## Arquitectura Actual (MVP)

```
src/
├── main.py                        # Entry point, loop principal, señales
├── core/                          # Infraestructura compartida
│   ├── config.py                  # Dataclass Config, carga .env, validación
│   ├── client.py                  # Wrapper ClobClient + Gamma API
│   ├── models.py                  # Dataclasses compartidas
│   └── logger.py                  # Logging con colores (colorama)
└── strategies/                    # Cada estrategia es un módulo
    └── arbitrage/                 # MVP: arbitraje matemático
        ├── scanner.py             # Escaneo de mercados por polling
        ├── websocket_feed.py      # Feed real-time WebSocket
        ├── analyzer.py            # Validación LLM vía OpenRouter
        └── executor.py            # Ejecución de trades + risk management
```

### Flujo
1. `scanner.scan()` obtiene mercados de Gamma API, consulta order books
2. Calcula `profit = 1.0 - (yes_ask + no_ask)` para cada mercado binario
3. Si profit > threshold → `ArbitrageOpportunity`
4. `analyzer.validate()` envía al LLM para evaluar riesgos
5. Si LLM dice safe → `executor.execute()` coloca órdenes FOK
6. WebSocket feed opera en paralelo para detección real-time

## APIs Utilizadas

### CLOB API (Polymarket)
- Host: `https://clob.polymarket.com`
- Auth: API key/secret/passphrase (auto-generados desde private key)
- Endpoints: order book, create order, cancel order
- Lib: `py-clob-client`

### Gamma API
- Host: `https://gamma-api.polymarket.com`
- Sin auth, público
- Endpoints: `/markets` (listado con filtros)
- Paginación: cursor-based (`next_cursor`)

### WebSocket
- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Suscripción por `assets_ids` (token IDs)
- Mensajes: book updates con asks/bids

### OpenRouter (LLM)
- URL: `https://openrouter.ai/api/v1/chat/completions`
- Auth: Bearer token
- Modelo configurable (default: claude-sonnet)
- Respuesta esperada: JSON con `safe`, `risk_level`, `reason`

## Configuración (.env)
Ver `.env.example` para todas las variables. Claves principales:
- `PRIVATE_KEY`: Wallet privada (requerida)
- `OPENROUTER_API_KEY`: Para validación LLM
- `DRY_RUN=true`: Modo simulación (default)
- `MIN_PROFIT_THRESHOLD=0.025`: Profit mínimo (2.5%)
- `MAX_DAILY_LOSS=50`: Kill switch

## Estrategias Futuras (Roadmap)
1. **Arbitraje Matemático** ← actual (MVP)
2. **Convergence Trading**: Mercados correlacionados con spreads anormales
3. **Calendar Arbitrage**: Mismo evento, diferentes fechas de resolución
4. **Cross-Platform Arbitrage**: Polymarket vs otras plataformas
5. **Sentiment-Driven**: LLM analiza noticias para predecir movimientos
6. **Mean Reversion**: Mercados que se desvían de su media histórica
7. **Liquidity Provision**: Market making con spreads inteligentes
8. **Event-Driven**: Detectar eventos (earnings, elecciones) antes del mercado

## Cómo Añadir una Nueva Estrategia
1. Crear directorio `src/strategies/nueva_estrategia/` con `__init__.py`
2. Implementar `scanner.py` con método `scan() -> list[Opportunity]`
3. Implementar `executor.py` para ejecución específica
4. Reutilizar `src/core/` (client, config, models, logger)
5. Registrar en `main.py`
6. Añadir configuración específica en `.env.example` y `Config`
7. El analyzer LLM puede reutilizarse con prompts especializados

## Nota Técnica: Gamma API
- `clobTokenIds` viene como string JSON (ej: `"[\"abc\", \"def\"]"`), necesita `json.loads()`
- Paginación: respuesta es lista directa (no dict con `data`), sin cursor real por defecto

## Convenciones
- Python 3.11+
- Dataclasses para modelos (no Pydantic por simplicidad)
- `logging` estándar con colorama
- Config vía `.env` + `python-dotenv`
- Todas las cantidades en USD
- Timestamps en UTC ISO format
- FOK (Fill or Kill) para órdenes de arbitraje
- `data/trades.json` como log persistente

## Ejecución
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # configurar claves
python -m src.main
```
