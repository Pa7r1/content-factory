"""Google News RSS por consulta: momentum del tema + candidatos "noticias".

Se descarga el feed con requests (timeout y reintentos controlados aquí, no
en feedparser) y se parsea el contenido ya en memoria.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

from factory.research.http_util import SourceUnavailable, get_with_retries

logger = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search?q={query}&hl=es-419&gl=US&ceid=US:es-419"
USER_AGENT = "content-factory-research/0.1 (proyecto personal)"


def headlines(query: str, max_items: int = 30) -> list[dict[str, Any]]:
    """Titulares recientes del tema, más nuevos primero.

    Cada titular: {title, link, published (datetime UTC o None)}.
    Lanza `SourceUnavailable` si el feed no responde.
    """
    url = RSS_URL.format(query=urllib.parse.quote(query))
    response = get_with_retries(url, headers={"User-Agent": USER_AGENT})
    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        raise SourceUnavailable(f"Google News: feed ilegible para {query!r} ({feed.bozo_exception})")

    items: list[dict[str, Any]] = []
    for entry in feed.entries:
        items.append(
            {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": _entry_datetime(entry),
            }
        )
    # Ordenar ANTES de recortar: con un feed desordenado, recortar primero
    # devolvería los primeros del XML y el momentum mediría titulares viejos.
    items.sort(key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return items[:max_items]


def momentum_signal(query: str, max_items: int = 30) -> dict[str, Any] | None:
    """Señal de momentum del tema: cuántos titulares hay y cómo de recientes.

    Devuelve {last_7d, last_30d, headlines} o None si no hay ningún titular
    (tema sin cobertura: señal ausente, no fuente caída).
    """
    items = headlines(query, max_items=max_items)
    if not items:
        return None
    return {
        "last_7d": _count_newer_than(items, days=7),
        "last_30d": _count_newer_than(items, days=30),
        "headlines": [item["title"] for item in items],
    }


def _count_newer_than(items: list[dict[str, Any]], days: int) -> int:
    """Cuántos titulares se publicaron en los últimos `days` días."""
    limite = datetime.now(timezone.utc) - timedelta(days=days)
    return sum(1 for item in items if item["published"] and item["published"] >= limite)


def _entry_datetime(entry: Any) -> datetime | None:
    """published_parsed de feedparser → datetime UTC, si existe."""
    parsed = entry.get("published_parsed")
    if parsed is None:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)
