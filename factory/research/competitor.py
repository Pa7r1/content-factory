"""Análisis de un canal competidor: ritmo, formato y videos que se salen de la media.

La métrica que importa es el **outlier ratio** (vistas del video / media del
canal): un video con ratio 5 dice que ese tema funcionó cinco veces mejor que
lo normal en ese canal, y eso es una idea, no una casualidad.

Coste en cuota: 3 unidades por canal (channels.list + playlistItems.list +
videos.list). Los uploads se leen SIEMPRE por la playlist de uploads; usar
search.list para esto costaría 100 unidades por canal.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from statistics import fmean
from typing import Any

from factory.core.db import get_conn, transaction
from factory.core.http_util import SourceUnavailable
from factory.research import youtube_source

logger = logging.getLogger(__name__)

MAX_UPLOADS = 50


def analyze_channel(
    channel_id: str,
    *,
    niche: str | None = None,
    max_videos: int = MAX_UPLOADS,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Analiza un canal y persiste el resultado en `competitors`/`competitor_videos`.

    Devuelve el resumen del canal (nombre, subs, frecuencia semanal, duración
    media, media de vistas y cuántos videos se guardaron). Lanza
    `SourceUnavailable` si YouTube no responde o no hay clave.
    """
    channel = _fetch_channel(channel_id)
    videos = _fetch_recent_videos(channel["uploads_playlist"], max_videos)
    if not videos:
        raise SourceUnavailable(f"canal {channel_id}: la playlist de uploads vino vacía")

    stats = summarize_uploads(videos)
    conn = conn or get_conn()
    competitor_id = _save(conn, channel_id, channel, niche, stats)

    resumen = {
        "competitor_id": competitor_id,
        "channel_id": channel_id,
        "name": channel["name"],
        "subscribers": channel["subscribers"],
        "videos_analyzed": len(stats["videos"]),
        "avg_views": stats["avg_views"],
        "upload_frequency": stats["upload_frequency"],
        "avg_duration_sec": stats["avg_duration_sec"],
    }
    logger.info(
        "Competidor %s (%s): %d videos, %.1f/semana, media %.0f vistas",
        channel["name"], channel_id, len(stats["videos"]),
        stats["upload_frequency"], stats["avg_views"],
    )
    return resumen


def summarize_uploads(videos: list[dict[str, Any]]) -> dict[str, Any]:
    """Métricas derivadas de una lista de uploads. Lógica pura: sin red ni DB.

    Cada video de entrada trae views, duration_sec, published_at y title. Cada
    video de salida añade `views_per_day` y `outlier_ratio`.
    """
    ahora = datetime.now(timezone.utc)
    vistas = [v["views"] for v in videos]
    media_vistas = fmean(vistas) if vistas else 0.0

    enriquecidos = []
    for video in videos:
        edad_dias = _age_days(video["published_at"], ahora)
        enriquecidos.append(
            {
                **video,
                "views_per_day": video["views"] / max(edad_dias, 1.0),
                "outlier_ratio": video["views"] / media_vistas if media_vistas > 0 else 0.0,
            }
        )

    return {
        "videos": enriquecidos,
        "avg_views": media_vistas,
        "avg_duration_sec": fmean([v["duration_sec"] for v in videos]) if videos else 0.0,
        "upload_frequency": _uploads_per_week(videos, ahora),
    }


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------


def _fetch_channel(channel_id: str) -> dict[str, Any]:
    """Datos del canal, con su playlist de uploads (imprescindible)."""
    channel = youtube_source.channels_details([channel_id]).get(channel_id)
    if channel is None:
        raise SourceUnavailable(f"canal {channel_id}: YouTube no lo devolvió")
    if not channel.get("uploads_playlist"):
        raise SourceUnavailable(f"canal {channel_id}: sin playlist de uploads")
    return channel


def _fetch_recent_videos(uploads_playlist: str, max_videos: int) -> list[dict[str, Any]]:
    """Últimos uploads con sus estadísticas, más recientes primero."""
    video_ids = youtube_source.playlist_video_ids(uploads_playlist, max_videos)
    if not video_ids:
        return []
    detalles = youtube_source.videos_details(video_ids)
    return [
        {"yt_video_id": vid, **detalles[vid]} for vid in video_ids if vid in detalles
    ]


# ---------------------------------------------------------------------------
# Cálculos
# ---------------------------------------------------------------------------


def _uploads_per_week(videos: list[dict[str, Any]], ahora: datetime) -> float:
    """Ritmo de publicación: videos por semana en la ventana analizada."""
    fechas = [_parse_rfc3339(v["published_at"]) for v in videos]
    fechas = [f for f in fechas if f is not None]
    if len(fechas) < 2:
        return 0.0
    dias = (max(fechas) - min(fechas)).total_seconds() / 86400
    if dias <= 0:
        return float(len(fechas))
    return len(fechas) / (dias / 7)


def _age_days(published_at: str | None, ahora: datetime) -> float:
    """Días desde la publicación (0 si la fecha no se puede leer)."""
    fecha = _parse_rfc3339(published_at)
    if fecha is None:
        return 0.0
    return max((ahora - fecha).total_seconds() / 86400, 0.0)


def _parse_rfc3339(raw: str | None) -> datetime | None:
    """'2026-08-06T10:00:00Z' → datetime UTC. None si no es una fecha legible."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Fecha ilegible de YouTube: %r", raw)
        return None


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


def _save(
    conn: sqlite3.Connection,
    channel_id: str,
    channel: dict[str, Any],
    niche: str | None,
    stats: dict[str, Any],
) -> int:
    """Guarda canal y videos en una sola transacción inmediata."""
    with transaction(conn):
        row = conn.execute(
            """
            INSERT INTO competitors
                   (channel_id, name, niche, subscribers, video_count,
                    avg_views, upload_frequency, last_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (channel_id) DO UPDATE SET
                   name             = excluded.name,
                   niche            = COALESCE(excluded.niche, competitors.niche),
                   subscribers      = excluded.subscribers,
                   video_count      = excluded.video_count,
                   avg_views        = excluded.avg_views,
                   upload_frequency = excluded.upload_frequency,
                   last_checked_at  = datetime('now')
            RETURNING id
            """,
            (
                channel_id,
                channel["name"],
                niche,
                channel["subscribers"],
                channel["video_count"],
                stats["avg_views"],
                stats["upload_frequency"],
            ),
        ).fetchone()
        competitor_id = int(row["id"])

        for video in stats["videos"]:
            conn.execute(
                """
                INSERT INTO competitor_videos
                       (competitor_id, yt_video_id, title, published_at,
                        views, duration_sec, views_per_day, outlier_ratio)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (yt_video_id) DO UPDATE SET
                       views         = excluded.views,
                       views_per_day = excluded.views_per_day,
                       outlier_ratio = excluded.outlier_ratio
                """,
                (
                    competitor_id,
                    video["yt_video_id"],
                    video["title"],
                    video["published_at"],
                    video["views"],
                    video["duration_sec"],
                    video["views_per_day"],
                    video["outlier_ratio"],
                ),
            )
    return competitor_id
