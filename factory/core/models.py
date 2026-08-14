"""Entidades principales y máquina de estados de `videos`.

Modelos puros: no tocan la base de datos. Sirven para pasar filas tipadas entre
funciones y para validar transiciones de estado antes de escribirlas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Máquina de estados de un video
# ---------------------------------------------------------------------------

# Camino feliz:
#   idea_approved → script_draft → script_approved → producing → video_ready
#   → video_approved → scheduled → published → measured
#
# Desvíos:
#   - rejected: el humano descarta en un checkpoint (guion o video final).
#   - failed: la producción o la subida revientan; desde ahí se puede reintentar
#     volviendo al estado que falló.
VIDEO_TRANSITIONS: dict[str, frozenset[str]] = {
    "idea_approved":   frozenset({"script_draft", "rejected"}),
    "script_draft":    frozenset({"script_approved", "rejected"}),
    "script_approved": frozenset({"producing", "rejected"}),
    "producing":       frozenset({"video_ready", "failed"}),
    "video_ready":     frozenset({"video_approved", "rejected"}),
    "video_approved":  frozenset({"scheduled", "rejected"}),
    "scheduled":       frozenset({"published", "failed"}),
    "published":       frozenset({"measured"}),
    "measured":        frozenset(),  # terminal
    "rejected":        frozenset(),  # terminal
    "failed":          frozenset({"producing", "scheduled"}),  # reintento manual
}

VIDEO_STATUSES: frozenset[str] = frozenset(VIDEO_TRANSITIONS)

IDEA_STATUSES: frozenset[str] = frozenset({"new", "shortlisted", "approved", "rejected", "used"})
JOB_STATUSES: frozenset[str] = frozenset({"pending", "running", "done", "failed"})


class IllegalTransition(ValueError):
    """Transición de estado no permitida por la máquina de estados."""


def can_transition(current: str, new: str) -> bool:
    """True si el paso `current` → `new` es legal."""
    return new in VIDEO_TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, new: str) -> None:
    """Lanza `IllegalTransition` si el paso `current` → `new` no es legal."""
    if current not in VIDEO_TRANSITIONS:
        raise IllegalTransition(f"Estado de origen desconocido: {current!r}")
    if new not in VIDEO_TRANSITIONS:
        raise IllegalTransition(f"Estado de destino desconocido: {new!r}")
    if not can_transition(current, new):
        legales = sorted(VIDEO_TRANSITIONS[current]) or ["(ninguno: es terminal)"]
        raise IllegalTransition(
            f"Transición ilegal {current!r} -> {new!r}; desde {current!r} solo: {legales}"
        )


# ---------------------------------------------------------------------------
# Entidades (espejo tipado de las filas; ver esquema en core/db.py)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Idea:
    """Oportunidad de contenido detectada por el motor de investigación."""

    id: int | None = None
    title: str = ""
    niche: str = ""
    keyword: str | None = None
    source: str | None = None
    demand: float | None = None
    competition: float | None = None
    evergreen: float | None = None
    cpm_factor: float | None = None
    score: float | None = None
    score_details: dict[str, Any] = field(default_factory=dict)
    status: str = "new"
    suggested_format: str | None = None


@dataclass(slots=True)
class Video:
    """Un video (largo o short) a lo largo de todo su ciclo de vida."""

    id: int | None = None
    idea_id: int | None = None
    channel: str | None = None
    kind: str = "long"
    parent_video_id: int | None = None
    format: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    chapters: str | None = None
    pinned_comment: str | None = None
    script_json: dict[str, Any] = field(default_factory=dict)
    status: str = "idea_approved"
    video_path: str | None = None
    thumbnail_path: str | None = None
    yt_video_id: str | None = None
    scheduled_at: str | None = None
    published_at: str | None = None


@dataclass(slots=True)
class Job:
    """Unidad de trabajo en la cola (tabla `jobs`)."""

    id: int | None = None
    type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 3
    run_after: str | None = None
    error: str | None = None
    # Marcas de tiempo que escribe la propia cola (texto UTC de SQLite). Sin
    # ellas el dashboard no puede distinguir un fallo de hoy de uno de hace
    # tres días, que es el diagnóstico básico de un sistema desatendido.
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
