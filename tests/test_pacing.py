"""El reloj del guion: palabras, segundos y la marca en la que entra cada capítulo.

Lógica pura y compartida. La usan dos sitios que no pueden importarse entre sí:
el writer, que estima las marcas del guion recién generado, y el editor del
dashboard, que las rehace cuando el humano cambia el texto. Si los dos no
contaran igual, las marcas de la descripción de YouTube mentirían.

Por eso aquí los números están escritos a mano —145 palabras por minuto— en vez
de calcularse llamando a la misma función que se comprueba: un test que se
recalcula solo acepta cualquier ritmo nuevo sin rechistar.
"""

from __future__ import annotations

from typing import Any

import pytest

from factory.core import pacing


def _palabras(cuantas: int) -> str:
    """Un texto con exactamente ese número de palabras."""
    return " ".join(["palabra"] * cuantas)


# Tres capítulos de 10, 20 y 30 palabras: 4, 8 y 12 segundos leídos.
CAPITULOS: list[dict[str, Any]] = [
    {"title": "El origen", "narration": _palabras(10), "words": 10},
    {"title": "La grieta", "narration": _palabras(20), "words": 20},
    {"title": "El final", "narration": _palabras(30), "words": 30},
]

# 5 palabras de gancho: 2 segundos.
GANCHO = _palabras(5)


# ---------------------------------------------------------------------------
# Palabras y segundos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "texto, palabras",
    [
        ("", 0),
        ("   ", 0),
        ("una", 1),
        ("dos   espacios   seguidos", 3),
        ("un salto\nde línea", 4),
        ("  bordes con espacios  ", 3),
        ("¿mañana? sí, con tildes y emoji 🎬", 7),
    ],
)
def test_las_palabras_se_cuentan_como_las_lee_un_locutor(texto: str, palabras: int):
    assert pacing.count_words(texto) == palabras


@pytest.mark.parametrize(
    "palabras, segundos",
    [
        (0, 0),
        (1, 0),        # menos de medio segundo: redondea a cero, no a uno
        (145, 60),     # el ritmo, por definición
        (1200, 497),   # un guion largo de verdad
    ],
)
def test_los_segundos_salen_del_ritmo_de_locucion(palabras: int, segundos: int):
    assert pacing.speech_seconds(palabras) == segundos


# ---------------------------------------------------------------------------
# Marcas de entrada de cada capítulo
# ---------------------------------------------------------------------------


def test_el_primer_capitulo_entra_en_cero_aunque_el_gancho_se_lea_antes():
    # YouTube exige que el primer marcador sea 00:00 o descarta la lista entera.
    marcados = pacing.with_start_times(_palabras(400), CAPITULOS)

    assert marcados[0]["start_sec"] == 0


def test_cada_capitulo_entra_donde_acaba_el_anterior_contando_el_gancho():
    # gancho 2 s; luego 4, 8 y 12 s de capítulo.
    marcados = pacing.with_start_times(GANCHO, CAPITULOS)

    assert [capitulo["start_sec"] for capitulo in marcados] == [0, 6, 14]


def test_marcar_los_capitulos_conserva_lo_que_ya_traian():
    marcados = pacing.with_start_times(GANCHO, CAPITULOS)

    assert marcados[1]["title"] == "La grieta"
    assert marcados[1]["narration"] == _palabras(20)
    assert marcados[1]["words"] == 20


def test_marcar_los_capitulos_no_toca_los_de_entrada():
    # El editor le pasa los capítulos que acaba de construir: si esta función
    # los mutase, el guardado escribiría marcas viejas mezcladas con nuevas.
    capitulos = [{"title": "Uno", "words": 10}]

    pacing.with_start_times(GANCHO, capitulos)

    assert capitulos == [{"title": "Uno", "words": 10}]


def test_un_guion_sin_capitulos_no_tiene_marcas():
    assert pacing.with_start_times(GANCHO, []) == []
    assert pacing.chapter_list([]) == ""


# ---------------------------------------------------------------------------
# La lista que va a la descripción de YouTube
# ---------------------------------------------------------------------------


def test_la_lista_de_capitulos_sale_en_el_formato_que_lee_youtube():
    marcados = pacing.with_start_times(GANCHO, CAPITULOS)

    assert pacing.chapter_list(marcados).splitlines() == [
        "00:00 El origen",
        "00:06 La grieta",
        "00:14 El final",
    ]


def test_un_capitulo_sin_marca_ni_titulo_no_revienta_la_lista():
    # `script_json` puede venir manipulado a mano: el hueco se ve, pero el
    # guardado del checkpoint humano no se cae.
    lista = pacing.chapter_list([{"title": "Sin marca"}, {"start_sec": 61}])

    assert lista.splitlines() == ["00:00 Sin marca", "01:01 "]


@pytest.mark.parametrize(
    "segundos, marca",
    [
        (0, "00:00"),
        (59, "00:59"),
        (60, "01:00"),
        (605, "10:05"),
        (3661, "61:01"),  # los guiones son de minutos: 61 minutos, no 01:01:01
    ],
)
def test_las_marcas_se_escriben_en_minutos_y_segundos(segundos: int, marca: str):
    assert pacing.mmss(segundos) == marca
