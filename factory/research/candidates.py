"""Criba determinista de candidatos: lo que evidentemente no es un tema de video.

Corre ANTES de gastar una sola llamada al LLM y es gratis. De la primera pasada
real salieron cuatro clases de basura que este módulo descarta:

- lanzamientos musicales ("... (Video Oficial)", "Visualizer", "Lyric Video"),
- Shorts y cadenas de hashtags ("#estoicismo #motivacion #inspirador"),
- títulos en otro idioma ("If you don't have Discipline you are a nobody"),
- duplicados exactos del mismo título por dos keywords distintas.

Además limpia el título de todo lo que no sobrevive a cp1252: la PC de
producción es un Windows y un emoji en un mensaje de log revienta la línea
entera, que es justo la que hace falta para diagnosticar.

Lógica pura: ni red ni base de datos.
"""

from __future__ import annotations

import logging
import re
import unicodedata

# Se reexportan a propósito: la criba compara títulos con las mismas reglas con
# las que la base guarda `ideas.dedupe_key`, y esas reglas viven en core/.
from factory.core.text import dedupe_key, normalize, words

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 200
MIN_TITLE_CHARS = 15
# Duración por debajo de la cual un video de YouTube es un Short. El tope real
# son 3 minutos desde 2024, pero un video de 2 minutos sobre el tema sigue
# siendo material válido; lo que no sirve es el clip vertical de 60 segundos.
SHORT_MAX_SECONDS = 61
MAX_HASHTAGS = 1

# Marcas inequívocas de lanzamiento musical, ya normalizadas (minúsculas y sin
# tildes). "video oficial" cubre también "Vídeo Oficial".
MUSIC_MARKERS = (
    "video oficial",
    "video musical",
    "official video",
    "official music video",
    "official audio",
    "audio oficial",
    "visualizer",
    "lyric video",
    "video lyric",
    "lyrics",
    "letra oficial",
    "album completo",
    "full album",
    "remix",
    "feat.",
    "ft.",
    "prod. by",
    "prod by",
)

# Palabras función que no se comparten entre español e inglés. Dos marcas
# inglesas sin ninguna española es un título que no es de este canal.
SPANISH_WORDS = frozenset(
    """de la el que los las una por para con del como se su sus lo mas y ni tu tus
    cuando donde cual cuales quien porque pero desde hasta sobre entre""".split()
)
ENGLISH_WORDS = frozenset(
    """the you your and of to with for how what if is are this that my we it from
    about why who dont doesnt cant have been will just""".split()
)
MIN_ENGLISH_WORDS = 2

_HASHTAG_RE = re.compile(r"#\w+")


def clean_title(title: str) -> str:
    """Título listo para la base: sin símbolos ilegibles, sin sobras, acotado.

    Los caracteres que no existen en cp1252 (emojis, flechas, alfabetos no
    latinos) se sustituyen por un espacio —no se borran— para no pegar dos
    palabras que estaban separadas por un emoji.
    """
    legible = "".join(car if _sobrevive_a_windows(car) else " " for car in title)
    return " ".join(legible.split())[:MAX_TITLE_CHARS]


def is_usable_title(title: str) -> bool:
    """¿Este título puede ser un tema de video para el canal?

    Se aplica tanto al material que entra (títulos ajenos, titulares) como a lo
    que devuelve el LLM: la misma criba en los dos extremos.
    """
    limpio = clean_title(title)
    if len(limpio) < MIN_TITLE_CHARS:
        return False
    if len(_HASHTAG_RE.findall(limpio)) > MAX_HASHTAGS:
        return False

    normalizado = normalize(limpio)
    if any(marca in normalizado for marca in MUSIC_MARKERS):
        return False
    if "shorts" in normalizado and "#" in limpio:
        return False
    return not _parece_otro_idioma(normalizado)


def usable_video_titles(videos: list[dict], limit: int) -> list[str]:
    """Títulos de video aprovechables, de mayor a menor outlier ratio.

    Los Shorts se descartan por duración, que es un dato medido y no una
    heurística sobre el título.
    """
    ordenados = sorted(videos, key=lambda v: v.get("outlier_ratio") or 0.0, reverse=True)
    titulos: list[str] = []
    for video in ordenados:
        if _es_short(video):
            continue
        if not is_usable_title(video["title"]):
            continue
        titulos.append(clean_title(video["title"]))
        if len(titulos) >= limit:
            break
    return titulos


def usable_headlines(headlines: list[str], limit: int) -> list[str]:
    """Titulares aprovechables, sin la firma del medio y ya limpios."""
    utiles: list[str] = []
    for headline in headlines:
        cuerpo = strip_outlet(headline)
        if not is_usable_title(cuerpo):
            continue
        utiles.append(clean_title(cuerpo))
        if len(utiles) >= limit:
            break
    return utiles


def strip_outlet(headline: str) -> str:
    """Quita el " - Medio" con el que Google News firma cada titular."""
    if " - " not in headline:
        return headline
    cuerpo, medio = headline.rsplit(" - ", 1)
    return cuerpo if len(medio) <= 40 else headline


def _sobrevive_a_windows(caracter: str) -> bool:
    """¿El carácter se puede escribir en un log de la PC de producción (cp1252)?"""
    if unicodedata.category(caracter).startswith("C"):
        return False
    try:
        caracter.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _es_short(video: dict) -> bool:
    """Un video de 60 segundos o menos es un Short, no un tema desarrollado."""
    duracion = video.get("duration_sec")
    return duracion is not None and 0 < duracion <= SHORT_MAX_SECONDS


def _parece_otro_idioma(normalizado: str) -> bool:
    """Título sin ninguna palabra española y con varias inglesas."""
    palabras = set(words(normalizado))
    if palabras & SPANISH_WORDS:
        return False
    return len(palabras & ENGLISH_WORDS) >= MIN_ENGLISH_WORDS
