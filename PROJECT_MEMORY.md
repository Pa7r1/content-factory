# Memoria del proyecto

Avances, decisiones y pendientes. Se actualiza al cerrar cada unidad de trabajo.
El plan completo por hitos está en [ROADMAP.md](ROADMAP.md).

---

## Dónde estamos (última sesión: 2026-08-13)

**Hito 1 cerrado y el motor por fin rinde entero.** Repo en GitHub:
`https://github.com/Pa7r1/content-factory` (privado). Esquema en `user_version = 3`.
El hito 2 (guiones + base de conocimiento) es lo siguiente; está descrito en [ROADMAP.md](ROADMAP.md).

**Arrancar:**

```bash
venv/bin/python app.py          # dashboard en http://localhost:8000
venv/bin/python -m pytest -q    # 758 passed, CERO xfail
```

Si aparece algún `xfail`, no lo ignores: significa que se dejó un arreglo a medias — el marcador
lleva dentro el motivo.

**Qué hace hoy:** cada mañana a las 07:30 sondea las keywords semilla de los 3 nichos, mide las
señales, **criba el material y le pide a Gemini temas de vídeo propios** (ya no copia títulos
ajenos), los puntúa de 0 a 100 y los deja en el dashboard para aprobar o descartar. También hay
un botón "Investigar ahora" en la pestaña Sistema.

**Las dos claves están puestas y verificadas** (`YOUTUBE_API_KEY`, `GOOGLE_AI_API_KEY`). Faltan
`PEXELS_API_KEY` y `PIXABAY_API_KEY`, que son del hito 3.

**Cuota:** una pasada de un nicho cuesta ~612 unidades de YouTube; el corte diario está en 3.200,
así que los 3 nichos entran justos. Gemini gasta ~1 unidad por keyword de un tope de 160.

**Al volver, en este orden:**

1. **Repoblar los dos nichos que quedaron sin pasar** (`historias_cristianas`, `historias_epicas`):
   la cuota de YouTube se agotó el día 13. El cron de las 07:30 lo hace solo si la app está viva.
2. Confirmar que la suite sigue verde antes de tocar nada.
3. Empezar el hito 2.

**Dos decisiones de calidad pendientes, que son del usuario:**

- **La basura sigue contando en el score.** La criba filtra lo que llega al LLM, pero los vídeos
  descartados **siguen alimentando la mediana de vistas**: el visualizer de Bad Bunny ya no genera
  una idea, pero todavía infla la demanda de la keyword "lo logré". Arreglarlo cambia todos los
  scores.
- **El dedupe no cubre reformulaciones con subtítulo**: "El efecto dominó de una pequeña elección"
  y "…: construyendo hábitos" siguen siendo dos ideas. Normaliza grafía y puntuación, no sentido.

**Cómo se trabaja aquí:** cada unidad de trabajo pasa por `backend-python` (implementa) →
`testing-python` (tests) → `verificador` (ejecuta y devuelve salida real) → `revisor` (revisa en
contexto limpio) → arreglar lo que salga → `git`. No es ceremonia: en el hito 1 la revisión
encontró **12 fallos que 611 tests verdes no veían**, tres de ellos capaces de dejar el sistema
muerto en silencio durante días. La suite verde no basta; hay que verificar en ejecución.

---

## Decisiones de arquitectura (2026-08-06)

- **Monolito modular Python + SQLite (WAL) + cola de jobs en tabla + APScheduler 3.x + FastAPI/Jinja2.**
  Sin Docker, sin Redis, sin n8n: un usuario, una PC. Justificación: cero infraestructura extra,
  backup = copiar un archivo, idéntico en Linux (dev) y Windows (prod).
- **Patrón blackboard**: los módulos se comunican solo a través de la DB; solo `core/` es compartido.
- **Checkpoints humanos**: aprobar guion y aprobar video final desde el dashboard. Es también la
  mitigación principal contra la política de "inauthentic content" de YouTube.
- **YouTube primero** (largos 16:9 + Shorts derivados); multicanal/multiplataforma después.
- **Todo gratis**: YouTube Data API (10k u/día), Edge TTS, FFmpeg, Gemini free tier, Wikipedia
  Pageviews, RSS. pytrends está muerto (archivado 2025) → trendspy opcional + fallbacks.
- Se migra el MVP de "granja de videos" (TTS, imágenes, montaje, llm_client), no se reescribe.

## Avances

- 2026-08-06 — Proyecto creado. Plan aprobado, especialistas montados (backend-python,
  testing-python + reglas Python/FFmpeg/FastAPI), esqueleto del repo con roadmap y memoria.
- 2026-08-06 — **Núcleo construido y verificado** (`factory/core/` + `app.py` + `config/settings.yaml`):
  - `db.py`: conexión por hilo (`threading.local`), pragmas por conexión (WAL, busy_timeout 10s,
    synchronous NORMAL, foreign_keys ON), `autocommit=True` explícito con transacciones vía
    `BEGIN IMMEDIATE`, migración 1 con las 8 tablas del plan + índices (el claim de jobs usa
    `idx_jobs_claim`, verificado con `EXPLAIN QUERY PLAN`).
  - `models.py`: dataclasses Idea/Video/Job + máquina de estados de videos con
    `assert_transition()` que rechaza pasos ilegales.
  - `queue.py`: claim atómico `UPDATE ... RETURNING` bajo `BEGIN IMMEDIATE`; reintentos con
    backoff exponencial en `run_after` (30s·2^n) hasta `max_attempts`; job sin handler → failed
    sin reintento; `JobWorker` en hilo con registro de handlers por tipo.
  - `quota.py`: reservar ANTES de la llamada, reconciliar después, corte al 80% del presupuesto.
  - `scheduler.py`: APScheduler 3.11.3, jobs que solo ENCOLAN (`research_daily` 07:30 por nicho,
    `fetch_metrics` 09:00), `max_instances=1`, `coalesce=True`, misfire 6h/12h razonado.
  - `app.py`: lifespan (migra DB → worker → scheduler; apagado en orden inverso), `/health` con
    estado de DB/esquema/cola, router de `factory/web/server.py` con índice provisional.
  - Verificado en vivo: app arriba, `/health` 200, job encolado desde otro proceso reclamado por
    el worker del proceso de la app (WAL entre procesos OK) y marcado failed por falta de handler.
  - Sin handlers registrados todavía: `research_daily` y `fetch_metrics` fallarán con
    "handler no registrado" hasta que existan `research/` y `analytics/` (esperado).

- 2026-08-06 — **Motor de investigación completo** (`factory/research/`), verificado en vivo:
  - `http_util.py`: timeout obligatorio, reintentos solo en 429/5xx/timeout con backoff exponencial
    + jitter, respeto de `Retry-After` (tope 30 s), `SourceUnavailable` con el código HTTP dentro
    para distinguir "no hay datos" (404) de "la fuente cayó".
  - Fuentes: `youtube_source` (REST v3, reserva de cuota ANTES de cada llamada; uploads por
    playlist, nunca `search.list`), `wikipedia_source` (pageviews mensuales, descarta el mes en
    curso porque va incompleto), `news_source` (Google News RSS → momentum 7d/30d),
    `reddit_source` (JSON público, una pasada por nicho en vez de una por keyword).
  - `scorer.py`: **lógica pura** (sin red, sin DB). demand/competition/evergreen/cpm con
    sub-pesos internos; si falta una señal se re-normalizan los pesos de las presentes en los dos
    niveles y se devuelve la lista de ausentes. Nunca devuelve 0 por falta de datos.
  - `competitor.py`: canal → 50 uploads por playlist → frecuencia/semana, duración media,
    views/día y **outlier ratio** por video; upsert en `competitors` + `competitor_videos`.
    Coste: 3 unidades de cuota por canal.
  - `pipeline.py`: handler de `research_daily`. Cada fuente en su try/except; la señal ausente se
    guarda con su motivo en `ideas.score_details.missing_reasons`. Las keywords son sondas: las
    ideas son temas concretos (títulos de YouTube con mayor outlier ratio + titulares de News).
    Dedupe por nicho+keyword+título en estado 'new'.
  - Verificado: 3 jobs `research_daily` encolados a mano contra la app real → los 3 `done`,
    19 ideas puntuadas en la base, con YouTube y Reddit como señales ausentes registradas.

- 2026-08-06 — **Cuatro bugs de `research/` arreglados** (los encontró la suite de testing-python;
  los dos xfail strict pasan a verde y desaparecen):
  - `http_util`: `requests.exceptions.SSLError` hereda de `ConnectionError`, así que caía en la
    rama transitoria y se reintentaba. Ahora se captura ANTES: un certificado inválido no mejora
    reintentando.
  - `news_source.headlines`: recortaba a `max_items` antes de ordenar por fecha; con feed
    desordenado devolvía los primeros del XML. Ahora ordena y luego recorta.
  - `wikipedia_source.monthly_pageviews`: la ventana era `months * 31` días y devolvía hasta 13
    meses. Ahora arranca el día 1 del mes que queda exactamente `months` meses atrás
    (`_first_day_months_ago`). Verificado en vivo: `months=12` → 12 meses exactos.
  - `reddit_source.collect_top_posts([])`: lanzaba "ningún subreddit respondió — " (0 motivos == 0
    subreddits). Ahora lanza `SourceUnavailable("sin subreddits configurados…")`. Se lanza en vez
    de devolver `[]` porque `[]` daría engagement 0 a cada keyword y el scorer penalizaría por
    falta de datos en lugar de re-normalizar.
  - Suite completa tras los arreglos: **497 passed, 0 xfail**.

- 2026-08-06 — **Dashboard del hito 1** (`factory/web/`), verificado en vivo contra la DB real:
  - `queries.py`: todo el SQL de la web (ranking de ideas, desglose por componente con las señales
    ausentes y su motivo desde `score_details`, recuento por estado, cuota de hoy) más la única
    escritura, `update_idea_status` bajo `BEGIN IMMEDIATE`.
  - `server.py`: `GET /` (Ideas), `GET /system`, `POST /ideas/{id}/approve|reject`,
    `POST /research/run`. Todos `def`; formularios POST + redirect 303, sin JavaScript.
  - Aprobar deja la idea en `approved` (estado del que parte el hito 2); descartar, en `rejected`,
    que el pipeline ya trata como terminal y no repropone.
  - `POST /research/run` llama a `scheduler.enqueue_research_daily()`, la MISMA función que dispara
    el cron de las 07:30, para que los dos caminos no se separen con el tiempo.
  - La próxima ejecución programada se lee del scheduler vivo vía `app.state.scheduler`.
  - Plantillas Jinja2 (`base.html`, `ideas.html`, `system.html`) con HTML semántico y clases
    estables; `static/dashboard.css` casi vacío a propósito: la capa visual es de `disenador`.
  - Verificado: 19 ideas listadas y ordenadas por score; aprobar la 8 y descartar la 9 cambia el
    estado en la DB y las saca del ranking (17 restantes); idea inexistente → 404, idea `used` →
    409; "Investigar ahora" encoló los jobs 4/5/6, los tres terminaron `done` y la pasada añadió 4
    ideas nuevas sin tocar la aprobada ni la descartada.

## Problemas resueltos

### El motor arreglado destapó que las ideas eran títulos ajenos (2026-08-13)

Con la clave de YouTube puesta, la primera pasada real funcionó de maravilla en lo técnico —15/15
jobs `done`, cuota controlada, log limpio— y produjo esto como mejores ideas:

```
78.91  BAD BUNNY - NADIE SABE (Visualizer)
78.91  Myke Towers - Lo Logré (Video Oficial)
70.40  Mujer de Altar | Canción Cristiana de Adoración (Video Oficial)
69.37  If you don't have Discipline you are a nobody #discipline #mentality
```

De las 20 mejores, 2 servían. **Causa**: `pipeline` tomaba los títulos de YouTube y los titulares
de News **literalmente como ideas**. La keyword "lo logré" encuentra a Myke Towers y, como su vídeo
tiene millones de vistas, el sistema lo lee como "tema con demanda enorme" y lo premia.

**El fallo llevaba ahí desde el principio y era invisible**: sin clave de YouTube las ideas salían
solo de titulares de noticias, así que arreglar el motor fue lo que lo destapó. 611 tests en verde
no lo veían porque ninguno preguntaba *si la idea era buena*, solo si se escribía bien.

**Arreglo**: `research/candidates.py` criba el material (música, Shorts, hashtags, otros idiomas,
y limpia a cp1252) y `core/llm.py` le pide a Gemini **temas propios**. Regla que no se negocia:
**si el modelo no responde, la keyword no produce nada y el motivo queda en `research_log`; jamás
se cae de vuelta a copiar un título ajeno.** Preferimos cero ideas a ideas basura.

Resultado con el mismo nicho y la misma máquina: `La quietud como motor: encuentra tu propósito en
el silencio`, `Desactivando el auto-sabotaje: entendiendo tus barreras internas`.

### La revisión en contexto limpio encontró 6 fallos más (2026-08-13)

Con 758 tests en verde. Dos críticos, del tipo "muerto en silencio durante días":

1. **El dedupe entre pasadas dejó de funcionar al meter el LLM.** `_upsert_idea` emparejaba por
   título **exacto**, algo que valía cuando los títulos se copiaban de una fuente; un modelo nunca
   repite la cadena literal. Consecuencia grave: **lo que el humano rechazaba volvía al día
   siguiente reformulado**, o sea que el checkpoint humano —que es una mitigación de diseño, no una
   comodidad— dejaba de tener efecto. Arreglo: columna `dedupe_key` (migración 3) con índice sobre
   `(niche, dedupe_key)`, emparejando por clave normalizada. Verificado contra la base real: tres
   variantes de un título ya rechazado chocan con él y no se reproponen.
2. **Una caída total del LLM dejaba el job en `done` tras quemar 1.938 unidades de YouTube.** La
   cuota se cobra *antes* de consultar al modelo, y como las keywords se "sondeaban" bien, el job
   terminaba verde sin escribir una sola idea. Cada día, sin error visible. Arreglo: se cuentan las
   keywords donde se consultó al modelo y falló; a las 2 seguidas se corta la pasada y el job queda
   `failed`. Medido: 204 unidades gastadas en vez de 612.

Y dos que introdujo el propio arreglo del filtro de noticias, ambos por pedir "frase completa":

3. **El filtro anulaba la señal en 14 de las 19 keywords.** "traición y venganza" no casaba con
   *"Traición, venganza y poder en la nueva serie"*, y 14 keywords son multipalabra. Arreglo:
   exigir todas las palabras significativas, no contiguas.
4. **El filtro fabricaba un 0 duro donde antes había ausencia.** Si quedaba un titular viejo,
   `last_7d=0` daba `momentum_norm = 0.0` en vez de `None`: **35 puntos de diferencia medidos**.
   Es el fallo más caro del hito 1 entrando por otra puerta. Ahora `None` cuando ningún titular
   trae fecha; "hay noticias y ninguna esta semana" sí es un 0 legítimo (tema frío).

También: nada impedía que el modelo copiase un título del material que se le pasaba (se descarta
por `dedupe_key`), y el backoff no leía el `retryDelay` que Gemini manda en el cuerpo en vez de en
`Retry-After` (un 429 reintentado a los 2 s caía en la misma ventana de 60 s y volvía a fallar).

### Revisión en contexto limpio: 10 fallos de operación (2026-08-06)

Los encontró una revisión completa del código con la suite ya en verde. Ninguno lo veía un test:
son fallos que solo aparecen con el sistema corriendo días sin nadie delante. Verificados uno a
uno en ejecución antes y después del arreglo.

**Críticos**

1. **Reddit devolvía señal 0 en vez de ausente.** `collect_top_posts` devolvía `[]` cuando todos
   los subreddits respondían 200 con `children` vacíos (sub vacío, o el cuerpo de error que Reddit
   sirve con 200 bajo rate-limit): `motivos` estaba vacío, el guard `len(motivos)==len(subreddits)`
   no saltaba y `engagement_in_posts` daba `engagement: 0`. Como 0 es un valor válido, el scorer
   **no re-normalizaba** y el score se hundía. Arreglo: segundo guard que lanza `SourceUnavailable`
   si no hay ni un post. Medido: con la misma keyword, 73.64 con reddit=0 frente a 78.80 con reddit
   ausente.
2. **El hilo del worker moría en silencio.** `complete()` y `fail()` estaban FUERA del `try` que
   protege al handler: un `OperationalError: database is locked` al cerrar el job subía por
   `_process_one` → `_run` y mataba el hilo. Los jobs quedaban `pending` para siempre sin ningún
   error visible. Arreglo: `_complete_safely`/`_fail_safely` (registran y siguen) y
   `_process_one_guarded` como red exterior. Ninguna excepción puede matar ya ese hilo.
3. **Ningún job en `running` se recuperaba tras un corte.** Un reinicio de Windows a mitad de job
   dejaba la fila `{status:'running', finished_at:NULL}` invisible para siempre. Arreglo:
   `requeue_stale_running()` al arrancar el worker. **Semántica decidida**: el rescate *no* consume
   un intento extra —ese intento ya lo contó el claim que se interrumpió, y castigar al job por un
   corte de luz lo dejaría fallido antes de tiempo— pero deja WARNING en el log y el motivo en
   `jobs.error`. Los que ya no tienen intentos disponibles **no** vuelven a la cola: se marcan
   `failed`, porque un job capaz de tumbar el proceso volvería a tumbarlo en cada arranque.

**Importantes**

4. **`stop()` mentía y los handlers no podían parar.** El `join(timeout)` expiraba y se logueaba
   "Worker de jobs parado" igual; además `research_niche` dormía 1 s entre keywords sin poder
   interrumpirse, así que el `stop(10.0)` del lifespan expiraba siempre en una pasada real de 19
   keywords. Arreglo: `queue.should_stop()` (la señal del worker en un `threading.local`, False
   fuera del worker) consultada por `research_niche` entre keywords; `stop()` avisa con WARNING si
   el hilo sigue vivo; timeout del lifespan explícito en `app.WORKER_STOP_TIMEOUT = 30.0`. Una
   pasada interrumpida termina el job como `done` y no reintenta: la del día siguiente la cubre y
   reintentarla costaría ~2.000 unidades de cuota.
5. **Reddit del nicho fuera del patrón `_try_source`.** `_niche_context` capturaba solo
   `SourceUnavailable`, así que un post con `"ups": null` lanzaba `TypeError` y tumbaba el nicho
   entero (job fallido + 3 reintentos). Arreglo: `_niche_reddit_posts` pasa por `_try_source` como
   las otras cuatro fuentes, y `reddit_source` normaliza campos nulos (`_as_text`, `_as_int`,
   `_children`) en vez de confiar en la forma de la respuesta.
6. **"No había nada nuevo" se confundía con "todo falló".** `if fallos and escritas == 0: raise`
   hacía fallar el job a las pocas semanas, cuando el humano ya ha descartado esas ideas y
   `_upsert_idea` devuelve None por `TERMINAL_STATUSES`. Cada falso fallo quemaba ~714 unidades de
   cuota en 3 reintentos. Arreglo: se cuenta `sondeadas` (keywords que terminaron sin excepción) en
   lugar de `escritas`; solo se lanza si `sondeadas == 0`. **Escribir cero ideas es normal; no
   poder sondear ninguna keyword es el fallo de verdad.**
7. **"Investigar ahora" no deduplicaba.** Dos clics el mismo día encolaban dos pasadas completas
   (1.938 u por pasada, corte en 3.200). Arreglo: `enqueue_research_daily()` devuelve
   `ResearchEnqueue(job_ids, skipped_niches)` y omite los nichos con un `research_daily` en
   `pending`/`running` (`queue.unfinished_by_type`). El dedupe vive en el scheduler, no en la web,
   para que proteja también al cron. Un nicho cuyo job ya está `done` sí se vuelve a encolar.
8. **Logging inservible en la PC de producción.** Los `→` de `queue.py`, `queries.py` y
   `pipeline.py` revientan bajo cp1252 (UnicodeEncodeError → logging descarta la línea, y se pierde
   justo la traza de diagnóstico); y `basicConfig` no escribía a fichero, así que en una máquina
   desatendida no quedaba rastro. Arreglo: `->` en todos los mensajes de log y de excepción (los de
   docstrings y comentarios se quedan), y `app.configure_logging()` con consola + `RotatingFileHandler`
   sobre `data/factory.log` (2 MB × 5, `encoding="utf-8"`).

**Menores**

9. Cuatro fallos silenciosos: `db.get_conn(otra_ruta)` devolvía la conexión del hilo ignorando la
   ruta pedida (ahora lanza `ValueError`); `quota.reconcile()` con id inexistente hacía 0 filas sin
   decir nada (ahora WARNING: significa gasto real sin apuntar); `quota.reserve()` evaluaba
   `_today()` dos veces dentro de la misma transacción, así que un cambio de día UTC entre medias
   comprobaba contra el presupuesto de ayer y apuntaba en el de hoy (ahora una sola vez).
10. **`transaction()` dejaba la conexión inutilizable si fallaba el COMMIT**: la transacción seguía
    abierta y todo `BEGIN IMMEDIATE` posterior de ese hilo daba "cannot start a transaction within a
    transaction" — bucle infinito de "Error reclamando job". El COMMIT pasa a estar dentro del `try`
    con `_rollback_quietly` en la rama de error. Y `pipeline._age_days` devolvía 0.0 con fecha
    ilegible (simulaba un vídeo recién subido: hundía `age_norm` y contaba como saturación); ahora
    devuelve `None` y los consumidores filtran, que es señal ausente de verdad.

**Tests ajustados** (afirmaban el comportamiento buggy, no se añadieron tests nuevos):
`test_si_ninguna_keyword_produce_ideas_por_fallo_el_job_falla` → renombrado a
`..._se_puede_sondear...` con el mensaje nuevo, y los tres casos de `_age_days` con fecha ilegible
pasan de esperar `0.0` a esperar `None`.

**Verificación en ejecución** (no solo suite verde): worker sobreviviendo a `complete()`/`fail()`
que lanzan y atendiendo el job siguiente; job huérfano en `running` rescatado y completado, y la
rama sin intentos marcada `failed`; parada limpia a mitad de pasada (2 de 4 keywords) con el
WARNING de `stop()` cuando el hilo sigue vivo; app real con dos `POST /research/run` seguidos → el
segundo no encola y devuelve "ya había investigación en cola para: …"; `data/factory.log` creado,
rotado (3 backups) y legible como UTF-8.

### Segunda ronda: lo que destapó la suite del dedupe (2026-08-06)

11. **El dedupe de "Investigar ahora" tenía una carrera.** El arreglo del punto 7 comprobaba y
    encolaba en **dos pasos sin transacción**: `_niches_with_research_queued()` y después
    `queue.enqueue()`. Dos disparos en vuelo a la vez —doble clic, dos pestañas, o el cron de las
    07:30 coincidiendo con el botón— pasaban los dos el control y encolaban el nicho por duplicado,
    que es justo lo que el dedupe existe para evitar (~1.900 unidades de cuota por pasada sobre un
    corte de 3.200). Arreglo: lectura + inserciones dentro de un `db.transaction()`
    (`BEGIN IMMEDIATE`), pasando la conexión a `queue.enqueue(..., conn=conn)` para que todo caiga
    en la misma transacción. **Comprobar y encolar es una operación, no dos** — el mismo principio
    que ya gobierna el claim de la cola. Verificado con 6 hilos simultáneos × 12 rondas: 12/12
    limpias con el arreglo y **12/12 duplicando** al reinstalar la versión de dos pasos con el mismo
    arnés (control rojo-verde: el arnés demuestra que sabe detectar el fallo). El
    `xfail(strict=True)` que lo esperaba en `tests/test_web_server.py` queda retirado.
12. **`import app` tenía efectos secundarios.** `configure_logging()` corría a nivel de módulo:
    `logging.basicConfig(force=True)` secuestraba el logging de quien importase el módulo (pytest,
    un script) y abría un `RotatingFileHandler` sobre `data/factory.log` por el mero hecho de
    importar. Por eso los tests de la web tuvieron que montar una app paralela con el mismo router
    en vez de usar la real, y `app.py` (mount de `/static`, `include_router`, `/health`, lifespan)
    se quedaba sin cubrir. Arreglo: la llamada se mueve al lifespan — no a `main()`, porque el
    despliegue documentado es `uvicorn app:app` y ahí no pasa por `main()`. Verificado: importar
    `app` con una configuración de logging propia la deja intacta y no crea el fichero; arrancando
    de verdad, el log aparece desde la primera línea ("Aplicando migración 1") y el cierre sigue
    registrando "Worker de jobs parado".

Suite completa tras las dos rondas: **611 passed, 0 xfailed**.

### Anteriores

- **es.wikipedia devolvía 429 al sondear ~19 keywords seguidas** (se perdía la señal evergreen del
  último nicho de la pasada). Arreglado con `PAUSE_BETWEEN_KEYWORDS = 1.0` en el pipeline y
  respeto de `Retry-After` en `http_util`. Tras el arreglo, las únicas ausencias de Wikipedia son
  keywords que de verdad no tienen artículo.
- **Reddit responde 403 a esta IP** (bloqueo de red, no de User-Agent: verificado con curl contra
  `www.reddit.com` y `old.reddit.com` con UA de navegador). El sistema degrada: la señal queda
  ausente con el motivo en la DB y el scorer re-normaliza. Se resolverá con la app registrada.

## Pendientes

### Depende del usuario (nadie más puede desbloquearlo)

- **Clave de YouTube Data API v3** en Google Cloud Console (gratis, 5 min; pasos en el README).
  Es el pendiente más rentable: sin ella el motor funciona pero **le falta la mitad** — no hay
  componente de competencia ni señal de vistas, y las ideas salen solo de titulares de noticias.
- **Crear el fichero `.env` a mano** en la raíz, con las claves que lista el README. Un hook del
  setup impide que lo escriba yo, y está bien que sea así: los ficheros de entorno son del usuario.
- **Registrar la app de Reddit** (la aprobación del free tier tarda 2–4 semanas). Mientras tanto
  se usa el JSON público, que **responde 403 a esta IP** — la señal degrada con su motivo en la DB.
- **Solicitar la auditoría de cumplimiento de la API de YouTube** al llegar al hito 4: los
  proyectos nuevos suben los vídeos como privados hasta pasarla, así que conviene pedirla antes.

### Deuda técnica anotada (la resuelvo yo cuando toque)

- **`factory/research/competitor.py` no tiene llamador todavía** (225 líneas, con sus tests en
  verde). Está escrito y probado a la espera de los hitos siguientes, que son los que lo enganchan
  al pipeline. No es código muerto: es código adelantado.
- **Los jobs se muestran sin fecha en el dashboard**: `core.models.Job` no lleva
  `created_at`/`finished_at`. Para un sistema desatendido, saber si el fallo fue hoy o hace tres
  días importa; son dos campos opcionales en el dataclass y dos columnas en `_row_to_job`.
- **El presupuesto de cuota no da para un cuarto nicho**: 102 unidades por keyword (search 100 +
  videos 1 + channels 1), 19 keywords = ~1.938 de las 3.200 del corte diario. Habrá que espaciar
  los nichos por días o bajar `TOP_VIDEOS`.
- **Google News es ruidoso con keywords genéricas** ("disciplina" trae noticias de fútbol).
  Mitigación actual: el checkpoint humano. Mejora posible: exigir la frase completa en el titular
  o filtrar por sección.
- **Los subreddits de `settings.yaml` están sin verificar**: se eligieron por criterio y no se
  pudieron comprobar por el 403. Uno que no exista simplemente se salta.
- **trendspy** quedó fuera a propósito como mejora futura del scorer (fuente opcional degradable).

### Fuera de este repo

- Los especialistas creados para este proyecto (`backend-python`, `testing-python` y las reglas
  `11-python`, `22-ffmpeg`, `32-fastapi`) viven en `~/.claude/` y se copiaron al repo
  `claude-setup` para que queden versionados. Si se editan en un sitio, hay que copiarlos al otro.
