"""Acceso a datos del dashboard: aquí vive todo el SQL de la capa web.

El dashboard es un consumidor más del blackboard: lee `ideas`, `jobs` y
`api_usage` y no importa de ningún módulo de dominio (`research/` incluido).
Devuelve estructuras ya listas para la plantilla, para que `server.py` se quede
solo con las rutas y las plantillas no tengan lógica.

La única escritura que hace la web es cambiar el estado de una idea, y vive
también aquí (`update_idea_status`) para no repartir el SQL en dos ficheros.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from factory.core import config
from factory.core.db import transaction
from factory.core.quota import CUTOFF_RATIO, usage_today

logger = logging.getLogger(__name__)

# Estados en los que una idea sigue esperando decisión humana.
PENDING_STATUSES: tuple[str, ...] = ("new", "shortlisted")

# Estados a los que el dashboard puede mover una idea desde el ranking.
APPROVED_STATUS = "approved"
REJECTED_STATUS = "rejected"

# Una idea ya convertida en video no vuelve al ranking: cambiarla dejaría el
# video huérfano de la decisión que lo originó.
LOCKED_STATUSES: frozenset[str] = frozenset({"used"})

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


def update_idea_status(conn: sqlite3.Connection, idea_id: int, new_status: str) -> str:
    """Cambia el estado de una idea y devuelve su título, para el mensaje de vuelta.

    Lee y luego escribe, así que va bajo `BEGIN IMMEDIATE`: una transacción
    DEFERRED que empieza leyendo falla al instante con "database is locked".
    """
    if new_status not in (APPROVED_STATUS, REJECTED_STATUS):
        raise ValueError(f"el dashboard no mueve ideas a {new_status!r}")

    with transaction(conn):
        fila = conn.execute(
            "SELECT title, status FROM ideas WHERE id = ?", (idea_id,)
        ).fetchone()
        if fila is None:
            raise IdeaNotFound(f"no existe la idea {idea_id}")
        if fila["status"] in LOCKED_STATUSES:
            raise IdeaLocked(
                f"la idea {idea_id} está en estado {fila['status']!r} y ya no se cambia"
            )
        conn.execute(
            "UPDATE ideas SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (new_status, idea_id),
        )
    logger.info("Idea %d -> %s", idea_id, new_status)
    return str(fila["title"])


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
