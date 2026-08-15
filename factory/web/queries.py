"""Acceso a datos del dashboard: aquí vive todo el SQL de la capa web.

El dashboard es un consumidor más del blackboard: lee `ideas`, `jobs` y
`api_usage` y no importa de ningún módulo de dominio (`research/` incluido).
Devuelve estructuras ya listas para la plantilla, para que `server.py` se quede
solo con las rutas y las plantillas no tengan lógica.

Lo único que escribe la web es la decisión sobre una idea —aprobarla o
descartarla—, y vive también aquí para no repartir el SQL en dos ficheros.
Aprobar además encola el guion, en la misma transacción.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from factory.core import config, queue
from factory.core.db import transaction
from factory.core.quota import CUTOFF_RATIO, usage_today

logger = logging.getLogger(__name__)

# Estados en los que una idea sigue esperando decisión humana.
PENDING_STATUSES: tuple[str, ...] = ("new", "shortlisted")

# Estados a los que el dashboard mueve una idea desde el ranking. Aprobar la
# deja en 'used', no en 'approved': 'approved' no distingue "esperando guion" de
# "ya tiene guion", y esa distinción es justo la que impide encolar dos veces.
REJECTED_STATUS = "rejected"
USED_STATUS = "used"

# Estados en los que la decisión ya está tomada y el dashboard no la revisa.
# 'used': su guion está en cola o escrito, y cambiarla dejaría el video huérfano
# de la decisión que lo originó.
# 'rejected': descartar es definitivo. Sin esto, aprobar una idea descartada la
# blanqueaba a 'used' y encolaba su guion, saltándose la guarda del writer, que
# no escribe desde 'rejected' precisamente porque el humano ya dijo que no.
# `research/pipeline` ya trata los dos como terminales y no los repropone.
LOCKED_STATUSES: frozenset[str] = frozenset({USED_STATUS, REJECTED_STATUS})

# Tipo del job que escribe el guion. La cadena está repetida a propósito con
# `factory.script.writer.JOB_TYPE`: la web no importa de un módulo de dominio,
# se hablan por la base (blackboard). Es el mismo trato que tienen
# `core.scheduler.RESEARCH_JOB_ID` y `research.pipeline.JOB_TYPE`.
WRITE_SCRIPT_JOB_TYPE = "write_script"

# Qué fuente alimenta cada sub-señal de cada componente del score. Sirve para
# explicar en el desglose POR QUÉ falta un trozo: `score_details.missing_signals`
# nombra sub-senales y `missing_reasons` nombra fuentes.
COMPONENT_SIGNALS: dict[str, dict[str, str]] = {
    "demand": {"views": "youtube", "momentum": "news", "reddit": "reddit"},
    "competition": {
        "small_channels": "youtube",
        "age": "youtube",
        "room_left": "youtube",
    },
    "evergreen": {"evergreen": "wikipedia"},
    "cpm": {"cpm": "cpm"},
}

COMPONENT_LABELS: dict[str, str] = {
    "demand": "Demanda",
    "competition": "Hueco de competencia",
    "evergreen": "Evergreen",
    "cpm": "CPM del nicho",
}


class IdeaNotFound(LookupError):
    """No existe ninguna idea con ese id."""


class IdeaLocked(RuntimeError):
    """La idea está en un estado que el dashboard no puede cambiar."""


@dataclass(frozen=True, slots=True)
class MissingSignal:
    """Una señal que no llegó, con el motivo tal como se registró en la DB."""

    signal: str
    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class ComponentView:
    """Un componente del score con su valor, su peso efectivo y sus ausencias."""

    name: str
    label: str
    value: float | None       # 0..1; None = el componente entero se quedó sin senales
    weight: float | None      # peso efectivo tras re-normalizar; None si no contó
    missing: list[MissingSignal]


@dataclass(frozen=True, slots=True)
class IdeaView:
    """Una fila del ranking de ideas, ya lista para pintar."""

    id: int
    title: str
    niche: str
    keyword: str | None
    source: str | None
    score: float
    status: str
    suggested_format: str | None
    updated_at: str
    components: list[ComponentView]


@dataclass(frozen=True, slots=True)
class ApprovedIdea:
    """La idea que se acaba de aprobar y el job de guion que salió con ella."""

    id: int
    title: str
    job_id: int


@dataclass(frozen=True, slots=True)
class QuotaView:
    """Consumo de hoy de una API contra su presupuesto y su corte."""

    api: str
    used: int
    budget: int
    cutoff: int
    pct_of_cutoff: float


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------


def ranked_ideas(
    conn: sqlite3.Connection,
    *,
    statuses: Sequence[str] = PENDING_STATUSES,
    limit: int = 200,
) -> list[IdeaView]:
    """Ideas pendientes de decisión, de mayor a menor score."""
    marcadores = ", ".join("?" for _ in statuses)
    filas = conn.execute(
        f"""
        SELECT id, title, niche, keyword, source, score, status,
               suggested_format, score_details, updated_at
          FROM ideas
         WHERE status IN ({marcadores})
         ORDER BY score DESC, id
         LIMIT ?
        """,
        (*statuses, limit),
    ).fetchall()
    return [_to_idea_view(fila) for fila in filas]


def count_ideas_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Cuántas ideas hay en cada estado (para la cabecera del ranking)."""
    filas = conn.execute(
        "SELECT status, COUNT(*) AS n FROM ideas GROUP BY status"
    ).fetchall()
    return {fila["status"]: int(fila["n"]) for fila in filas}


def approve_idea_for_script(conn: sqlite3.Connection, idea_id: int) -> ApprovedIdea:
    """Aprueba una idea y encola su guion, todo en la misma transacción.

    La idea queda en 'used' y no en 'approved' porque 'approved' no dice si el
    guion ya se pidió. Como 'used' está en `LOCKED_STATUSES`, la segunda
    aprobación de la misma idea choca con `IdeaLocked` antes de encolar nada:
    ese es el dedupe del doble clic, de las dos pestañas y del botón "atrás".

    Leer el estado, escribirlo y encolar el job son UNA operación, no tres: van
    juntas bajo `BEGIN IMMEDIATE`. En pasos separados, dos peticiones en vuelo a
    la vez pasan las dos el control y encolan dos guiones, y cada guion es una
    generación completa de Gemini.

    La fila de `videos` no se crea aquí: la escribe el job cuando el guion
    existe de verdad. Una fila vacía esperando guion es un estado que nadie
    limpia si el job falla.
    """
    with transaction(conn):
        fila = _idea_que_admite_decision(conn, idea_id)
        conn.execute(
            "UPDATE ideas SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (USED_STATUS, idea_id),
        )
        # Dos intentos, no los tres por defecto: cada intento de este job es una
        # generación de Gemini pagada, y encima cada uno reintenta ya por dentro
        # (`llm.MAX_ATTEMPTS`), así que el peor caso son intentos x reintentos
        # peticiones seguidas contra un free tier de 10 por minuto. Lo que se cura
        # reintentando —un 503 de Gemini— se curó al segundo intento cuando pasó
        # de verdad; el tercero solo repite un fallo determinista (un título que
        # bloquea el filtro de seguridad) gastando la ventana que otro job necesita.
        job_id = queue.enqueue(
            WRITE_SCRIPT_JOB_TYPE, {"idea_id": idea_id}, max_attempts=2, conn=conn
        )
    logger.info("Idea %d aprobada -> %s, guion encolado en el job %d",
                idea_id, USED_STATUS, job_id)
    return ApprovedIdea(id=idea_id, title=str(fila["title"]), job_id=job_id)


def update_idea_status(conn: sqlite3.Connection, idea_id: int, new_status: str) -> str:
    """Cambia el estado de una idea y devuelve su título, para el mensaje de vuelta.

    Solo descarta: aprobar escribe además en `jobs` y tiene su propia función
    (`approve_idea_for_script`). Dejar aquí un camino que mueva la idea a
    'approved' sin encolar el guion sería volver al estado ambiguo del que
    venimos.

    Descartar dos veces la misma idea NO es idempotente: el segundo intento
    choca con `IdeaLocked` igual que la segunda aprobación, porque 'rejected'
    está en `LOCKED_STATUSES`. Es el mismo trato que el doble clic en aprobar y
    la idea ya está descartada de todas formas.

    Lee y luego escribe, así que va bajo `BEGIN IMMEDIATE`: una transacción
    DEFERRED que empieza leyendo falla al instante con "database is locked".
    """
    if new_status != REJECTED_STATUS:
        raise ValueError(f"el dashboard no mueve ideas a {new_status!r}")

    with transaction(conn):
        fila = _idea_que_admite_decision(conn, idea_id)
        conn.execute(
            "UPDATE ideas SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (new_status, idea_id),
        )
    logger.info("Idea %d -> %s", idea_id, new_status)
    return str(fila["title"])


def _idea_que_admite_decision(conn: sqlite3.Connection, idea_id: int) -> sqlite3.Row:
    """Fila de la idea si su estado todavía admite una decisión del dashboard.

    Se llama SIEMPRE dentro de la transacción que va a escribir: leer fuera y
    escribir después es la carrera que ya mordió dos veces en este repo.
    """
    fila = conn.execute(
        "SELECT title, status FROM ideas WHERE id = ?", (idea_id,)
    ).fetchone()
    if fila is None:
        raise IdeaNotFound(f"no existe la idea {idea_id}")
    if fila["status"] in LOCKED_STATUSES:
        raise IdeaLocked(
            f"la idea {idea_id} está en estado {fila['status']!r} y ya no se cambia"
        )
    return fila


def _to_idea_view(fila: sqlite3.Row) -> IdeaView:
    """Fila cruda de `ideas` → estructura de presentación."""
    detalles = _load_details(fila["score_details"], fila["id"])
    return IdeaView(
        id=int(fila["id"]),
        title=fila["title"],
        niche=fila["niche"],
        keyword=fila["keyword"],
        source=fila["source"],
        score=float(fila["score"] or 0.0),
        status=fila["status"],
        suggested_format=fila["suggested_format"],
        updated_at=fila["updated_at"],
        components=_component_views(detalles),
    )


def _load_details(raw: str | None, idea_id: int) -> dict[str, Any]:
    """`score_details` decodificado. Un JSON roto no puede tumbar el dashboard."""
    if not raw:
        return {}
    try:
        detalles = json.loads(raw)
    except ValueError:
        logger.warning("score_details ilegible en la idea %d", idea_id)
        return {}
    return detalles if isinstance(detalles, dict) else {}


def _component_views(detalles: dict[str, Any]) -> list[ComponentView]:
    """Desglose del score: un componente por fila, con sus senales ausentes."""
    valores: dict[str, Any] = detalles.get("components") or {}
    pesos: dict[str, Any] = detalles.get("weights_used") or {}
    ausentes: set[str] = set(detalles.get("missing_signals") or [])
    motivos: dict[str, str] = detalles.get("missing_reasons") or {}

    vistas: list[ComponentView] = []
    for name, senales in COMPONENT_SIGNALS.items():
        vistas.append(
            ComponentView(
                name=name,
                label=COMPONENT_LABELS[name],
                value=_as_float(valores.get(name)),
                weight=_as_float(pesos.get(name)),
                missing=_missing_signals(senales, ausentes, motivos),
            )
        )
    return vistas


def _missing_signals(
    senales: dict[str, str], ausentes: set[str], motivos: dict[str, str]
) -> list[MissingSignal]:
    """Las sub-senales de un componente que faltaron, con su motivo registrado."""
    faltan: list[MissingSignal] = []
    for signal, source in senales.items():
        if signal not in ausentes:
            continue
        faltan.append(
            MissingSignal(
                signal=signal,
                source=source,
                reason=motivos.get(source, "sin motivo registrado"),
            )
        )
    return faltan


def _as_float(value: Any) -> float | None:
    """Número del JSON a float, tolerando None y basura."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Cuota
# ---------------------------------------------------------------------------


def quota_today(conn: sqlite3.Connection) -> list[QuotaView]:
    """Consumo de hoy de cada API con presupuesto configurado."""
    vistas: list[QuotaView] = []
    for api in config.quota_apis():
        budget = config.daily_budget(api)
        cutoff = int(budget * CUTOFF_RATIO)
        used = usage_today(api, conn)
        vistas.append(
            QuotaView(
                api=api,
                used=used,
                budget=budget,
                cutoff=cutoff,
                pct_of_cutoff=100.0 * used / cutoff if cutoff > 0 else 0.0,
            )
        )
    return vistas
