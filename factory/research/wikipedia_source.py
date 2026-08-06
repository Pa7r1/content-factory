"""Wikipedia Pageviews (REST oficial, sin clave): demanda estable y evergreen.

Vistas mensuales de un artículo de es.wikipedia en los últimos 12 meses.
Poca varianza mes a mes = tema evergreen. El título del artículo se resuelve
con la API opensearch de es.wikipedia.
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from factory.research.http_util import SourceUnavailable, get_json

logger = logging.getLogger(__name__)

USER_AGENT = "content-factory-research/0.1 (proyecto personal; contacto en el repo)"
HEADERS = {"User-Agent": USER_AGENT}

SEARCH_API = "https://es.wikipedia.org/w/api.php"
PAGEVIEWS_API = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "es.wikipedia/all-access/user/{title}/monthly/{start}/{end}"
)


def find_article(query: str) -> str | None:
    """Título del artículo de es.wikipedia que mejor casa con la consulta.

    None si no hay resultados (tema sin artículo: señal ausente, no error).
    """
    data = get_json(
        SEARCH_API,
        params={
            "action": "opensearch",
            "search": query,
            "limit": 1,
            "namespace": 0,
            "format": "json",
        },
        headers=HEADERS,
    )
    # opensearch devuelve [consulta, [títulos], [descripciones], [urls]]
    titles = data[1] if isinstance(data, list) and len(data) > 1 else []
    return titles[0] if titles else None


def monthly_pageviews(title: str, months: int = 12) -> list[int]:
    """Vistas mensuales (usuarios reales, no bots) de los últimos `months` meses.

    Lista vacía si el artículo no tiene serie de pageviews: la API responde 404
    y eso es "sin datos", no "la fuente cayó".
    """
    now = datetime.now(timezone.utc)
    url = PAGEVIEWS_API.format(
        title=urllib.parse.quote(title.replace(" ", "_"), safe=""),
        start=_first_day_months_ago(now, months),
        end=now.strftime("%Y%m%d"),
    )
    try:
        data = get_json(url, headers=HEADERS)
    except SourceUnavailable as exc:
        if exc.status == 404:
            logger.info("Wikipedia: sin pageviews para %r", title)
            return []
        raise
    mes_actual = now.strftime("%Y%m")
    return [
        int(item.get("views", 0))
        for item in data.get("items", [])
        # El mes en curso va incompleto y dispararía la varianza: fuera.
        if str(item.get("timestamp", ""))[:6] != mes_actual
    ]


def _first_day_months_ago(now: datetime, months: int) -> str:
    """Día 1 del mes que queda exactamente `months` meses antes de `now`, 'YYYYMMDD'.

    Con 12 meses desde agosto de 2026 la ventana arranca el 20250801: descartado
    el mes en curso (incompleto), quedan los 12 meses completos que pide la firma.
    Restar `months * 31` días arrancaba un mes antes y devolvía 13.
    """
    total = (now.year * 12 + now.month - 1) - months
    return f"{total // 12:04d}{total % 12 + 1:02d}01"


def demand_signal(keyword: str) -> dict[str, Any] | None:
    """Señal completa para una keyword: artículo + vistas mensuales.

    None si no hay artículo que corresponda. Lanza `SourceUnavailable` si la
    API está caída.
    """
    article = find_article(keyword)
    if article is None:
        logger.info("Wikipedia: sin artículo para %r", keyword)
        return None
    views = monthly_pageviews(article)
    if not views:
        return None
    return {"article": article, "monthly_views": views}
