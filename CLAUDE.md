# CLAUDE.md

Instrucciones para trabajar en este repositorio. Léelas antes de tocar nada.

## Qué es esto

**Content Factory**: una fábrica de contenido para YouTube que automatiza *la toma de decisiones*,
no solo la producción. Descubre oportunidades → las puntúa → guion → video → miniatura → SEO →
publica → mide → aprende. El humano controla la calidad en dos checkpoints: aprobar el guion y
aprobar el video final.

Corre **24/7 sin nadie mirando**, en una PC con Windows. El desarrollo es en Linux. Objetivo de
coste: $0/mes.

Esa frase —nadie va a estar delante cuando falle— gobierna casi todas las reglas de abajo.

| Necesitas saber | Está en |
|---|---|
| Estado actual, qué hacer al volver, pendientes | `PROJECT_MEMORY.md`, cabecera "Dónde estamos" |
| Los 5 hitos y qué falta de cada uno | `ROADMAP.md` |
| Instalación, claves y cómo conseguirlas | `README.md` |
| Por qué el código es como es, y los fallos ya resueltos | `PROJECT_MEMORY.md` |

Actualiza `PROJECT_MEMORY.md` al cerrar cada unidad de trabajo, y `ROADMAP.md` al cerrar un hito.

## Arquitectura

Monolito modular en Python 3.12+. Sin Docker, sin Redis, sin n8n: un usuario, una PC.

- **SQLite en WAL** (`data/factory.db`), migraciones versionadas con `PRAGMA user_version`.
- **Cola de jobs como tabla**, con claim atómico. El worker vive en un hilo.
- **APScheduler 3.x** (la 4.x lleva años en alpha: no se usa). Los jobs del scheduler **encolan**;
  el worker ejecuta.
- **FastAPI + Jinja2** para el dashboard. Sin JavaScript: formularios POST y redirect.
- **Patrón blackboard**: los módulos de dominio se comunican **solo a través de la base de datos**.

Lo que existe hoy son tres módulos, no más:

```
factory/core/       db, models, queue, quota, scheduler, config   ← lo único compartido
factory/research/   fuentes + scorer + pipeline diario            ← hito 1, hecho
factory/web/        dashboard FastAPI + Jinja2                    ← hito 1, hecho
```

**Aún no existen — no los busques**: `script/` (h2), `media/` `video/` `thumbnail/` (h3),
`seo/` `publish/` (h4), `analytics/` (h5). Ni siquiera como directorio vacío.

Dentro de `web/` la separación también es dura: **todo el SQL vive en `web/queries.py`** y sale
como dataclasses frozen ya listas para pintar; `web/server.py` no escribe SQL y las plantillas no
llevan lógica.

Máquina de estados de un video (en `core/models.py`, con transiciones ilegales rechazadas):

```
idea_approved → script_draft → [✋ humano] → script_approved → producing
  → video_ready → [✋ humano] → video_approved → scheduled → published → measured
```

## Restricciones duras

No se negocian. Cada una está aquí porque su ausencia ya causó un fallo real o lo causaría.

**Modularidad**
- Un módulo de dominio **no importa de otro módulo de dominio**. Solo de `core/`. Si un import
  cruzado parece inevitable: **para y pregunta**. Romper el blackboard se hace una sola vez.

**Secretos**
- **El `.env` no se escribe nunca.** Hay un hook que lo bloquea y está bien que exista: las claves
  son del usuario y las pone a mano. Ningún secreto en el código, en el repo ni en un log.

**Base de datos**
- Toda escritura va dentro de **`with db.transaction(conn):`**, que ya emite `BEGIN IMMEDIATE` y
  hace rollback. No lo escribas a mano. El `DEFERRED` por defecto falla al instante con "database
  is locked" ignorando el `busy_timeout`, y subirlo no lo arregla nunca.
- **Comprobar y escribir es una operación, no dos.** Un `SELECT` y luego un `UPDATE` es una carrera
  esperando su turno; ya la tuvimos dos veces (claim de la cola, dedupe de investigación).
- Una conexión por hilo. Los pragmas se fijan en cada conexión nueva, no una vez al crear la base.

**FastAPI**
- Endpoints que tocan SQLite: **`def`**, nunca `async def` — una llamada bloqueante dentro de un
  `async def` para el servidor entero.
- **`lifespan`**, nunca `@app.on_event`: si defines `lifespan`, los `on_event` se ignoran en
  silencio y el arranque simplemente no ocurre.
- **Un solo worker de uvicorn** mientras el scheduler viva en el proceso, o son N schedulers
  ejecutando el mismo job N veces.
- Importar `app` no debe tener efectos secundarios.

**Fuentes externas**
- Toda fuente se cae y este sistema depende de varias. Una fuente caída deja la **señal ausente con
  su motivo registrado en la DB** (no solo en el log) y el scorer **re-normaliza los pesos**.
- **Nunca un 0 por falta de datos.** Un 0 es un valor válido y hunde el score; la ausencia se
  propaga como ausencia. Este fue el fallo más caro del hito 1.
- Timeout explícito siempre. Reintentos solo en 429/5xx/timeout, con backoff y jitter: reintentar
  un 400 o un 403 gasta cuota para recibir el mismo error.

**Cuota de APIs**
- Reservar el coste **antes** de la llamada y reconciliar después. Corte al 80% del presupuesto.
- La cuota de YouTube es el recurso escaso: 102 unidades por keyword, corte diario en 3.200. Antes
  de añadir una llamada, calcula lo que cuesta una pasada completa.

**Producción es Windows**
- `pathlib` siempre; `encoding="utf-8"` explícito al escribir texto (el default de Windows no lo es).
- **Sin flechas ni símbolos unicode en mensajes de log**: revientan bajo cp1252 y logging descarta
  la línea, justo la que hacía falta para diagnosticar. En docstrings y comentarios sí valen.
- No tocar la política de bucle de eventos de asyncio: el default es el único que soporta
  subprocesos, y forzar el selector rompe FFmpeg y edge-tts.

**Producto**
- Las decisiones de política de plataforma —disclosure de contenido sintético, qué se publica sin
  aprobación humana, la auditoría de la API de YouTube— **se preguntan al usuario**. Tienen
  consecuencias de suspensión de canal y no le corresponden a un agente.
- Los checkpoints humanos no son un adorno: son la mitigación de diseño contra la política de
  "inauthentic content" de YouTube. No los saltes ni propongas saltarlos.

## Cómo se añade algo

Cada pieza tiene ya su forma en el repo. Cópiala en vez de inventar una nueva.

- **Tabla nueva** → un string más al final de `MIGRATIONS` en `core/db.py`. La posición en la lista
  *es* la versión: nunca edites una migración ya aplicada.
- **Función de acceso a datos** → firma `conn: sqlite3.Connection | None = None` y
  `conn = conn or get_conn()` dentro. Nunca un `sqlite3.connect` propio.
- **Trabajo en segundo plano** → el módulo expone `JOB_TYPE`, `handle_x(job: Job) -> None` y
  `register(worker: JobWorker)`. Se registra en `app.py` *antes* de `worker.start()`. No hay
  decorador ni registro global: es explícito a propósito. Modelo a copiar: `research/pipeline.py`.
- **Bucle largo dentro de un job** → consulta `queue.should_stop()` entre pasos, o el apagado se
  come los 30 s de timeout enteros.
- **Tarea periódica** → `add_job` en `build_scheduler()` con `id=` fijo, `replace_existing=True`,
  `max_instances=1`, `coalesce=True` y un `misfire_grace_time` razonado. El wrapper **solo encola**,
  y hace `close_conn()` en `finally`: el hilo del executor acumula conexiones si no.
- **API externa** → el transporte es `http_util.get_json` / `get_with_retries`, que ya traen
  timeout, reintentos con jitter y `Retry-After`. Con cuota: `quota.reserve(...)` → llamada →
  `quota.reconcile(...)`. Falta de credencial → `SourceUnavailable`, no un return vacío.
- **Configuración** → un accessor con nombre y tipo en `core/config.py`; no leas el YAML crudo desde
  tu módulo. Los secretos se leen con `os.environ` en el punto de uso.

Y la distinción de la que depende todo el scorer:

> `None` / `[]` = *la fuente respondió y no tiene ese dato*.
> `SourceUnavailable` = *la fuente se cayó*.
> Confundirlas es exactamente el fallo más caro del hito 1.

## Cómo se trabaja aquí

Cada unidad de trabajo pasa por este ciclo:

1. **`backend-python`** implementa.
2. **`testing-python`** escribe los tests.
3. **`verificador`** ejecuta y devuelve la salida real.
4. **`revisor`** revisa en contexto limpio, sin haber participado.
5. Se arregla lo que salga.
6. **`git`** commitea con el repo en verde.

El dashboard se reparte: `backend-python` hace rutas, datos y estructura de plantilla;
**`disenador`** hace la capa visual, con el principio **"menos es más"** — interfaz simple, humana,
no robótica, pocas vistas y lenguaje natural.

No es ceremonia. En el hito 1, con 611 tests en verde, la revisión en contexto limpio encontró
**12 fallos**, tres de ellos capaces de dejar el sistema muerto en silencio durante días: el hilo
del worker moría sin avisar, un corte de luz dejaba jobs irrecuperables, y una señal ausente se
contaba como 0. Ningún test los veía.

### La suite verde no basta

Los fallos que importan en un sistema desatendido —hilos que mueren, cortes de luz, carreras,
codificación de logs— **solo aparecen verificando en ejecución**. Arranca el proceso, mata el
proceso, dispara el job a mano, consulta la tabla, mira el fichero.

**Enseña la evidencia, no la conclusión**: pega la salida real del comando. Si algo no se puede
verificar (falta una clave, hace falta la PC Windows), **dilo explícitamente** en vez de darlo por
bueno.

## Comandos

```bash
venv/bin/python app.py                              # dashboard en http://localhost:8000
venv/bin/python -m pytest -q                        # 611 tests, CERO xfail
venv/bin/python -m pytest tests/test_scorer.py -q   # un fichero
venv/bin/python -m pytest -k "dedupe" -q            # por nombre
venv/bin/python -m pytest -m concurrencia           # los que levantan hilos contra SQLite real
```

Un `xfail` que aparezca no se ignora: significa que hay un arreglo a medias, y el marcador lleva
el motivo dentro.

**No hay linter, formateador ni type-checker** en este repo. No los invoques ni añadas su
configuración sin preguntar. `pyproject.toml` contiene solo `[tool.pytest.ini_options]`, con dos
cosas deliberadas: `--strict-markers` (un marcador sin declarar es error) y
`filterwarnings = ["error::DeprecationWarning:factory.*"]` (un `DeprecationWarning` propio revienta
el test; el de una dependencia no).

Los tests viven planos en `tests/`, sin `__init__.py`, con nombres en español. Antes de pelearte
con uno, mira `tests/conftest.py`: tiene tres fixtures **autouse** que explican casi cualquier
sorpresa —`_sin_red` (bloquea el socket: cualquier salida real lanza `RuntimeError`),
`_sin_clave_de_youtube` y `_config_sin_cache`— más `muestra` (carga `tests/fixtures/`) y
`sin_esperas` (anula los `sleep`).

Para forzar una investigación sin esperar a las 07:30: botón "Investigar ahora" en la pestaña
Sistema del dashboard. Está deduplicado — no encola si ya hay una en curso para ese nicho.

## Convenciones de código

- Español en docstrings, comentarios y mensajes de log. Nombres de código en inglés si es lo
  natural; sigue el estilo que ya usa el repo.
- `from __future__ import annotations` y type hints en toda firma pública. `X | None`, no `Optional`.
- Una función hace una cosa y cabe en una pantalla. Si necesita un comentario para explicar el
  bloque de en medio, ese bloque es otra función.
- Sin abstracción especulativa: dos implementaciones concretas se leen mejor que una jerarquía que
  anticipa la tercera.
- `logging` con un logger por módulo, nunca `print()` en código que corre desatendido.
- Migrar no es reescribir. Buena parte de `factory/media/` y `factory/video/` saldrá del MVP viejo
  (`../granja de videos/scripts/`): esos módulos ya funcionan y se mueven, no se rehacen.
