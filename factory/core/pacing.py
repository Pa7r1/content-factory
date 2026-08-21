"""Ritmo de un guion: palabras, segundos y la marca en la que entra cada capítulo.

Vive en `core/` porque lo usan dos sitios que no pueden importarse entre sí: el
escritor de guiones (`script/writer.py`), que estima las marcas del guion recién
generado, y el editor del dashboard (`web/`), que las rehace cuando el humano
cambia el texto. Con una copia en cada lado, el mismo guion tendría marcas
distintas según quién lo hubiera tocado por última vez y las de la barra del
video acabarían mintiendo.

Todo son estimaciones de escritorio: la duración real solo se sabe cuando el
hito 3 sintetice el audio, y entonces se recalcula con la de verdad.

Lógica pura: ni red ni base de datos.
"""

from __future__ import annotations

from typing import Any

# Ritmo de locución en español con el que se estiman las duraciones.
WORDS_PER_MINUTE = 145


def count_words(texto: str) -> int:
    """Palabras del texto, contadas como las cuenta un locutor: separadas por espacios."""
    return len(texto.split())


def speech_seconds(palabras: int) -> int:
    """Segundos que se tarda en leer ese número de palabras en voz alta."""
    return round(palabras * 60 / WORDS_PER_MINUTE)


def with_start_times(
    hook: str, chapters: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Copia de los capítulos con el segundo en el que empieza cada uno.

    Cada capítulo de entrada trae ya su recuento en `words`. El primero empieza
    en 0 aunque el gancho se lea antes: YouTube exige que el primer marcador sea
    00:00 y el gancho no es un capítulo aparte.
    """
    transcurrido = speech_seconds(count_words(hook))
    marcados: list[dict[str, Any]] = []
    for indice, capitulo in enumerate(chapters):
        marcados.append({**capitulo, "start_sec": 0 if indice == 0 else transcurrido})
        transcurrido += speech_seconds(int(capitulo["words"]))
    return marcados


def chapter_list(chapters: list[dict[str, Any]]) -> str:
    """Los capítulos con su marca de tiempo, en el formato que lee YouTube.

    Un capítulo al que le falte la marca o el título entra igual, con el hueco a
    la vista: los que vienen de `with_start_times` los traen siempre, y un JSON
    manipulado a mano no debe reventar el guardado del checkpoint humano.
    """
    return "\n".join(
        f"{mmss(int(capitulo.get('start_sec') or 0))} {capitulo.get('title', '')}"
        for capitulo in chapters
    )


def mmss(segundos: int) -> str:
    """Segundos a 'MM:SS'. Los guiones son de minutos, nunca de horas."""
    return f"{segundos // 60:02d}:{segundos % 60:02d}"
