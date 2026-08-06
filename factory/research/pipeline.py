"""Orquestador del job `research_daily`: de keywords semilla a ideas puntuadas.

Cómo funciona una pasada de un nicho:

1. Se recogen una vez por nicho los datos que valen para todas sus keywords
   (posts de Reddit, CPM del nicho).
2. Por cada keyword semilla se consultan las cuatro fuentes. **Cada fuente va en
   su propio try/except**: si una cae, la señal queda ausente con su motivo
   escrito en `ideas.score_details` y el scorer re-normaliza pesos.
3. Con las señales se calcula el score y se extraen **temas concretos**: las
   keywords son sondas, las ideas son títulos. Los temas salen de los videos de
   YouTube con mayor outlier ratio y de los titulares de Google News.
4. Cada idea se hace upsert en `ideas` deduplicando por nicho+keyword+título en
   estado 'new': una segunda pasada actualiza la fila, no la duplica.

Este módulo no importa de ningún otro módulo de dominio: entra y sale por la
base de datos (patrón blackboard).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any, Callable

from factory.core import config
from factory.core.db import get_conn, transaction
from factory.core.models import Idea, Job
from factory.core.queue import JobWorker, should_stop
from factory.core.quota import QuotaExceeded
from factory.research import (
    news_source,
    reddit_source,
    scorer,
    wikipedia_source,
    youtube_source,
)
from factory.research.http_util import SourceUnavailable

logger = logging.getLogger(__name__)

JOB_TYPE = "research_daily"

TOP_VIDEOS = 10
# Ventana de búsqueda en YouTube: 3 años. Suficiente para que la antigüedad
# mediana del top discrimine (el techo del scorer son 2 años) sin arrastrar
# éxitos de hace una década que ya no describen la competencia de hoy.
YOUTUBE_WINDOW_DAYS = 1095
YOUTUBE_TOPICS_PER_KEYWORD = 2
NEWS_TOPICS_PER_KEYWORD = 1
MAX_TITLE_CHARS = 200
# Pausa entre keywords: verificado en vivo que sondear ~19 keywords seguidas
# hace que la API de es.wikipedia empiece a responder 429. Un segundo de espera
# cuesta 19 segundos por pasada diaria y nos devuelve la señal evergreen.
PAUSE_BETWEEN_KEYWORDS = 1.0
# Estados desde los que una idea NO debe volver a proponerse cada mañana.
TERMINAL_STATUSES = ("rejected", "used")


@dataclass(frozen=True, slots=True)
class NicheContext:
    """Lo que se recoge una vez por nicho y sirve para todas sus keywords."""

    niche_id: str
    cpm_usd: float | None
    reddit_posts: list[dict[str, Any]] | None
    reddit_reason: str | None  # por qué no hay posts, si no los hay


@dataclass(slots=True)
class KeywordResearch:
    """Resultado de sondear una keyword: señales, ausencias y temas concretos."""

    signals: scorer.Signals
    raw: dict[str, Any] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    topics: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Handler de la cola
# ---------------------------------------------------------------------------


def register(worker: JobWorker) -> None:
    """Registra el handler de `research_daily` en el worker de jobs."""
    worker.register(JOB_TYPE, handle_research_daily)


def handle_research_daily(job: Job) -> None:
    """Investiga el nicho del payload, o todos los nichos si no viene ninguno."""
    disponibles = config.niches()
    pedido = job.payload.get("niche")
    if pedido is not None and pedido not in disponibles:
        raise ValueError(f"nicho desconocido en el payload: {pedido!r}")

    objetivos = [pedido] if pedido else list(disponibles)
    for niche_id in objetivos:
        research_niche(niche_id)


def research_niche(niche_id: str) -> int:
    """Sondea todas las keywords semilla de un nicho. Devuelve ideas escritas.

    Una keyword que revienta no tumba el nicho; que revienten TODAS sí hace
    fallar el job, porque entonces el problema no es una fuente sino el sistema.

    Escribir cero ideas NO es un fallo por sí solo: a las pocas semanas es lo
    normal, porque el humano ya descartó esas ideas y `_upsert_idea` no las
    repropone. Lo que decide es cuántas keywords se pudieron sondear.
    """
    keywords: list[str] = config.niches()[niche_id].get("keywords", [])
    context = _niche_context(niche_id)

    escritas = 0
    sondeadas = 0
    fallos: list[str] = []
    for indice, keyword in enumerate(keywords):
        if should_stop():
            logger.warning(
                "Nicho %s: parada pedida, se dejan %d keywords sin sondear",
                niche_id, len(keywords) - indice,
            )
            break
        if indice > 0:
            time.sleep(PAUSE_BETWEEN_KEYWORDS)
        try:
            escritas += _research_keyword(context, keyword)
        except Exception as exc:  # una keyword rota no cancela el resto del nicho
            logger.exception("Keyword %r del nicho %s falló", keyword, niche_id)
            fallos.append(f"{keyword}: {type(exc).__name__}: {exc}")
        else:
            sondeadas += 1

    if fallos and sondeadas == 0:
        raise RuntimeError(
            f"nicho {niche_id}: no se pudo sondear ninguna keyword — {' | '.join(fallos)}"
        )
    logger.info(
        "Nicho %s: %d ideas escritas de %d keywords sondeadas (%d con fallo)",
        niche_id, escritas, sondeadas, len(fallos),
    )
    return escritas


# ---------------------------------------------------------------------------
# Recogida de señales
# ---------------------------------------------------------------------------


def _niche_context(niche_id: str) -> NicheContext:
    """Prepara los datos comunes del nicho (Reddit una sola vez, CPM)."""
    posts, motivo = _niche_reddit_posts(niche_id)
    return NicheContext(
        niche_id=niche_id,
        cpm_usd=config.cpm_for_niche(niche_id),
        reddit_posts=posts,
        reddit_reason=motivo,
    )


def _niche_reddit_posts(niche_id: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Posts de Reddit del nicho y, si no los hay, el motivo.

    Va por `_try_source` como las otras cuatro fuentes: aquí se capturaba solo
    `SourceUnavailable`, así que un campo inesperado en la respuesta (un `ups`
    nulo) tumbaba el nicho entero con sus tres reintentos.
    """
    subreddits = config.subreddits(niche_id)
    if not subreddits:
        return None, "el nicho no tiene subreddits configurados"

    reasons: dict[str, str] = {}
    posts = _try_source(
        "reddit", lambda: reddit_source.collect_top_posts(subreddits), reasons
    )
    return posts, reasons.get("reddit")


def collect_keyword_signals(context: NicheContext, keyword: str) -> KeywordResearch:
    """Consulta las cuatro fuentes para una keyword y arma señales + temas."""
    reasons: dict[str, str] = {}

    youtube = _try_source("youtube", lambda: _youtube_research(keyword), reasons)
    news = _try_source("news", lambda: news_source.momentum_signal(keyword), reasons)
    wiki = _try_source("wikipedia", lambda: wikipedia_source.demand_signal(keyword), reasons)
    reddit = _reddit_research(context, keyword, reasons)

    if context.cpm_usd is None:
        reasons["cpm"] = f"no hay CPM configurado para el nicho {context.niche_id}"

    return KeywordResearch(
        signals=_build_signals(youtube, news, wiki, reddit, context.cpm_usd),
        raw=_raw_snapshot(youtube, news, wiki, reddit, context.cpm_usd),
        reasons=reasons,
        topics=_topics(youtube, news),
    )


def _build_signals(
    youtube: dict[str, Any] | None,
    news: dict[str, Any] | None,
    wiki: dict[str, Any] | None,
    reddit: dict[str, Any] | None,
    cpm_usd: float | None,
) -> scorer.Signals:
    """Empaqueta lo recogido en la entrada que espera el scorer."""
    videos = youtube["videos"] if youtube else []
    subs = [v["subscribers"] for v in videos if v["subscribers"] is not None]
    edades = [v["age_days"] for v in videos if v["age_days"] is not None]
    return scorer.Signals(
        top_video_views=[v["views"] for v in videos] or None,
        top_channel_subs=subs or None,
        top_video_ages_days=edades or None,
        news_last_7d=news["last_7d"] if news else None,
        news_last_30d=news["last_30d"] if news else None,
        reddit_engagement=reddit["engagement"] if reddit else None,
        wikipedia_monthly_views=wiki["monthly_views"] if wiki else None,
        cpm_usd=cpm_usd,
    )


def _raw_snapshot(
    youtube: dict[str, Any] | None,
    news: dict[str, Any] | None,
    wiki: dict[str, Any] | None,
    reddit: dict[str, Any] | None,
    cpm_usd: float | None,
) -> dict[str, Any]:
    """Foto de las señales crudas que se guarda en `ideas.score_details`.

    Es lo que permite responder desde el dashboard "¿por qué esta idea tiene
    este número?" sin volver a llamar a ninguna API.
    """
    raw: dict[str, Any] = {"cpm_usd": cpm_usd}
    if youtube:
        videos = youtube["videos"]
        raw["youtube"] = {
            "videos_encontrados": len(videos),
            "vistas": [v["views"] for v in videos],
            "subs_canales": [v["subscribers"] for v in videos],
            "antiguedad_dias": [_redondear(v["age_days"]) for v in videos],
        }
    if news:
        raw["news"] = {
            "last_7d": news["last_7d"],
            "last_30d": news["last_30d"],
            "titulares": news["headlines"][:5],
        }
    if wiki:
        raw["wikipedia"] = {
            "articulo": wiki["article"],
            "vistas_mensuales": wiki["monthly_views"],
        }
    if reddit:
        raw["reddit"] = {
            "engagement": reddit["engagement"],
            "posts": reddit["matched_posts"][:5],
        }
    return raw


def _try_source(
    name: str, fetch: Callable[[], Any], reasons: dict[str, str]
) -> Any | None:
    """Consulta una fuente; si falla o no trae datos, apunta el motivo y sigue.

    Ninguna fuente puede tumbar la investigación: una señal ausente con su
    motivo registrado vale infinitamente más que un job que revienta a las 7:30
    y nadie ve hasta tres días después.
    """
    try:
        resultado = fetch()
    except (SourceUnavailable, QuotaExceeded) as exc:
        reasons[name] = f"{type(exc).__name__}: {exc}"
        logger.info("Señal %s ausente: %s", name, exc)
        return None
    except Exception as exc:  # bug o fallo no previsto de la fuente
        reasons[name] = f"error inesperado: {type(exc).__name__}: {exc}"
        logger.exception("La fuente %s falló de forma inesperada", name)
        return None

    if resultado is None:
        reasons[name] = "la fuente respondió pero no tiene datos de esta keyword"
    return resultado


def _reddit_research(
    context: NicheContext, keyword: str, reasons: dict[str, str]
) -> dict[str, Any] | None:
    """Engagement de la keyword en los posts ya descargados para el nicho."""
    if context.reddit_posts is None:
        reasons["reddit"] = context.reddit_reason or "sin posts de Reddit"
        return None
    return reddit_source.engagement_in_posts(context.reddit_posts, keyword)


def _youtube_research(keyword: str) -> dict[str, Any] | None:
    """Top de videos de la keyword con vistas, subs del canal y antigüedad.

    Coste: 102 unidades de cuota (search.list 100 + videos.list + channels.list).
    """
    published_after = _rfc3339(
        datetime.now(timezone.utc) - timedelta(days=YOUTUBE_WINDOW_DAYS)
    )
    resultados = youtube_source.search_videos(keyword, published_after, TOP_VIDEOS)
    if not resultados:
        return None

    detalles = youtube_source.videos_details([r["video_id"] for r in resultados])
    canales = youtube_source.channels_details(
        [r["channel_id"] for r in resultados if r["channel_id"]]
    )
    videos = _merge_youtube(resultados, detalles, canales)
    if not videos:
        return None

    media_vistas = fmean([v["views"] for v in videos])
    for video in videos:
        video["outlier_ratio"] = video["views"] / media_vistas if media_vistas > 0 else 0.0
    return {"videos": videos}


def _merge_youtube(
    resultados: list[dict[str, Any]],
    detalles: dict[str, dict[str, Any]],
    canales: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cruza búsqueda + estadísticas + canal en una fila por video."""
    ahora = datetime.now(timezone.utc)
    videos: list[dict[str, Any]] = []
    for resultado in resultados:
        detalle = detalles.get(resultado["video_id"])
        if detalle is None:
            continue
        canal = canales.get(resultado["channel_id"] or "", {})
        videos.append(
            {
                "video_id": resultado["video_id"],
                "title": detalle["title"] or resultado["title"],
                "views": detalle["views"],
                "duration_sec": detalle["duration_sec"],
                "channel_id": resultado["channel_id"],
                "channel_title": resultado["channel_title"],
                "subscribers": canal.get("subscribers"),
                "age_days": _age_days(detalle["published_at"], ahora),
            }
        )
    return videos


# ---------------------------------------------------------------------------
# De señales a ideas
# ---------------------------------------------------------------------------


def _topics(
    youtube: dict[str, Any] | None, news: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Temas concretos que merecen ser una idea, con su procedencia.

    De YouTube salen los títulos con mayor outlier ratio (vistas del video
    frente a la media del top: lo que se salió de lo esperable). De News, los
    titulares más recientes.
    """
    topics: list[dict[str, Any]] = []

    if youtube:
        mejores = sorted(
            youtube["videos"], key=lambda v: v["outlier_ratio"], reverse=True
        )
        for video in mejores[:YOUTUBE_TOPICS_PER_KEYWORD]:
            topics.append(
                {
                    "origin": "youtube",
                    "title": _clean_title(video["title"]),
                    "video_id": video["video_id"],
                    "channel_title": video["channel_title"],
                    "views": video["views"],
                    "outlier_ratio": round(video["outlier_ratio"], 2),
                    "suggested_format": None,
                }
            )

    if news:
        for headline in news["headlines"][:NEWS_TOPICS_PER_KEYWORD]:
            topics.append(
                {
                    "origin": "news",
                    "title": _clean_title(_strip_outlet(headline)),
                    "suggested_format": "noticias",
                }
            )

    return topics


def _research_keyword(context: NicheContext, keyword: str) -> int:
    """Sondea una keyword y escribe sus ideas. Devuelve cuántas se escribieron."""
    research = collect_keyword_signals(context, keyword)
    resultado = scorer.score_idea(research.signals, config.score_weights())
    ideas = _build_ideas(context.niche_id, keyword, research, resultado)

    conn = get_conn()
    escritas = 0
    for idea in ideas:
        if _upsert_idea(conn, idea) is not None:
            escritas += 1

    logger.info(
        "%s / %r -> score %.1f (faltan: %s) -> %d ideas",
        context.niche_id, keyword, resultado.score,
        ", ".join(resultado.missing) or "nada", escritas,
    )
    return escritas


def _build_ideas(
    niche_id: str,
    keyword: str,
    research: KeywordResearch,
    resultado: scorer.ScoreResult,
) -> list[Idea]:
    """Convierte los temas de una keyword en filas de `ideas` listas para grabar.

    Todos los temas de una keyword comparten su score: las señales miden el
    tema-sonda, no cada título. Lo que los diferencia (outlier ratio, origen)
    va en `score_details.topic`, y el dashboard ordena por score y desempata
    por ahí. Si no salió ningún tema concreto, se graba la keyword misma para
    no perder la medición.
    """
    base = {
        "signals": research.raw,
        "sub_metrics": resultado.sub_metrics,
        "components": resultado.components,
        "weights_used": resultado.weights_used,
        "missing_signals": resultado.missing,
        "missing_reasons": research.reasons,
        "collected_at": _rfc3339(datetime.now(timezone.utc)),
    }

    topics = research.topics or [
        {
            "origin": "keyword",
            "title": _clean_title(keyword),
            "suggested_format": None,
            "note": "ninguna fuente devolvió temas concretos; se guarda la sonda",
        }
    ]

    ideas: list[Idea] = []
    for topic in topics:
        ideas.append(
            Idea(
                title=topic["title"],
                niche=niche_id,
                keyword=keyword,
                source=topic["origin"],
                demand=resultado.components["demand"],
                competition=resultado.components["competition"],
                evergreen=resultado.components["evergreen"],
                cpm_factor=resultado.components["cpm"],
                score=resultado.score,
                score_details={**base, "topic": topic},
                suggested_format=topic.get("suggested_format"),
            )
        )
    return ideas


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


def _upsert_idea(conn: sqlite3.Connection, idea: Idea) -> int | None:
    """Inserta la idea, o actualiza la equivalente que siga en estado 'new'.

    Equivalente = mismo nicho, misma keyword y mismo título. Si esa idea ya fue
    descartada o usada, no se vuelve a crear: proponerla otra vez cada mañana
    sería ruido para el humano que ya decidió.

    Devuelve el id de la idea, o None si se omitió por estar en estado terminal.
    """
    detalles = json.dumps(idea.score_details, ensure_ascii=False, default=str)
    with transaction(conn):
        fila = conn.execute(
            """
            SELECT id, status FROM ideas
             WHERE niche = ? AND keyword IS ? AND title = ?
             ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, id DESC
             LIMIT 1
            """,
            (idea.niche, idea.keyword, idea.title),
        ).fetchone()

        if fila is not None and fila["status"] in TERMINAL_STATUSES:
            logger.debug("Idea %r ya está en estado %s: no se repropone", idea.title, fila["status"])
            return None

        if fila is not None and fila["status"] == "new":
            conn.execute(
                """
                UPDATE ideas
                   SET source = ?, demand = ?, competition = ?, evergreen = ?,
                       cpm_factor = ?, score = ?, score_details = ?,
                       suggested_format = ?, updated_at = datetime('now')
                 WHERE id = ?
                """,
                (
                    idea.source, idea.demand, idea.competition, idea.evergreen,
                    idea.cpm_factor, idea.score, detalles, idea.suggested_format,
                    fila["id"],
                ),
            )
            return int(fila["id"])

        if fila is not None:
            # Está shortlisted o approved: el humano ya la movió, no se toca.
            logger.debug("Idea %r en estado %s: no se modifica", idea.title, fila["status"])
            return None

        cursor = conn.execute(
            """
            INSERT INTO ideas (title, niche, keyword, source, demand, competition,
                               evergreen, cpm_factor, score, score_details,
                               status, suggested_format)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                idea.title, idea.niche, idea.keyword, idea.source, idea.demand,
                idea.competition, idea.evergreen, idea.cpm_factor, idea.score,
                detalles, idea.suggested_format,
            ),
        )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _strip_outlet(headline: str) -> str:
    """Quita el " - Medio" con el que Google News firma cada titular."""
    if " - " not in headline:
        return headline
    cuerpo, medio = headline.rsplit(" - ", 1)
    return cuerpo if len(medio) <= 40 else headline


def _redondear(valor: float | None) -> float | None:
    """Redondea a un decimal para el snapshot, conservando los None."""
    return None if valor is None else round(valor, 1)


def _clean_title(title: str) -> str:
    """Título normalizado: sin espacios sobrantes y acotado en longitud."""
    limpio = " ".join(title.split())
    return limpio[:MAX_TITLE_CHARS]


def _age_days(published_at: str | None, ahora: datetime) -> float | None:
    """Días desde la publicación, o None si la fecha no viene o no se puede leer.

    None, no 0.0: un 0.0 diría "recién subido", hundiría `age_norm` y contaría
    como saturación del top. Una fecha ilegible es una señal ausente.
    """
    if not published_at:
        return None
    try:
        fecha = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Fecha ilegible de YouTube: %r", published_at)
        return None
    return max((ahora - fecha).total_seconds() / 86400, 0.0)


def _rfc3339(momento: datetime) -> str:
    """datetime UTC → '2026-08-06T10:00:00Z', el formato que pide YouTube."""
    return momento.strftime("%Y-%m-%dT%H:%M:%SZ")
