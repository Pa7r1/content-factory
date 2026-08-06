"""APScheduler 3.x en el proceso: los jobs periódicos ENCOLAN, nunca ejecutan.

Trabajo pesado dentro de un job del scheduler bloquearía su executor y haría
perder la siguiente ejecución. Aquí cada disparo inserta una fila en `jobs` y
termina en milisegundos; el worker (core/queue.py) hace el trabajo real.

La PC de producción es un Windows que suspende: `coalesce=True` colapsa las
ejecuciones perdidas acumuladas en una sola, y `misfire_grace_time` decide
hasta cuándo tiene sentido recuperarla.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from factory.core import config, queue
from factory.core.db import close_conn, get_conn, transaction

logger = logging.getLogger(__name__)


RESEARCH_JOB_ID = "research_daily"


@dataclass(frozen=True, slots=True)
class ResearchEnqueue:
    """Qué se encoló y qué nichos se omitieron por tenerlo ya en cola."""

    job_ids: list[int] = field(default_factory=list)
    skipped_niches: list[str] = field(default_factory=list)


def enqueue_research_daily() -> ResearchEnqueue:
    """Encola la investigación: un job por nicho activo que no la tenga ya en cola.

    Pública porque el botón "Investigar ahora" del dashboard dispara exactamente
    lo mismo que el cron de las 07:30, y hacerlo por dos caminos distintos sería
    la forma más rápida de que dejen de coincidir.

    El dedupe vive aquí, no en la web, para que proteja también al cron: una
    pasada de un nicho cuesta ~1900 unidades de cuota de YouTube y el corte
    diario está en 3200, así que dos clics seguidos revientan el presupuesto.

    Comprobar y encolar es UNA operación, no dos: van juntas bajo
    `BEGIN IMMEDIATE`. En dos pasos, dos disparos en vuelo a la vez (doble clic,
    dos pestañas, o el cron de las 07:30 coincidiendo con el botón) pasan los
    dos el control y encolan el nicho por duplicado.
    """
    conn = get_conn()
    resultado = ResearchEnqueue()
    with transaction(conn):
        en_cola = _niches_with_research_queued(conn)
        for niche_id in config.niches():
            if niche_id in en_cola:
                resultado.skipped_niches.append(niche_id)
                continue
            resultado.job_ids.append(
                queue.enqueue(RESEARCH_JOB_ID, {"niche": niche_id}, conn=conn)
            )

    if resultado.skipped_niches:
        logger.info(
            "Investigación ya en cola, no se reencola: %s",
            ", ".join(resultado.skipped_niches),
        )
    return resultado


def _niches_with_research_queued(conn: sqlite3.Connection) -> set[str]:
    """Nichos con un `research_daily` pendiente o en ejecución ahora mismo."""
    nichos = set()
    for job in queue.unfinished_by_type(RESEARCH_JOB_ID, conn):
        niche_id = job.payload.get("niche")
        if niche_id:
            nichos.add(niche_id)
    return nichos


def _enqueue_research_daily() -> None:
    """Disparo del cron: encola y suelta la conexión del hilo del executor."""
    try:
        enqueue_research_daily()
    finally:
        close_conn()  # el hilo del executor de APScheduler no debe acumular conexiones


def _enqueue_fetch_metrics() -> None:
    """Encola la recogida de métricas. Aún sin handler: el worker lo marcará
    como fallido con "handler no registrado" hasta que llegue analytics/ (hito 5)."""
    try:
        queue.enqueue("fetch_metrics", {})
    finally:
        close_conn()


def build_scheduler() -> BackgroundScheduler:
    """Construye el scheduler con las tareas periódicas leyendo horas de config.

    No lo arranca: `app.py` lo hace dentro del lifespan.
    """
    scheduler = BackgroundScheduler()

    hour, minute = config.schedule_time("research_daily")
    scheduler.add_job(
        _enqueue_research_daily,
        CronTrigger(hour=hour, minute=minute),
        id=RESEARCH_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # La investigación de la mañana sigue valiendo por la tarde: si la PC
        # despierta dentro de las 6 horas siguientes, se recupera; más tarde no
        # (mejor esperar al día siguiente que investigar de madrugada).
        misfire_grace_time=6 * 3600,
    )

    hour, minute = config.schedule_time("fetch_metrics")
    scheduler.add_job(
        _enqueue_fetch_metrics,
        CronTrigger(hour=hour, minute=minute),
        id="fetch_metrics",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # Las métricas son snapshots por día: recuperarlas vale casi todo el
        # día, pero no invadir el día siguiente (12 h).
        misfire_grace_time=12 * 3600,
    )

    logger.info(
        "Scheduler construido: research_daily %02d:%02d, fetch_metrics %02d:%02d",
        *config.schedule_time("research_daily"),
        *config.schedule_time("fetch_metrics"),
    )
    return scheduler
