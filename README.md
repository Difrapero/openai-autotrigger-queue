# OpenAI AutoTrigger Queue

Cola persistente en Python para ejecutar prompts con OpenAI y reanudar cada tarea en el instante que indica el propio servidor. Incluye una CLI y una API FastAPI importable como Action de un GPT.

## Qué hace diferente al reloj dinámico

El proyecto no solicita al usuario un tiempo de espera ni aplica un `sleep(60)` arbitrario. Ante `openai.RateLimitError` (HTTP 429), `autotrigger/cooldown.py` inspecciona, en este orden:

1. `retry-after` (segundos, fracciones de segundo o fecha HTTP).
2. `retry-after-ms`.
3. Todos los encabezados `x-ratelimit-reset-*`, incluidas las ventanas de requests y tokens. Entiende valores como `250ms`, `14.25s` y `1h2m3s`; si hay varias ventanas agotadas usa la más larga para no adelantarse.
4. Campos estructurados del error del SDK (`retry_after`, `retry_after_ms`, `reset_at`).
5. Mensajes como `Please try again in 20s`.

La hora se calcula como `time.time() + segundos_del_servidor` y se guarda tanto como timestamp Unix (precisión subsegundo) como fecha ISO 8601 UTC. La CLI muestra la hora local y ejecuta `time.sleep(segundos_exactos)`. FastAPI usa un timeout asíncrono: el worker espera, pero el servidor continúa respondiendo a `/enqueue` y `/status/...`.

El SDK se inicializa con `max_retries=0`. Así no añade reintentos ni esperas internas antes de que la aplicación pueda leer el 429. Si OpenAI devuelve un 429 sin reloj —por ejemplo, crédito/saldo agotado sin fecha de reposición— la tarea pasa a `failed`; inventar una espera provocaría un bucle sin fundamento.

Los encabezados de rate limit empleados están documentados en la [referencia oficial de OpenAI](https://developers.openai.com/api/reference/overview#debugging-requests). Las llamadas usan la [Responses API](https://developers.openai.com/api/reference/responses/create).

## Estructura

```text
.
├── autotrigger/
│   ├── cooldown.py       # extracción y parsing del reloj del servidor
│   ├── openai_service.py # adaptador de Responses API
│   ├── queue_store.py    # persistencia JSON atómica
│   ├── runner.py         # ejecución síncrona de la CLI
│   └── worker.py         # scheduler asíncrono de FastAPI
├── autotrigger_cli.py
├── main.py
├── openapi.yaml
├── requirements.txt
└── tests/
```

`cola_tareas.json` se crea automáticamente y no se versiona. Cada registro conserva prompt, modelo, estado, intentos, hora de reanudación, fuente del cooldown, IDs de OpenAI, resultado y error.

## Instalación

Requiere Python 3.10 o posterior.

```bash
python -m venv .venv
```

macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="sk-..."
```

PowerShell:

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:OPENAI_API_KEY = "sk-..."
```

Opcionalmente define `OPENAI_MODEL` (por defecto `gpt-5.4-mini`) y `AUTOTRIGGER_QUEUE_PATH`. La clave de OpenAI permanece exclusivamente en el servidor.

## Fase 1: CLI

Añadir tareas:

```bash
python autotrigger_cli.py --add "Resume la teoría de juegos en cinco puntos"
python autotrigger_cli.py --model gpt-5.4-mini --add "Escribe una prueba unitaria"
```

Consultar la cola:

```bash
python autotrigger_cli.py --list
```

Procesar hasta que no queden tareas pendientes:

```bash
python autotrigger_cli.py --run
```

Si se pulsa `Ctrl+C`, una tarea en curso vuelve a `queued`; una tarea que ya recibió un 429 mantiene su `waiting` y su hora exacta de reanudación.

## Fase 2: FastAPI

Arranque local:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Use un solo worker de Uvicorn: el backend de ejemplo es un archivo JSON con bloqueo dentro del proceso. Para varias réplicas o workers, sustituya `TaskStore` por PostgreSQL/Redis con adquisición transaccional de tareas.

Encolar:

```bash
curl -X POST http://localhost:8000/enqueue \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Explica las colas de prioridad"}'
```

Respuesta (`202 Accepted`):

```json
{
  "task_id": "51d4b5c0-310b-47ea-9da8-f012e67838f7",
  "status": "queued",
  "status_url": "http://localhost:8000/status/51d4b5c0-310b-47ea-9da8-f012e67838f7"
}
```

Consultar:

```bash
curl http://localhost:8000/status/51d4b5c0-310b-47ea-9da8-f012e67838f7
```

Estados posibles: `queued`, `processing`, `waiting`, `completed`, `failed`.

### Autenticación y despliegue como GPT Action

Para producción configura un secreto propio, diferente de `OPENAI_API_KEY`:

```bash
export AUTOTRIGGER_API_KEY="un-secreto-largo-y-aleatorio"
```

Cuando existe, los dos endpoints requieren `Authorization: Bearer un-secreto-largo-y-aleatorio`. Sin esa variable se permite acceso anónimo únicamente para facilitar el desarrollo local.

1. Despliega la API detrás de HTTPS.
2. Sustituye `https://YOUR_PUBLIC_HTTPS_DOMAIN` en `openapi.yaml`.
3. Importa `openapi.yaml` en la configuración de Actions del GPT.
4. Configura autenticación por API key tipo Bearer con el valor de `AUTOTRIGGER_API_KEY`.

La Action encola rápidamente y debe consultar `getOpenAITaskStatus` hasta obtener `completed` o `failed`; no mantiene una petición web abierta durante el cooldown.

## Pruebas

No requieren una clave ni realizan llamadas reales:

```bash
python -m unittest discover -s tests -v
```

Cubren segundos decimales, fechas HTTP, duraciones compuestas, selección de la ventana más larga, fallback al cuerpo del SDK y persistencia del cooldown global.
