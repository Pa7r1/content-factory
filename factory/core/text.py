"""Normalización de texto compartida: comparar títulos sin que la grafía estorbe.

Vive en `core/` porque la usan tres sitios que no pueden importarse entre sí: la
criba de candidatos y el filtro de noticias (`research/`) y las migraciones de
`core/db.py`, que rellenan `ideas.dedupe_key` de las filas ya escritas. Un módulo
de dominio no importa de otro, y `core/` no puede importar de uno de dominio.

Lógica pura: ni red ni base de datos.
"""

from __future__ import annotations

import re
import unicodedata

# Los dígitos cuentan como palabra: sin ellos "los 7 hábitos" y "los 8 hábitos"
# tendrían la misma clave de deduplicación y una de las dos se perdería.
_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    """Minúsculas, sin tildes y con los espacios colapsados.

    Sin tildes porque el corpus es español y mezcla las dos grafías de cada
    palabra acentuada: "fantasia" tiene que casar con "fantasía". La ñ se pliega
    a n por el mismo mecanismo, y como se aplica a los dos lados de la
    comparación no importa.
    """
    descompuesto = unicodedata.normalize("NFD", text)
    sin_tildes = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return " ".join(sin_tildes.casefold().split())


def words(text: str) -> list[str]:
    """Las palabras del texto ya normalizadas, sin puntuación."""
    return _WORD_RE.findall(normalize(text))


def dedupe_key(title: str) -> str:
    """Clave de deduplicación: el título normalizado y sin puntuación.

    Con ella, "¿Cómo Afrontar Los Problemas?" y "Como afrontar los problemas"
    son la misma idea, que es lo que eran cuando entraron dos veces en la misma
    pasada por dos keywords distintas.
    """
    return " ".join(words(title))
