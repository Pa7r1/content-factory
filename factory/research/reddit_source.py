"""Reddit vía JSON público: engagement (upvotes + comentarios) por keyword.

Sin app registrada todavía (aprobación pendiente), se usa el endpoint público
con User-Agent propio y pausas entre llamadas. Reddit bloquea IPs de
datacenter con 403: en ese caso la señal queda ausente y NO se reintenta en
bucle (max_attempts=1 en el GET).

El uso previsto es: `collect_top_posts()` una vez por nicho (red) y luego
`engagement_in_posts()` por cada keyword (memoria).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from factory.research.http_util import SourceUnavailable, get_json

logger = logging.getLogger(__name__)

USER_AGENT = "linux:content-factory-research:0.1 (proyecto personal)"
HEADERS = {"User-Agent": USER_AGENT}
TOP_URL = "https://www.reddit.com/r/{sub}/top.json?t=week&limit=25"

PAUSE_BETWEEN_CALLS = 1.5  # segundos entre subreddits, para no parecer ráfaga

# Palabras de la keyword demasiado genéricas para contar como coincidencia.
_STOPWORDS = frozenset({"de", "la", "el", "los", "las", "del", "y", "en", "con", "por"})


def top_posts(subreddit: str) -> list[dict[str, Any]]:
    """Top de la semana de un subreddit: {title, ups, num_comments}.

    Un solo intento: 403/429 aquí significa bloqueo, y reintentar es ruido.
    """
    data = get_json(
        TOP_URL.format(sub=subreddit), headers=HEADERS, max_attempts=1
    )
    posts = []
    for child in _children(data):
        post = child.get("data") or {}
        posts.append(
            {
                "title": _as_text(post.get("title")),
                "ups": _as_int(post.get("ups")),
                "num_comments": _as_int(post.get("num_comments")),
            }
        )
    return posts


def _children(data: Any) -> list[dict[str, Any]]:
    """Lista de posts de una respuesta de Reddit, tolerando cuerpos raros.

    Bajo rate-limit Reddit devuelve 200 con un cuerpo de error que no tiene esta
    forma: eso es ausencia de datos, no un TypeError en mitad del worker.
    """
    if not isinstance(data, dict):
        return []
    contenedor = data.get("data")
    if not isinstance(contenedor, dict):
        return []
    hijos = contenedor.get("children")
    return [child for child in hijos if isinstance(child, dict)] if isinstance(hijos, list) else []


def _as_text(value: Any) -> str:
    """Texto de un campo que Reddit puede servir como null."""
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    """Entero de un campo que Reddit puede servir como null o como texto."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def collect_top_posts(subreddits: list[str]) -> list[dict[str, Any]]:
    """Top de la semana de todos los subreddits de un nicho, en UNA pasada.

    Se llama una vez por nicho, no una por keyword: los mismos posts sirven
    para todas las keywords del nicho y Reddit no necesita ver 20 peticiones
    donde bastan 3. Lanza `SourceUnavailable` si no respondió ninguno, y también
    si la lista viene vacía: devolver [] haría que el engagement de cada keyword
    valiese 0 y el scorer penalizaría por falta de datos en vez de re-normalizar.
    """
    if not subreddits:
        raise SourceUnavailable("sin subreddits configurados para este nicho")

    todos: list[dict[str, Any]] = []
    motivos: list[str] = []

    for index, sub in enumerate(subreddits):
        if index > 0:
            time.sleep(PAUSE_BETWEEN_CALLS)
        try:
            todos.extend(top_posts(sub))
        except SourceUnavailable as exc:
            logger.info("Reddit r/%s sin señal: %s", sub, exc)
            motivos.append(f"r/{sub}: {exc}")

    if len(motivos) == len(subreddits):
        raise SourceUnavailable("ningún subreddit respondió — " + " | ".join(motivos))
    if not todos:
        # 200 con `children` vacío: el sub está vacío, o es el cuerpo de error
        # que Reddit sirve con 200 bajo rate-limit. En ambos casos no hay señal,
        # y devolver [] la convertiría en un engagement de 0 para cada keyword.
        raise SourceUnavailable(
            "los subreddits respondieron pero ninguno trajo posts: "
            + ", ".join(f"r/{sub}" for sub in subreddits)
        )
    return todos


def engagement_in_posts(posts: list[dict[str, Any]], keyword: str) -> dict[str, Any]:
    """Engagement de los posts que hablan de la keyword. Lógica pura, sin red.

    Devuelve {engagement, matched_posts}: la suma de upvotes y comentarios de
    los posts cuyo título contiene alguna palabra significativa de la keyword.
    """
    tokens = _significant_tokens(keyword)
    total = 0
    matched: list[str] = []
    for post in posts:
        title_lower = post["title"].lower()
        if any(token in title_lower for token in tokens):
            total += post["ups"] + post["num_comments"]
            matched.append(post["title"])
    return {"engagement": total, "matched_posts": matched}


def _significant_tokens(keyword: str) -> list[str]:
    """Palabras de la keyword con peso real (fuera stopwords y muy cortas).

    Si no queda ninguna (keywords de una palabra corta, como "fe"), se usa la
    keyword entera: mejor buscar poco que no buscar nada.
    """
    tokens = [
        token
        for token in keyword.lower().split()
        if len(token) > 3 and token not in _STOPWORDS
    ]
    return tokens or [keyword.lower().strip()]
