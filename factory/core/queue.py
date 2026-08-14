"""Cola de jobs sobre la tabla `jobs` + worker que despacha por tipo.

El claim es una sola operación atómica (`UPDATE ... WHERE id = (SELECT ...)
RETURNING *` dentro de `BEGIN IMMEDIATE`): dos workers nunca cogen el mismo job.

Los módulos de dominio registran sus handlers en el worker; la cola no sabe qué
hace cada tipo de job, solo despacha.

Dos garantías del worker, pensadas para una máquina desatendida: su hilo no
puede morir por ninguna excepción, y al arrancar rescata los jobs que quedaron
'running' cuando el proceso anterior se cortó (`requeue_stale_running`).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any, Callable

from factory.core.db import close_conn, get_conn, transaction
from factory.core.models import Job

logger = logging.getLogger(__name__)

# Un handler recibe el job reclamado y hace el trabajo. Si lanza, el job falla
# (y se reintenta hasta max_attempts con backoff).
Handler = Callable[[Job], None]

# Backoff exponencial entre reintentos: 30s, 60s, 120s...
RETRY_BASE_SECONDS = 30

# Señal de parada del worker que corre en ESTE hilo, para que los handlers
# largos puedan consultarla con should_stop().
_current_worker = threading.local()

# Espera por defecto al parar: cubre de sobra un job normal y deja claro en el
# log lo que pasa cuando no basta (ver JobWorker.stop).
DEFAULT_STOP_TIMEOUT = 30.0


def enqueue(
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 0,
    max_attempts: int = 3,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Encola un job y devuelve su id."""
    conn = conn or get_conn()
    cur = conn.execute(
        "INSERT INTO jobs (type, payload, priority, max_attempts) VALUES (?, ?, ?, ?)",
        (job_type, json.dumps(payload or {}, ensure_ascii=False), priority, max_attempts),
    )
    job_id = int(cur.lastrowid)
    logger.info("Job %d encolado: %s", job_id, job_type)
    return job_id


def claim_next(conn: sqlite3.Connection | None = None) -> Job | None:
    """Reclama el siguiente job pendiente (mayor prioridad, luego más antiguo).

    Atómico: el SELECT y el UPDATE son una sola sentencia bajo BEGIN IMMEDIATE.
    Devuelve None si no hay nada listo (respeta `run_after` de los reintentos).
    """
    conn = conn or get_conn()
    with transaction(conn):
        row = conn.execute(
            """
            UPDATE jobs
               SET status = 'running',
                   attempts = attempts + 1,
                   started_at = datetime('now')
             WHERE id = (
                    SELECT id FROM jobs
                     WHERE status = 'pending'
                       AND (run_after IS NULL OR run_after <= datetime('now'))
                     ORDER BY priority DESC, id
                     LIMIT 1
                   )
            RETURNING *
            """
        ).fetchone()
    return _row_to_job(row) if row is not None else None


def complete(job_id: int, conn: sqlite3.Connection | None = None) -> None:
    """Marca un job como terminado con éxito."""
    conn = conn or get_conn()
    conn.execute(
        "UPDATE jobs SET status = 'done', error = NULL, finished_at = datetime('now')"
        " WHERE id = ?",
        (job_id,),
    )


def fail(
    job_id: int,
    error: str,
    *,
    retry: bool = True,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Marca un job como fallido.

    Si quedan intentos (`attempts < max_attempts`) y `retry` es True, vuelve a
    'pending' con backoff exponencial en `run_after`. Devuelve el estado final
    ('pending' o 'failed').
    """
    conn = conn or get_conn()
    with transaction(conn):
        row = conn.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} no existe")

        puede_reintentar = retry and row["attempts"] < row["max_attempts"]
        if puede_reintentar:
            delay = RETRY_BASE_SECONDS * (2 ** max(row["attempts"] - 1, 0))
            conn.execute(
                "UPDATE jobs SET status = 'pending', error = ?,"
                " run_after = datetime('now', ? || ' seconds') WHERE id = ?",
                (error, str(delay), job_id),
            )
            estado = "pending"
        else:
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?,"
                " finished_at = datetime('now') WHERE id = ?",
                (error, job_id),
            )
            estado = "failed"
    logger.warning("Job %d falló (-> %s): %s", job_id, estado, error)
    return estado


def list_jobs(
    status: str | None = None,
    *,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[Job]:
    """Lista jobs, opcionalmente filtrados por estado, los más recientes primero."""
    conn = conn or get_conn()
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def pending_count(conn: sqlite3.Connection | None = None) -> int:
    """Número de jobs pendientes (para /health y el dashboard)."""
    conn = conn or get_conn()
    row = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'").fetchone()
    return int(row[0])


def unfinished_by_type(job_type: str, conn: sqlite3.Connection | None = None) -> list[Job]:
    """Jobs de un tipo que siguen pendientes o en ejecución.

    Sirve para no encolar dos veces el mismo trabajo (el cron y el botón del
    dashboard disparan lo mismo, y un doble clic cuesta cuota real).
    """
    conn = conn or get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE type = ? AND status IN ('pending', 'running')"
        " ORDER BY id",
        (job_type,),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def requeue_stale_running(conn: sqlite3.Connection | None = None) -> tuple[list[int], list[int]]:
    """Rescata los jobs que quedaron 'running' cuando murió el proceso anterior.

    Solo hay un worker por proceso, así que al arrancar nadie puede estar
    ejecutando nada: un 'running' es siempre el rastro de un corte (reinicio de
    Windows, apagón, parada que expiró el timeout).

    Semántica de `attempts`: **no se consume un intento extra**. El intento ya lo
    contó el claim que se interrumpió, y castigar al job por un corte de luz lo
    dejaría fallido antes de tiempo. Los que ya no tienen intentos disponibles no
    vuelven a la cola: se marcan 'failed', porque un job capaz de tumbar el
    proceso volvería a tumbarlo en cada arranque.

    Devuelve (reencolados, abandonados) con sus ids, y lo deja en el log.
    """
    conn = conn or get_conn()
    with transaction(conn):
        reencolados = [
            int(row["id"])
            for row in conn.execute(
                "UPDATE jobs SET status = 'pending', run_after = NULL, started_at = NULL,"
                " error = 'reencolado: el proceso murió mientras se ejecutaba'"
                " WHERE status = 'running' AND attempts < max_attempts"
                " RETURNING id"
            ).fetchall()
        ]
        # Lo que siga en 'running' aquí ya agotó sus intentos.
        abandonados = [
            int(row["id"])
            for row in conn.execute(
                "UPDATE jobs SET status = 'failed', finished_at = datetime('now'),"
                " error = 'abandonado: el proceso murió y no quedaban intentos'"
                " WHERE status = 'running'"
                " RETURNING id"
            ).fetchall()
        ]

    if reencolados:
        logger.warning(
            "Jobs interrumpidos por un corte, devueltos a pending sin gastar intento: %s",
            reencolados,
        )
    if abandonados:
        logger.error(
            "Jobs interrumpidos sin intentos restantes, marcados failed: %s", abandonados
        )
    return reencolados, abandonados


def should_stop() -> bool:
    """True si el worker que está ejecutando este handler ha recibido la parada.

    Un handler largo (una pasada de investigación son 19 keywords y minutos de
    red) la consulta entre pasos para poder terminar pronto. Fuera del worker
    —un script manual, un test— siempre es False.
    """
    evento: threading.Event | None = getattr(_current_worker, "stop_event", None)
    return evento is not None and evento.is_set()


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        type=row["type"],
        payload=json.loads(row["payload"] or "{}"),
        status=row["status"],
        priority=row["priority"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        run_after=row["run_after"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class JobWorker:
    """Hilo que consume la cola y despacha cada job a su handler por `type`.

    Los módulos registran handlers con `register()` antes de `start()`. Un job
    cuyo tipo no tiene handler falla sin reintento: reintentarlo no haría
    aparecer el handler.
    """

    def __init__(self, poll_interval: float = 2.0) -> None:
        self._handlers: dict[str, Handler] = {}
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register(self, job_type: str, handler: Handler) -> None:
        """Registra el handler de un tipo de job. Registrar dos veces es un bug."""
        if job_type in self._handlers:
            raise ValueError(f"Ya hay un handler registrado para {job_type!r}")
        self._handlers[job_type] = handler

    def start(self) -> None:
        """Arranca el hilo del worker, rescatando antes los jobs de un corte."""
        self._recover_interrupted_jobs()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="job-worker", daemon=True)
        self._thread.start()
        logger.info("Worker de jobs arrancado (handlers: %s)", sorted(self._handlers) or "ninguno")

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        """Pide la parada y espera a que el job en curso termine.

        Si el timeout expira, lo dice: el hilo sigue vivo y su job quedará
        'running' hasta que `requeue_stale_running()` lo rescate al arrancar.
        Dar por parado lo que no lo está es exactamente la mentira que hace que
        nadie investigue por qué un job lleva tres días en 'running'.
        """
        self._stop.set()
        if self._thread is None:
            return
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning(
                "El worker sigue ocupado tras %.0fs de espera: su job quedará"
                " 'running' y se reencolará en el próximo arranque",
                timeout,
            )
            return
        logger.info("Worker de jobs parado")

    # ------------------------------------------------------------------

    def _recover_interrupted_jobs(self) -> None:
        """Reencola los 'running' huérfanos. Si no se puede, arrancar igual."""
        try:
            requeue_stale_running()
        except Exception:
            logger.exception("No se pudieron rescatar los jobs interrumpidos")

    def _run(self) -> None:
        """Bucle del worker. Ninguna excepción puede matar este hilo.

        Si muriese, los jobs se quedarían 'pending' para siempre sin que nadie
        lo note: en un sistema desatendido eso es el fallo más caro que hay.
        """
        _current_worker.stop_event = self._stop
        try:
            while not self._stop.is_set():
                job = self._process_one_guarded()
                if job is None:
                    # Cola vacía: dormir sin bloquear la parada.
                    self._stop.wait(self._poll_interval)
        finally:
            _current_worker.stop_event = None
            close_conn()

    def _process_one_guarded(self) -> Job | None:
        """`_process_one` con red debajo: registra cualquier fallo y sigue."""
        try:
            return self._process_one()
        except Exception:
            logger.exception("Fallo inesperado en el bucle del worker")
            self._stop.wait(self._poll_interval)
            return None

    def _process_one(self) -> Job | None:
        """Reclama y ejecuta un job. Devuelve el job procesado, o None si no había."""
        try:
            job = claim_next()
        except Exception:
            # P. ej. base bloqueada más allá del busy_timeout: registrar y seguir.
            logger.exception("Error reclamando job")
            self._stop.wait(self._poll_interval)
            return None
        if job is None:
            return None

        assert job.id is not None
        handler = self._handlers.get(job.type)
        if handler is None:
            _fail_safely(job.id, f"handler no registrado para el tipo {job.type!r}", retry=False)
            return job

        try:
            handler(job)
        except Exception as exc:  # bucle exterior de worker: el proceso no puede caer
            logger.exception("Job %d (%s) falló", job.id, job.type)
            _fail_safely(job.id, f"{type(exc).__name__}: {exc}")
        else:
            _complete_safely(job.id, job.type)
        return job


def _complete_safely(job_id: int, job_type: str) -> None:
    """Marca el job como hecho sin que un fallo de la base mate al worker."""
    try:
        complete(job_id)
    except Exception:
        logger.exception("No se pudo marcar como completado el job %d", job_id)
        return
    logger.info("Job %d (%s) completado", job_id, job_type)


def _fail_safely(job_id: int, error: str, *, retry: bool = True) -> None:
    """Registra el fallo del job sin que un fallo de la base mate al worker."""
    try:
        fail(job_id, error, retry=retry)
    except Exception:
        logger.exception("No se pudo registrar el fallo del job %d", job_id)
