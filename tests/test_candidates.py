"""Criba determinista de candidatos: lógica pura, sin red y sin base de datos.

Es la capa más barata del sistema y la que decide qué se le enseña al LLM y qué
se acepta de vuelta, así que se prueba con el corpus que produjo el fallo:
`tests/fixtures/titulos_primera_pasada.json` guarda los títulos reales que la
primera pasada de investigación escribió en `ideas` como si fueran temas de
video.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.research import candidates

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "titulos_primera_pasada.json").read_text(
        encoding="utf-8"
    )
)["titulos"]

CASOS_REALES = [
    pytest.param(caso["titulo"], caso["util"], id=caso["motivo"]) for caso in CORPUS
]


# ---------------------------------------------------------------------------
# El corpus real de la primera pasada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("titulo", "util"), CASOS_REALES)
def test_la_criba_separa_los_titulos_reales_que_valen_de_los_que_no(titulo, util):
    assert candidates.is_usable_title(titulo) is util


@pytest.mark.parametrize("caso", CORPUS, ids=lambda c: c["titulo"][:30])
def test_todo_titulo_real_queda_escribible_en_la_pc_de_produccion(caso):
    limpio = candidates.clean_title(caso["titulo"])

    limpio.encode("cp1252")  # revienta el test si algún carácter no sobrevive


# ---------------------------------------------------------------------------
# clean_title
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("  hola   mundo  ", "hola mundo"),
        ("con\nsaltos\tde línea", "con saltos de línea"),
        ("Traición y venganza", "Traición y venganza"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_clean_title_normaliza_espacios_sin_perder_tildes(crudo, esperado):
    assert candidates.clean_title(crudo) == esperado


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("Por qué fracasan tus hábitos 🔥", "Por qué fracasan tus hábitos"),
        ("Traición ⚔️ y venganza", "Traición y venganza"),
        ("Hábitos → resultados", "Hábitos resultados"),
        ("Мотивация y hábitos", "y hábitos"),
    ],
)
def test_clean_title_sustituye_por_espacio_lo_que_no_existe_en_cp1252(crudo, esperado):
    assert candidates.clean_title(crudo) == esperado


def test_clean_title_no_pega_las_palabras_que_separaba_un_emoji():
    # Borrar el emoji en vez de sustituirlo daría "hábitosdiarios".
    assert candidates.clean_title("hábitos🔥diarios") == "hábitos diarios"


def test_clean_title_acota_la_longitud_del_titulo():
    largo = "á" * 500

    limpio = candidates.clean_title(largo)

    assert len(limpio) == candidates.MAX_TITLE_CHARS


# ---------------------------------------------------------------------------
# is_usable_title: casos límite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("titulo", "util"),
    [
        ("Hábitos diarios", True),          # exactamente MIN_TITLE_CHARS
        ("Hábitos diario", False),          # un carácter menos
        ("", False),
        ("   ", False),
        ("Hábitos diarios 🔥🔥🔥", True),    # los emojis no cuentan para el largo
        ("hábitos 🔥🔥🔥🔥🔥🔥🔥🔥", False),  # sin ellos se queda en 8 caracteres
    ],
)
def test_un_titulo_demasiado_corto_para_ser_un_tema_no_sirve(titulo, util):
    assert candidates.is_usable_title(titulo) is util


@pytest.mark.parametrize(
    ("titulo", "util"),
    [
        ("La rutina de los hábitos #hábitos", True),          # una etiqueta se tolera
        ("La rutina de los hábitos #hábitos #vida", False),   # dos ya es una cadena
    ],
)
def test_una_cadena_de_hashtags_no_es_un_tema(titulo, util):
    assert candidates.is_usable_title(titulo) is util


@pytest.mark.parametrize(
    "titulo",
    [
        "Nadie sabe lo que pasa (Video Oficial)",
        "Nadie sabe lo que pasa (Vídeo Oficial)",
        "Nadie sabe lo que pasa (VIDEO OFICIAL)",
        "Cancion nueva (Official Music Video)",
        "Cancion nueva (Audio Oficial)",
        "Cancion nueva (Visualizer)",
        "Cancion nueva (Lyric Video)",
        "Cancion nueva - Remix del verano",
        "Cancion nueva feat. otro artista",
        "Album de la temporada - Album Completo",
    ],
)
def test_un_lanzamiento_musical_nunca_es_un_tema_de_video(titulo):
    assert candidates.is_usable_title(titulo) is False


def test_un_short_marcado_con_hashtag_no_es_un_tema():
    assert candidates.is_usable_title("Como afrontar los problemas #shorts") is False


def test_la_palabra_shorts_sin_hashtag_no_descarta_el_titulo():
    # "Los shorts de verano que arrasan" habla de ropa, no de un Short.
    assert candidates.is_usable_title("Los shorts de verano que arrasan") is True


@pytest.mark.parametrize(
    ("titulo", "util"),
    [
        ("If you don't have Discipline you are a nobody", False),
        ("How to build habits that actually work for you", False),
        ("Los hábitos que funcionan de verdad", True),
        # Mezclado: lleva palabras españolas, así que es del canal.
        ("Los hábitos y el mindset that works", True),
        # Una sola marca inglesa, sin ninguna española, no basta para descartar.
        ("Habits that build discipline", True),
    ],
)
def test_un_titulo_en_otro_idioma_no_es_para_este_canal(titulo, util):
    assert candidates.is_usable_title(titulo) is util


# ---------------------------------------------------------------------------
# usable_video_titles
# ---------------------------------------------------------------------------


def _video(titulo: str, ratio: float | None = 1.0, duracion: int | None = 600) -> dict:
    return {"title": titulo, "outlier_ratio": ratio, "duration_sec": duracion}


def test_los_videos_salen_de_mayor_a_menor_outlier_ratio():
    videos = [
        _video("El hábito de levantarse temprano", ratio=0.4),
        _video("Por qué fracasan tus hábitos", ratio=1.7),
    ]

    titulos = candidates.usable_video_titles(videos, 5)

    assert titulos == [
        "Por qué fracasan tus hábitos",
        "El hábito de levantarse temprano",
    ]


def test_un_video_sin_outlier_ratio_va_al_final_en_vez_de_reventar():
    videos = [
        _video("El hábito de levantarse temprano", ratio=None),
        _video("Por qué fracasan tus hábitos", ratio=0.2),
    ]

    titulos = candidates.usable_video_titles(videos, 5)

    assert titulos[-1] == "El hábito de levantarse temprano"


def test_un_short_se_descarta_por_su_duracion_medida_aunque_el_titulo_sirva():
    videos = [_video("Los hábitos que funcionan de verdad", duracion=59)]

    assert candidates.usable_video_titles(videos, 5) == []


@pytest.mark.parametrize("duracion", [None, 0, 62, 600])
def test_un_video_que_no_es_un_short_se_conserva(duracion):
    # duration_sec 0 o ausente es duración DESCONOCIDA, no un Short de cero
    # segundos: descartarlo sería contar una ausencia como un dato.
    videos = [_video("Los hábitos que funcionan de verdad", duracion=duracion)]

    assert candidates.usable_video_titles(videos, 5) == [
        "Los hábitos que funcionan de verdad"
    ]


def test_los_videos_musicales_del_corpus_real_no_pasan_la_criba():
    videos = [
        _video("BAD BUNNY - NADIE SABE (Visualizer)", ratio=3.0),
        _video("Myke Towers - Lo Logré (Video Oficial)", ratio=2.0),
        _video("Los hábitos que funcionan de verdad", ratio=1.0),
    ]

    assert candidates.usable_video_titles(videos, 5) == [
        "Los hábitos que funcionan de verdad"
    ]


def test_el_titulo_del_video_llega_ya_limpio():
    videos = [_video("  Por qué fracasan   tus hábitos 🔥  ")]

    assert candidates.usable_video_titles(videos, 5) == ["Por qué fracasan tus hábitos"]


def test_se_devuelven_como_mucho_los_titulos_pedidos():
    videos = [_video(f"Los hábitos que funcionan, parte {i}", ratio=i) for i in range(9)]

    assert len(candidates.usable_video_titles(videos, 3)) == 3


def test_sin_videos_no_hay_titulos():
    assert candidates.usable_video_titles([], 5) == []


# ---------------------------------------------------------------------------
# usable_headlines y strip_outlet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("titular", "esperado"),
    [
        ("Un titular cualquiera - El País", "Un titular cualquiera"),
        ("Un titular cualquiera - BBC News Mundo", "Un titular cualquiera"),
        ("Sin medio detrás", "Sin medio detrás"),
        ("Doble - guion - Infobae", "Doble - guion"),
        (
            "Titular - con un medio absurdamente largo que no puede ser un medio de verdad",
            "Titular - con un medio absurdamente largo que no puede ser un medio de verdad",
        ),
        ("", ""),
    ],
)
def test_strip_outlet_quita_la_firma_del_medio(titular, esperado):
    assert candidates.strip_outlet(titular) == esperado


def test_los_titulares_llegan_sin_el_medio_y_ya_limpios():
    titulares = ["Un estudio desmonta la regla de los 21 días - El País"]

    assert candidates.usable_headlines(titulares, 5) == [
        "Un estudio desmonta la regla de los 21 días"
    ]


def test_un_titular_que_no_pasa_la_criba_no_llega_al_prompt():
    titulares = [
        "Myke Towers - Lo Logré (Video Oficial) - Billboard",
        "Un estudio desmonta la regla de los 21 días - El País",
    ]

    assert candidates.usable_headlines(titulares, 5) == [
        "Un estudio desmonta la regla de los 21 días"
    ]


def test_se_devuelven_como_mucho_los_titulares_pedidos():
    titulares = [f"Un titular cualquiera numero {i} - El País" for i in range(9)]

    assert len(candidates.usable_headlines(titulares, 2)) == 2


def test_sin_titulares_no_hay_material():
    assert candidates.usable_headlines([], 5) == []


# ---------------------------------------------------------------------------
# normalize y dedupe_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("Hábitos", "habitos"),
        ("HÁBITOS", "habitos"),
        ("  Dos   espacios  ", "dos espacios"),
        ("Ñandú", "nandu"),
        ("", ""),
    ],
)
def test_normalize_baja_a_minusculas_y_quita_tildes(crudo, esperado):
    assert candidates.normalize(crudo) == esperado


def test_dos_grafias_del_mismo_tema_comparten_clave_de_deduplicacion():
    # Los dos entraron en la misma pasada real por dos keywords distintas.
    assert candidates.dedupe_key("¿Cómo Afrontar Los Problemas?") == candidates.dedupe_key(
        "Como afrontar los problemas"
    )


def test_dos_temas_que_solo_se_diferencian_en_un_numero_no_son_el_mismo():
    assert candidates.dedupe_key("Los 7 hábitos") != candidates.dedupe_key("Los 8 hábitos")


@pytest.mark.parametrize(
    ("titulo", "clave"),
    [
        ("¡Hábitos, por fin!", "habitos por fin"),
        ("Hábitos: guía 2026", "habitos guia 2026"),
        ("...", ""),
    ],
)
def test_dedupe_key_deja_solo_palabras_y_numeros(titulo, clave):
    assert candidates.dedupe_key(titulo) == clave
