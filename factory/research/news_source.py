"""Google News RSS por consulta: momentum del tema + candidatos "noticias".

Se descarga el feed con requests (timeout y reintentos controlados aquí, no
en feedparser) y se parsea el contenido ya en memoria.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

from factory.core.http_util import SourceUnavailable, get_with_retries
from factory.core.text import normalize, words

logger = logging.getLogger(__name__)

RSS_URL = "https://news.google.com/rss/search?q={query}&hl=es-419&gl=US&ceid=US:es-419"
USER_AGENT = "content-factory-research/0.1 (proyecto personal)"

# Conectores que no dicen de qué va el tema. Exigirlos en el titular dejaba
# fuera a "Cinco claves de productividad para tu vida personal" por buscar
# "productividad personal": la frase literal casi nunca aparece entera.
CONNECTORS = frozenset(
    """a al ante con contra de del desde e el en entre hacia hasta la las lo los
    mas o para por que se segun sin sobre su sus tras tu tus un una unos unas
    y""".split()
)
# Longitud mínima de la raíz al quitar la marca de plural. Sin ella, "dios" se
# quedaría en "dio" y casaría con cualquier titular que hable de dar algo.
MIN_STEM_CHARS = 4


def headlines(
    query: str, max_items: int = 30, *, only_on_topic: bool = True
) -> list[dict[str, Any]]:
    """Titulares recientes del tema, más nuevos primero.

    Cada titular: {title, link, published (datetime UTC o None)}.
    Lanza `SourceUnavailable` si el feed no responde.

    Con `only_on_topic` (por defecto) se descartan los titulares que no
    contienen todas las palabras significativas de la keyword: Google News
    responde a "disciplina" con taekwondo, un tribunal judicial y una marca de
    ropa, y ese ruido acaba convertido en ideas de video.

    Una lista vacía significa "el feed respondió y no hay noticias del tema":
    señal ausente, que se propaga como None desde `momentum_signal`. Nunca un 0.
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
    if only_on_topic:
        items = _only_on_topic(items, query)
    # Ordenar ANTES de recortar: con un feed desordenado, recortar primero
    # devolvería los primeros del XML y el momentum mediría titulares viejos.
    items.sort(key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return items[:max_items]


def momentum_signal(
    query: str, max_items: int = 30, *, only_on_topic: bool = True
) -> dict[str, Any] | None:
    """Señal de momentum del tema: cuántos titulares hay y cómo de recientes.

    Devuelve {last_7d, last_30d, headlines} o None si no queda ningún titular
    del tema (sin cobertura: señal ausente, no fuente caída). El conteo se hace
    sobre los titulares ya filtrados, para que el momentum mida este tema y no
    la actividad general del feed.

    Los dos conteos valen **None si ningún titular trae fecha**: sin fechas no
    hay con qué medir la recencia y un 0 sería un dato inventado que hunde el
    score. Con fechas, un 0 sí es un dato: hay cobertura del tema pero no de
    esta semana, o sea un tema frío, y eso se puntúa como lo que es.
    """
    items = headlines(query, max_items=max_items, only_on_topic=only_on_topic)
    if not items:
        return None
    hay_fechas = any(item["published"] for item in items)
    return {
        "last_7d": _count_newer_than(items, days=7) if hay_fechas else None,
        "last_30d": _count_newer_than(items, days=30) if hay_fechas else None,
        "headlines": [item["title"] for item in items],
    }


def _only_on_topic(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Titulares que hablan del tema de la keyword.

    Devolver [] es un resultado legítimo: el feed respondió y ninguna noticia
    habla del tema.
    """
    patrones = _topic_patterns(query)
    if not patrones:
        return items

    del_tema = [item for item in items if _tiene_todas(patrones, item["title"])]
    logger.debug(
        "Google News %r: %d titulares -> %d del tema", query, len(items), len(del_tema)
    )
    return del_tema


def _tiene_todas(patrones: list[re.Pattern[str]], titulo: str) -> bool:
    """¿El titular contiene todas las palabras significativas de la keyword?"""
    normalizado = normalize(titulo)
    return all(patron.search(normalizado) for patron in patrones)


def _topic_patterns(query: str) -> list[re.Pattern[str]]:
    """Un patrón por palabra significativa de la keyword.

    Se exigen todas, pero no seguidas. Pedir la frase literal y contigua dejaba
    sin señal de momentum, de forma permanente, a 14 de las 19 keywords
    configuradas: un titular real dice "Traicion, venganza y poder en la nueva
    serie" y nunca "traición y venganza".

    Los conectores no cuentan; si la keyword es solo conectores se usan todas
    sus palabras, y si se queda vacía al normalizar no hay nada que filtrar.
    """
    palabras = words(query)
    significativas = [p for p in palabras if p not in CONNECTORS] or palabras
    return [_word_pattern(palabra) for palabra in significativas]


def _word_pattern(palabra: str) -> re.Pattern[str]:
    """La palabra suelta, en singular o en plural, sin casar dentro de otra.

    Los lookarounds evitan que "arte" case dentro de "cuarteto"; el sufijo
    opcional hace que "hábitos" case con "hábito" y "reflexiones" con
    "reflexion", que en un titular son la misma palabra.
    """
    return re.compile(rf"(?<!\w){re.escape(_singular(palabra))}(?:e?s)?(?!\w)")


def _singular(palabra: str) -> str:
    """Quita la marca de plural, salvo si lo que queda ya no es una palabra."""
    for sufijo in ("es", "s"):
        raiz = palabra.removesuffix(sufijo)
        if raiz != palabra and len(raiz) >= MIN_STEM_CHARS:
            return raiz
    return palabra


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
