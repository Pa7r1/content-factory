"""Cliente REST de YouTube Data API v3 (sin SDK: es un GET con `key=`).

Cada llamada reserva su coste en `api_usage` ANTES de dispararse (patrón de
core/quota.py). Costes: search.list = 100 unidades; videos.list,
channels.list y playlistItems.list = 1 unidad por petición/página.

Sin `YOUTUBE_API_KEY` en el entorno se lanza `SourceUnavailable`: el
orquestador lo trata como señal ausente, no como error del pipeline.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from factory.core import config, quota
from factory.core.http_util import SourceUnavailable, get_json

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"

COST_SEARCH = 100
COST_LIST = 1

_DURATION_RE = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise SourceUnavailable("YOUTUBE_API_KEY no está en el entorno (.env)")
    return key


def _call(endpoint: str, params: dict[str, Any], *, cost: int, detail: str) -> dict[str, Any]:
    """Reserva cuota, llama al endpoint y reconcilia la reserva.

    Si la llamada falla, la reserva queda como 'reserved' (contar de más es
    seguro; contar de menos es pasarse del límite sin saberlo).
    """
    key = _api_key()  # antes de reservar: sin clave no hay que gastar cuota
    reservation_id = quota.reserve(
        "youtube", cost, config.daily_budget("youtube"), detail=detail
    )
    data = get_json(f"{API_BASE}/{endpoint}", params={**params, "key": key})
    quota.reconcile(reservation_id)
    return data


def search_videos(
    keyword: str,
    published_after: str,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    """Top videos por views para una keyword (search.list, 100 unidades).

    `published_after` en formato RFC 3339 (p. ej. "2024-08-06T00:00:00Z").
    Devuelve dicts con video_id, title, channel_id, channel_title, published_at.
    """
    data = _call(
        "search",
        {
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "order": "viewCount",
            "relevanceLanguage": "es",
            "publishedAfter": published_after,
            "maxResults": max_results,
        },
        cost=COST_SEARCH,
        detail=f"search.list q={keyword!r}",
    )
    results = []
    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        results.append(
            {
                "video_id": item.get("id", {}).get("videoId"),
                "title": snippet.get("title", ""),
                "channel_id": snippet.get("channelId"),
                "channel_title": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt"),
            }
        )
    return [r for r in results if r["video_id"]]


def videos_details(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Estadísticas y duración por video (videos.list, 1 unidad por lote de 50)."""
    details: dict[str, dict[str, Any]] = {}
    for batch in _batches(video_ids, 50):
        data = _call(
            "videos",
            # Sin maxResults: videos.list solo lo admite con `chart`/`myRating`,
            # y filtrando por `id` devuelve exactamente los ids pedidos.
            {"part": "statistics,contentDetails,snippet", "id": ",".join(batch)},
            cost=COST_LIST,
            detail=f"videos.list x{len(batch)}",
        )
        for item in data.get("items", []):
            stats = item.get("statistics", {})
            details[item["id"]] = {
                "title": item.get("snippet", {}).get("title", ""),
                "channel_id": item.get("snippet", {}).get("channelId"),
                "published_at": item.get("snippet", {}).get("publishedAt"),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0) or 0),
                "duration_sec": parse_iso8601_duration(
                    item.get("contentDetails", {}).get("duration", "")
                ),
            }
    return details


def channels_details(channel_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Estadísticas de canal (channels.list, 1 unidad por lote de 50).

    Incluye `uploads_playlist`, necesario para `channel_uploads()`.
    """
    details: dict[str, dict[str, Any]] = {}
    unique_ids = list(dict.fromkeys(channel_ids))
    for batch in _batches(unique_ids, 50):
        data = _call(
            "channels",
            {"part": "statistics,contentDetails,snippet", "id": ",".join(batch)},
            cost=COST_LIST,
            detail=f"channels.list x{len(batch)}",
        )
        for item in data.get("items", []):
            stats = item.get("statistics", {})
            uploads = (
                item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
            )
            details[item["id"]] = {
                "name": item.get("snippet", {}).get("title", ""),
                "subscribers": int(stats.get("subscriberCount", 0) or 0),
                "video_count": int(stats.get("videoCount", 0) or 0),
                "uploads_playlist": uploads,
            }
    return details


def playlist_video_ids(playlist_id: str, max_videos: int = 50) -> list[str]:
    """Ids de los últimos videos de una playlist (la de uploads de un canal).

    playlistItems.list cuesta 1 unidad por página de 50 — NUNCA search.list
    (100 unidades) para esto. El llamador pasa el `uploads_playlist` que ya
    obtuvo de `channels_details()`, para no pagar dos veces por lo mismo.
    """
    video_ids: list[str] = []
    page_token: str | None = None
    while len(video_ids) < max_videos:
        params: dict[str, Any] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": min(50, max_videos - len(video_ids)),
        }
        if page_token:
            params["pageToken"] = page_token
        data = _call(
            "playlistItems", params, cost=COST_LIST,
            detail=f"playlistItems.list {playlist_id}",
        )
        for item in data.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid:
                video_ids.append(vid)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return video_ids[:max_videos]


def parse_iso8601_duration(raw: str) -> int:
    """'P1DT2H3M4S' → segundos. Devuelve 0 si no se puede interpretar."""
    match = _DURATION_RE.fullmatch(raw or "")
    if match is None:
        return 0
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _batches(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
