"""Acceso a datos del dashboard: aquí vive todo el SQL de la capa web.

El dashboard es un consumidor más del blackboard: lee `ideas`, `jobs` y
`api_usage` y no importa de ningún módulo de dominio (`research/` incluido).
Devuelve estructuras ya listas para la plantilla, para que `server.py` se quede
solo con las rutas y las plantillas no tengan lógica.

Lo que escribe la web son las dos decisiones del humano que hay hasta hoy: la
que toma sobre una idea —aprobarla o descartarla— y la que toma sobre un guion
—editarlo, aprobarlo o descartarlo—. Viven también aquí para no repartir el SQL
en dos ficheros. Aprobar una idea además encola su guion, en la misma
transacción.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from factory.core import config, pacing, queue
from factory.core.db import transaction
from factory.core.models import assert_transition
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

# Los tres estados de `videos` entre los que se mueve el checkpoint del guion.
# La máquina de estados que los valida vive en `core/models.py`; aquí solo se
# nombran para no repetir la cadena por el fichero.
SCRIPT_DRAFT_STATUS = "script_draft"
SCRIPT_APPROVED_STATUS = "script_approved"
SCRIPT_REJECTED_STATUS = "rejected"

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


class ScriptNotFound(LookupError):
    """No existe ningún video con ese id."""


class ScriptLocked(RuntimeError):
    """El guion ya no espera decisión: alguien la tomó antes."""


class ScriptInvalid(ValueError):
    """La edición que llegó del formulario dejaría el guion inservible."""


class JobNotFound(LookupError):
    """No existe ningún job con ese id."""


class JobNotRetryable(RuntimeError):
    """El job no está 'failed', o ya hay uno igual pendiente o en ejecución."""


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
class ScriptSummary:
    """Una línea de la lista de guiones que esperan el checkpoint humano."""

    video_id: int
    title: str
    niche: str | None
    format: str | None
    chapter_count: int
    word_count: int | None
    estimated_minutes: float | None
    edited: bool
    updated_at: str


@dataclass(frozen=True, slots=True)
class ScriptChapterView:
    """Un capítulo tal como se lee y se edita en el checkpoint."""

    number: int
    title: str
    narration: str
    words: int
    start_mmss: str


@dataclass(frozen=True, slots=True)
class ScriptDetail:
    """El guion entero, listo para leerlo y para rellenar el editor."""

    video_id: int
    title: str
    niche: str | None
    format: str | None
    model: str | None
    generated_at: str | None
    edited: bool
    hook: str
    chapters: list[ScriptChapterView]
    outro: str
    word_count: int | None
    estimated_minutes: float | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ChapterEdit:
    """Un capítulo tal como lo devuelve el formulario del editor."""

    title: str
    narration: str


@dataclass(frozen=True, slots=True)
class ScriptEdit:
    """El texto que el humano dejó en el editor, ya limpio."""

    hook: str
    outro: str
    chapters: tuple[ChapterEdit, ...]


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
# Guiones: el checkpoint humano
# ---------------------------------------------------------------------------


def scripts_awaiting_review(
    conn: sqlite3.Connection, *, limit: int = 50
) -> list[ScriptSummary]:
    """Guiones en borrador, el que lleva más tiempo esperando primero.

    El nicho sale de la idea con un LEFT JOIN: `videos.idea_id` admite NULL y un
    guion sin idea detrás tiene que seguir apareciendo aquí. Si desaparece de la
    lista, deja de existir para el humano y el video se queda parado para siempre.
    """
    filas = conn.execute(
        """
        SELECT v.id, v.title, v.format, v.script_json, v.updated_at, i.niche
          FROM videos v
          LEFT JOIN ideas i ON i.id = v.idea_id
         WHERE v.status = ?
         ORDER BY v.updated_at, v.id
         LIMIT ?
        """,
        (SCRIPT_DRAFT_STATUS, limit),
    ).fetchall()
    return [_to_script_summary(fila) for fila in filas]


def script_for_review(conn: sqlite3.Connection, video_id: int) -> ScriptDetail:
    """El guion completo para leerlo y editarlo, si sigue esperando decisión."""
    fila = conn.execute(
        """
        SELECT v.id, v.title, v.status, v.format, v.script_json, v.updated_at,
               i.niche
          FROM videos v
          LEFT JOIN ideas i ON i.id = v.idea_id
         WHERE v.id = ?
        """,
        (video_id,),
    ).fetchone()
    _assert_en_borrador(fila, video_id)
    return _to_script_detail(fila)


def save_script_draft(
    conn: sqlite3.Connection, video_id: int, edit: ScriptEdit
) -> str:
    """Guarda la edición sin decidir nada: el guion sigue en borrador.

    Existe para que el humano pueda dejar a medias un guion de 1.400 palabras sin
    tener que aprobarlo para no perder lo escrito.
    """
    with transaction(conn):
        fila = _guion_en_borrador(conn, video_id)
        guion = _with_edit(_script_json(fila), edit)
        _write_script(conn, video_id, guion, SCRIPT_DRAFT_STATUS)
    logger.info("Guion del video %d guardado; sigue en borrador", video_id)
    return str(fila["title"] or "")


def approve_script(
    conn: sqlite3.Connection, video_id: int, edit: ScriptEdit
) -> str:
    """Guarda la edición y aprueba el guion, todo en la misma transacción.

    Leer el estado, escribir el texto y mover el video son UNA operación bajo
    `BEGIN IMMEDIATE`, no tres: en pasos separados, dos pestañas abiertas sobre
    el mismo guion pasan las dos el control y la segunda pisa el texto de un
    video que ya estaba aprobado. Es la misma carrera que ya mordió en el claim
    de la cola y en el dedupe de investigación.
    """
    with transaction(conn):
        fila = _guion_en_borrador(conn, video_id)
        assert_transition(fila["status"], SCRIPT_APPROVED_STATUS)
        guion = _with_edit(_script_json(fila), edit)
        _write_script(conn, video_id, guion, SCRIPT_APPROVED_STATUS)
    logger.info("Guion del video %d aprobado -> %s", video_id, SCRIPT_APPROVED_STATUS)
    return str(fila["title"] or "")


def reject_script(conn: sqlite3.Connection, video_id: int) -> str:
    """Descarta el guion: el video no se produce.

    No guarda lo que hubiera en el editor. Quien descarta no quiere conservar su
    edición, y escribirla dejaría en la base un texto que nadie va a volver a
    mirar como si fuera el guion bueno.
    """
    with transaction(conn):
        fila = _guion_en_borrador(conn, video_id)
        assert_transition(fila["status"], SCRIPT_REJECTED_STATUS)
        conn.execute(
            "UPDATE videos SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (SCRIPT_REJECTED_STATUS, video_id),
        )
    logger.info("Guion del video %d descartado -> %s", video_id, SCRIPT_REJECTED_STATUS)
    return str(fila["title"] or "")


def rejected_scripts(conn: sqlite3.Connection, *, limit: int = 50) -> list[ScriptSummary]:
    """Guiones descartados con una idea detrás, los más recientes primero.

    Sin `idea_id` no hay a qué idea reencolarle un guion nuevo, así que esos
    quedan fuera de esta lista: no hay ninguna acción posible sobre ellos.
    """
    filas = conn.execute(
        """
        SELECT v.id, v.title, v.format, v.script_json, v.updated_at, i.niche
          FROM videos v
          LEFT JOIN ideas i ON i.id = v.idea_id
         WHERE v.status = ? AND v.idea_id IS NOT NULL
         ORDER BY v.updated_at DESC, v.id DESC
         LIMIT ?
        """,
        (SCRIPT_REJECTED_STATUS, limit),
    ).fetchall()
    return [_to_script_summary(fila) for fila in filas]


def rewrite_script(conn: sqlite3.Connection, video_id: int) -> int:
    """Pide un guion nuevo para la idea de un guion rechazado. Devuelve el job id.

    El guion viejo se queda en 'rejected' como historial: `writer._existing_video`
    ya no lo cuenta como "hay guion" para esa idea (excluye 'rejected' a
    propósito), así que el job nuevo crea una fila fresca en 'script_draft' en
    vez de chocar con la vieja.

    Se reescribe una sola vez: si la idea ya tiene otro guion vivo, esto corta
    aquí en vez de encolar un job que el writer va a descartar en silencio.
    Leerlo y encolar van juntos bajo `BEGIN IMMEDIATE`, que es lo que hace que
    dos clics simultáneos no paguen dos generaciones de Gemini.
    """
    with transaction(conn):
        fila = conn.execute(
            "SELECT idea_id, status FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        if fila is None:
            raise ScriptNotFound(f"no existe el video {video_id}")
        if fila["status"] != SCRIPT_REJECTED_STATUS:
            raise ScriptLocked(
                f"el guion del video {video_id} está en estado {fila['status']!r}"
                " y solo se reescribe un guion descartado"
            )
        idea_id = fila["idea_id"]
        if idea_id is None:
            raise ScriptLocked(
                f"el video {video_id} no tiene una idea asociada: no hay a qué reescribir"
            )
        # La idea ya reescrita tiene una fila viva, y el writer no la pisa: sin
        # esta comprobación el job se encola, no escribe nada y solo deja un
        # WARNING en el log, mientras el humano lee "en reescritura" y espera
        # algo que no va a pasar. Sucede con la página sin refrescar y con el
        # botón "atrás", que siguen enseñando la fila descartada y su botón.
        vivo = _video_vivo_de_la_idea(conn, idea_id)
        if vivo is not None:
            raise ScriptLocked(
                f"la idea {idea_id} ya tiene otro guion en estado"
                f" {vivo['status']!r} (video {vivo['id']}): no se reescribe"
            )
        if _ya_hay_en_curso(conn, WRITE_SCRIPT_JOB_TYPE, {"idea_id": idea_id}):
            raise ScriptLocked(f"ya hay un guion en camino para la idea {idea_id}")
        job_id = queue.enqueue(
            WRITE_SCRIPT_JOB_TYPE, {"idea_id": idea_id}, max_attempts=2, conn=conn
        )
    logger.info(
        "Guion del video %d (idea %d) en reescritura -> job %d", video_id, idea_id, job_id
    )
    return job_id


def _video_vivo_de_la_idea(
    conn: sqlite3.Connection, idea_id: int
) -> sqlite3.Row | None:
    """El video largo de la idea que sigue en pie, si lo hay.

    Mismo criterio que `writer._existing_video`: solo el largo (los shorts
    cuelgan de él y no tienen guion propio) y sin contar los descartados, que
    son historial. La cadena 'rejected' está repetida a propósito con la del
    writer: la web no importa de un módulo de dominio.
    """
    return conn.execute(
        "SELECT id, status FROM videos WHERE idea_id = ? AND kind = 'long'"
        " AND status != ? ORDER BY id LIMIT 1",
        (idea_id, SCRIPT_REJECTED_STATUS),
    ).fetchone()


def _guion_en_borrador(conn: sqlite3.Connection, video_id: int) -> sqlite3.Row:
    """Fila del video si su guion sigue esperando decisión.

    Se llama SIEMPRE dentro de la transacción que va a escribir: comprobar fuera
    y escribir después son dos operaciones, y entre ellas cabe la decisión de
    otra pestaña.
    """
    fila = conn.execute(
        "SELECT id, title, status, script_json FROM videos WHERE id = ?",
        (video_id,),
    ).fetchone()
    _assert_en_borrador(fila, video_id)
    return fila


def _assert_en_borrador(fila: sqlite3.Row | None, video_id: int) -> None:
    """Corta si el video no existe o si su guion ya no admite decisión."""
    if fila is None:
        raise ScriptNotFound(f"no existe el video {video_id}")
    if fila["status"] != SCRIPT_DRAFT_STATUS:
        raise ScriptLocked(
            f"el guion del video {video_id} está en estado {fila['status']!r}"
            " y ya no espera decisión"
        )


def _write_script(
    conn: sqlite3.Connection, video_id: int, guion: dict[str, Any], status: str
) -> None:
    """Escribe el guion y el estado del video. Siempre dentro de una transacción.

    `videos.chapters` se rehace con el guion: es la lista de marcadores que va a
    la descripción de YouTube, y si el humano cambió el texto las marcas viejas
    apuntan a segundos que ya no existen.
    """
    conn.execute(
        "UPDATE videos SET script_json = ?, chapters = ?, status = ?,"
        " updated_at = datetime('now') WHERE id = ?",
        (
            json.dumps(guion, ensure_ascii=False),
            pacing.chapter_list(_stored_chapters(guion)),
            status,
            video_id,
        ),
    )


def _with_edit(guion: dict[str, Any], edit: ScriptEdit) -> dict[str, Any]:
    """El guion con el texto del humano, y los recuentos y las marcas rehechos.

    Si no cambió ni una coma se devuelve el guion tal cual: marcarlo como editado
    porque alguien pulsó "Aprobar" sin tocar nada sería mentir justo en el dato
    con el que el hito 5 va a medir cuánto hay que corregirle al modelo.

    Lo que escribió el modelo se conserva en `original`, y solo se guarda la
    primera vez: en la segunda edición el original ya está dentro y pisarlo
    dejaría como "lo que escribió el modelo" lo que escribió el humano.
    """
    _assert_completo(edit)
    if not _cambia_el_texto(guion, edit):
        return guion

    capitulos = [
        {
            "title": capitulo.title,
            "narration": capitulo.narration,
            "words": pacing.count_words(capitulo.narration),
        }
        for capitulo in edit.chapters
    ]
    palabras = (
        pacing.count_words(edit.hook)
        + pacing.count_words(edit.outro)
        + sum(int(capitulo["words"]) for capitulo in capitulos)
    )
    return {
        **guion,
        "hook": edit.hook,
        "chapters": pacing.with_start_times(edit.hook, capitulos),
        "outro": edit.outro,
        "word_count": palabras,
        "words_per_minute": pacing.WORDS_PER_MINUTE,
        "estimated_seconds": pacing.speech_seconds(palabras),
        "edited_by_human": True,
        "edited_at": _ahora_rfc3339(),
        "original": guion.get("original") or guion,
    }


def _assert_completo(edit: ScriptEdit) -> None:
    """Corta si la edición deja un hueco en el guion.

    No se comprueban las bandas de palabras que el writer le exige al modelo:
    cuánto dura su video lo decide el humano. Lo único que no puede quedar es un
    hueco, porque el hito 3 locuta este texto tal cual.
    """
    if not edit.hook:
        raise ScriptInvalid("el gancho no puede quedar vacío")
    if not edit.outro:
        raise ScriptInvalid("el cierre no puede quedar vacío")
    if not edit.chapters:
        raise ScriptInvalid("un guion sin capítulos no se puede producir")
    for numero, capitulo in enumerate(edit.chapters, start=1):
        if not capitulo.title:
            raise ScriptInvalid(f"el capítulo {numero} se quedó sin título")
        if not capitulo.narration:
            raise ScriptInvalid(f"el capítulo {numero} se quedó sin narración")


def _cambia_el_texto(guion: dict[str, Any], edit: ScriptEdit) -> bool:
    """True si la edición trae algo distinto de lo que ya estaba guardado."""
    if edit.hook != guion.get("hook") or edit.outro != guion.get("outro"):
        return True
    guardados = _stored_chapters(guion)
    if len(guardados) != len(edit.chapters):
        return True
    for guardado, editado in zip(guardados, edit.chapters):
        if guardado.get("title") != editado.title:
            return True
        if guardado.get("narration") != editado.narration:
            return True
    return False


def _to_script_summary(fila: sqlite3.Row) -> ScriptSummary:
    """Fila de `videos` → línea de la lista de guiones."""
    guion = _load_script(fila["script_json"], int(fila["id"]))
    return ScriptSummary(
        video_id=int(fila["id"]),
        title=str(fila["title"] or ""),
        niche=fila["niche"],
        format=fila["format"],
        chapter_count=len(_stored_chapters(guion)),
        word_count=_as_int(guion.get("word_count")),
        estimated_minutes=_as_minutes(guion.get("estimated_seconds")),
        edited=bool(guion.get("edited_by_human")),
        updated_at=str(fila["updated_at"] or ""),
    )


def _to_script_detail(fila: sqlite3.Row) -> ScriptDetail:
    """Fila de `videos` → guion completo para leer y editar."""
    guion = _load_script(fila["script_json"], int(fila["id"]))
    return ScriptDetail(
        video_id=int(fila["id"]),
        title=str(fila["title"] or ""),
        niche=fila["niche"],
        format=fila["format"],
        model=_as_text(guion.get("model")),
        generated_at=_as_text(guion.get("generated_at")),
        edited=bool(guion.get("edited_by_human")),
        hook=_as_text(guion.get("hook")) or "",
        chapters=_chapter_views(guion),
        outro=_as_text(guion.get("outro")) or "",
        word_count=_as_int(guion.get("word_count")),
        estimated_minutes=_as_minutes(guion.get("estimated_seconds")),
        updated_at=str(fila["updated_at"] or ""),
    )


def _chapter_views(guion: dict[str, Any]) -> list[ScriptChapterView]:
    """Los capítulos guardados, numerados y con su marca en MM:SS."""
    vistas: list[ScriptChapterView] = []
    for numero, capitulo in enumerate(_stored_chapters(guion), start=1):
        vistas.append(
            ScriptChapterView(
                number=numero,
                title=_as_text(capitulo.get("title")) or "",
                narration=_as_text(capitulo.get("narration")) or "",
                words=_as_int(capitulo.get("words")) or 0,
                start_mmss=pacing.mmss(_as_int(capitulo.get("start_sec")) or 0),
            )
        )
    return vistas


def _stored_chapters(guion: dict[str, Any]) -> list[dict[str, Any]]:
    """Los capítulos del JSON, sin lo que no sea un objeto."""
    capitulos = guion.get("chapters")
    if not isinstance(capitulos, list):
        return []
    return [capitulo for capitulo in capitulos if isinstance(capitulo, dict)]


def _script_json(fila: sqlite3.Row) -> dict[str, Any]:
    """`script_json` de la fila ya decodificado."""
    return _load_script(fila["script_json"], int(fila["id"]))


def _load_script(raw: str | None, video_id: int) -> dict[str, Any]:
    """`script_json` decodificado. Un JSON roto no puede tumbar el checkpoint.

    Con {} el editor sale sin capítulos y guardar da un error explícito, que es
    mucho mejor que una traza de 500 delante del único humano que puede
    desatascar el video.
    """
    if not raw:
        return {}
    try:
        guion = json.loads(raw)
    except ValueError:
        logger.warning("script_json ilegible en el video %d", video_id)
        return {}
    return guion if isinstance(guion, dict) else {}


def _as_text(value: Any) -> str | None:
    """Campo de texto del JSON, o None si no lo es."""
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    """Número del JSON a int, tolerando None y basura."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_minutes(segundos: Any) -> float | None:
    """Segundos estimados a minutos con un decimal, para el resumen."""
    valor = _as_int(segundos)
    return round(valor / 60, 1) if valor is not None else None


def _ahora_rfc3339() -> str:
    """Instante actual en UTC, con el mismo formato que escribe el writer."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def retry_job(conn: sqlite3.Connection, job_id: int) -> int:
    """Reencola un job fallido con el mismo tipo y payload. Devuelve el id nuevo.

    No revive el job viejo: encola uno nuevo y deja el fallido tal cual, como
    rastro de qué pasó. El dedupe compara tipo y payload contra lo que ya está
    pendiente o en ejecución, para que un doble clic no encole dos veces algo
    tan caro como una generación de guion.
    """
    with transaction(conn):
        fila = conn.execute(
            "SELECT type, status, payload, max_attempts FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if fila is None:
            raise JobNotFound(f"no existe el job {job_id}")
        if fila["status"] != "failed":
            raise JobNotRetryable(
                f"el job {job_id} está en estado {fila['status']!r}:"
                " solo se reintenta un job 'failed'"
            )
        payload = json.loads(fila["payload"] or "{}")
        if _ya_hay_en_curso(conn, fila["type"], payload):
            raise JobNotRetryable(
                f"ya hay un job de tipo {fila['type']!r} en curso con el mismo payload"
            )
        nuevo_id = queue.enqueue(
            fila["type"], payload, max_attempts=fila["max_attempts"], conn=conn
        )
    logger.info("Job %d reintentado -> nuevo job %d", job_id, nuevo_id)
    return nuevo_id


def _ya_hay_en_curso(
    conn: sqlite3.Connection, job_type: str, payload: dict[str, Any]
) -> bool:
    """True si ya hay un job del mismo tipo y payload pendiente o en ejecución."""
    return any(
        pendiente.payload == payload
        for pendiente in queue.unfinished_by_type(job_type, conn=conn)
    )


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
