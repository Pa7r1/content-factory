"""Scorer: lógica pura, el sitio donde un fallo silencioso envenena todo lo demás.

La invariante que se prueba una y otra vez aquí es la del docstring del módulo:
**una señal ausente nunca vale 0**; los pesos de las presentes se re-normalizan.
"""

from __future__ import annotations

import pytest

from factory.research import scorer
from factory.research.scorer import Signals, score_idea

# Los pesos reales de config/settings.yaml. Aquí se escriben a mano a propósito:
# si alguien los cambia en el YAML, estos tests siguen midiendo la fórmula.
PESOS = {"demand": 0.35, "competition": 0.30, "evergreen": 0.20, "cpm": 0.15}


# ---------------------------------------------------------------------------
# Normalizadores, uno a uno
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("views", "esperado"),
    [
        (None, None),                       # la fuente no respondió
        ([], None),                         # respondió sin videos
        ([1_000_000], 1.0),                 # log10(1e6)/6 = 1
        ([1_000, 1_000], 0.5),              # log10(1e3)/6 = 0.5
        ([100, 1_000, 10_000], 0.5),        # se usa la mediana, no la media
        ([1], 0.0),                         # log10(1) = 0, sin dividir por cero
        ([0], 0.0),                         # cero vistas no revienta el log
        ([10_000_000], 1.0),                # por encima del techo se recorta
    ],
)
def test_views_norm_usa_la_mediana_en_escala_log(views, esperado):
    assert scorer.views_norm(views) == esperado


@pytest.mark.parametrize(
    ("last_7d", "last_30d", "esperado"),
    [
        (None, None, None),                 # sin dato de 7d no hay momentum
        (None, 30, None),                   # 30d solo no basta
        (15, 15, 1.0),                      # volumen 1 y recencia 1
        (0, 0, 0.0),                        # tema sin cobertura ninguna
        (30, 30, 1.0),                      # volumen por encima del techo: clamp
        (3, 30, 0.6 * 0.2 + 0.4 * 0.1),     # poco volumen, poca recencia
        (5, 0, 5 / 15),                     # 30d en cero: solo cuenta el volumen
        (5, None, 5 / 15),                  # 30d ausente: idem
        (10, 5, 0.6 * (10 / 15) + 0.4),     # datos incoherentes: recencia se recorta a 1
    ],
)
def test_momentum_norm_pondera_volumen_y_recencia(last_7d, last_30d, esperado):
    assert scorer.momentum_norm(last_7d, last_30d) == pytest.approx(esperado)


@pytest.mark.parametrize(
    ("engagement", "esperado"),
    [
        (None, None),                       # Reddit caído
        (0, 0.0),                           # respondió: cero engagement es un dato
        (5000, 1.0),                        # justo el techo
        (50_000, 1.0),                      # por encima: clamp
        (-10, 0.0),                         # dato absurdo, no ValueError del log
    ],
)
def test_reddit_norm_escala_el_engagement_en_log(engagement, esperado):
    assert scorer.reddit_norm(engagement) == pytest.approx(esperado)


@pytest.mark.parametrize(
    ("subs", "esperado"),
    [
        (None, None),
        ([], None),
        ([1_000, 2_000], 1.0),              # todos pequeños: hueco total
        ([500_000, 900_000], 0.0),          # todos gigantes: sin hueco
        ([1_000, 500_000], 0.5),
        ([100_000], 0.0),                   # el umbral es estricto: 100k no es pequeño
        ([99_999], 1.0),
    ],
)
def test_small_channel_share_mide_el_hueco_del_top(subs, esperado):
    assert scorer.small_channel_share(subs) == esperado


@pytest.mark.parametrize(
    ("edades", "esperado"),
    [
        (None, None),
        ([], None),
        ([730.0], 1.0),                     # dos años: techo
        ([365.0], 0.5),
        ([5000.0], 1.0),                    # más viejo que el techo: clamp
        ([0.0], 0.0),                       # subido hoy
    ],
)
def test_age_norm_normaliza_la_antiguedad_mediana(edades, esperado):
    assert scorer.age_norm(edades) == esperado


@pytest.mark.parametrize(
    ("edades", "esperado"),
    [
        (None, None),
        ([], None),
        ([10.0, 20.0], 0.0),                # top copado por videos de este mes
        ([200.0, 400.0], 1.0),              # nada reciente: hay sitio
        ([10.0, 400.0], 0.5),
        ([90.0], 1.0),                      # 90 días exactos ya NO es reciente
        ([89.9], 0.0),
    ],
)
def test_room_left_es_uno_menos_la_saturacion(edades, esperado):
    assert scorer.room_left(edades) == esperado


@pytest.mark.parametrize(
    ("meses", "esperado"),
    [
        (None, None),
        ([], None),
        ([100, 100], None),                 # con dos meses no hay serie que juzgar
        ([100, 100, 100], 1.0),             # interés plano: evergreen puro
        ([0, 0, 0], 0.0),                   # media cero: no se divide por cero
        ([100, 200, 300], 1 - (81.6496580927726 / 200)),  # pstdev/media
    ],
)
def test_evergreen_norm_penaliza_la_varianza_mensual(meses, esperado):
    assert scorer.evergreen_norm(meses) == pytest.approx(esperado)


def test_evergreen_norm_se_recorta_a_cero_con_un_pico_brutal():
    # Un mes viral entre once planos: coeficiente de variación > 1 → 0, no negativo.
    assert scorer.evergreen_norm([1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 100_000]) == 0.0


@pytest.mark.parametrize(
    ("cpm", "esperado"),
    [(None, None), (8.0, 1.0), (4.0, 0.5), (0.0, 0.0), (25.0, 1.0)],
)
def test_cpm_norm_compara_contra_la_referencia_de_ocho_dolares(cpm, esperado):
    assert scorer.cpm_norm(cpm) == esperado


# ---------------------------------------------------------------------------
# score_idea con todas las señales
# ---------------------------------------------------------------------------


SENALES_COMPLETAS = Signals(
    top_video_views=[1_000, 1_000],        # views      → 0.50
    top_channel_subs=[50_000, 50_000],     # small_ch.  → 1.00
    top_video_ages_days=[365.0, 365.0],    # age 0.50 / room_left 1.00
    news_last_7d=15,                       # momentum   → 1.00
    news_last_30d=15,
    reddit_engagement=5_000,               # reddit     → 1.00
    wikipedia_monthly_views=[100, 100, 100],  # evergreen → 1.00
    cpm_usd=4.0,                           # cpm        → 0.50
)
# demand      = 0.5*0.50 + 0.3*1.00 + 0.2*1.00 = 0.75
# competition = 0.5*1.00 + 0.3*0.50 + 0.2*1.00 = 0.85
# total       = 0.35*0.75 + 0.30*0.85 + 0.20*1.00 + 0.15*0.50 = 0.7925
SCORE_COMPLETO = 79.25


def test_el_score_con_todas_las_senales_sale_de_la_formula_documentada():
    resultado = score_idea(SENALES_COMPLETAS, PESOS)

    assert resultado.score == SCORE_COMPLETO
    assert resultado.components["demand"] == pytest.approx(0.75)
    assert resultado.components["competition"] == pytest.approx(0.85)
    assert resultado.components["evergreen"] == pytest.approx(1.0)
    assert resultado.components["cpm"] == pytest.approx(0.5)
    assert resultado.missing == []
    assert resultado.weights_used == pytest.approx(PESOS)


def test_con_todas_las_senales_los_pesos_efectivos_son_los_de_configuracion():
    resultado = score_idea(SENALES_COMPLETAS, PESOS)

    assert sum(resultado.weights_used.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Re-normalización: la invariante central
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("campos_ausentes", "componente"),
    [
        (("top_video_views", "news_last_7d", "news_last_30d", "reddit_engagement"), "demand"),
        (("top_channel_subs", "top_video_ages_days"), "competition"),
        (("wikipedia_monthly_views",), "evergreen"),
        (("cpm_usd",), "cpm"),
    ],
)
def test_un_componente_ausente_desaparece_del_reparto_de_pesos(campos_ausentes, componente):
    señales = _sin(SENALES_COMPLETAS, *campos_ausentes)

    resultado = score_idea(señales, PESOS)

    assert resultado.components[componente] is None
    assert componente not in resultado.weights_used
    assert sum(resultado.weights_used.values()) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "campo",
    [
        "top_video_views",
        "top_channel_subs",
        "top_video_ages_days",
        "news_last_7d",
        "reddit_engagement",
        "wikipedia_monthly_views",
        "cpm_usd",
    ],
)
def test_una_senal_ausente_no_cuenta_como_cero(campo):
    # El componente que pierde una sub-señal se recalcula con las que quedan;
    # el score resultante nunca puede ser menor que si la señal valiese 0.
    ausente = score_idea(_sin(SENALES_COMPLETAS, campo), PESOS)
    en_cero = score_idea(_con_cero(SENALES_COMPLETAS, campo), PESOS)

    assert ausente.score > en_cero.score
    assert campo not in {"cpm_usd"} or ausente.score > SCORE_COMPLETO


def test_sin_cpm_el_score_sube_porque_era_el_componente_mas_flojo():
    # 0.7175 / 0.85 = 0.844117..., frente a 0.7925 con el cpm de 0.5 dentro.
    resultado = score_idea(_sin(SENALES_COMPLETAS, "cpm_usd"), PESOS)

    assert resultado.score == 84.41
    assert resultado.weights_used == pytest.approx(
        {"demand": 0.35 / 0.85, "competition": 0.30 / 0.85, "evergreen": 0.20 / 0.85}
    )


def test_la_renormalizacion_es_de_dos_niveles():
    # Falta solo Reddit: demand se recalcula con views y momentum (0.5 y 0.3),
    # y los pesos de nivel superior siguen intactos porque demand sigue existiendo.
    resultado = score_idea(_sin(SENALES_COMPLETAS, "reddit_engagement"), PESOS)

    demanda_esperada = (0.5 * 0.5 + 0.3 * 1.0) / 0.8
    assert resultado.components["demand"] == pytest.approx(demanda_esperada)
    assert resultado.weights_used == pytest.approx(PESOS)
    assert resultado.missing == ["reddit"]


def test_missing_nombra_las_sub_senales_ausentes_ordenadas():
    señales = _sin(SENALES_COMPLETAS, "top_video_ages_days", "cpm_usd")

    resultado = score_idea(señales, PESOS)

    assert resultado.missing == ["age", "cpm", "room_left"]


# ---------------------------------------------------------------------------
# Extremos
# ---------------------------------------------------------------------------


def test_sin_ninguna_senal_el_score_es_cero_y_no_hay_pesos_que_usar():
    resultado = score_idea(Signals(), PESOS)

    assert resultado.score == 0.0
    assert resultado.weights_used == {}
    assert all(valor is None for valor in resultado.components.values())
    assert resultado.missing == [
        "age", "cpm", "evergreen", "momentum", "reddit",
        "room_left", "small_channels", "views",
    ]


def test_todas_las_senales_al_maximo_dan_cien():
    señales = Signals(
        top_video_views=[1_000_000],
        top_channel_subs=[1_000],
        top_video_ages_days=[730.0],
        news_last_7d=15,
        news_last_30d=15,
        reddit_engagement=5_000,
        wikipedia_monthly_views=[10, 10, 10],
        cpm_usd=8.0,
    )

    assert score_idea(señales, PESOS).score == 100.0


def test_todas_las_senales_al_minimo_dan_cero_sin_estar_ausentes():
    señales = Signals(
        top_video_views=[0],
        top_channel_subs=[10_000_000],
        top_video_ages_days=[0.0],
        news_last_7d=0,
        news_last_30d=0,
        reddit_engagement=0,
        wikipedia_monthly_views=[0, 0, 0],
        cpm_usd=0.0,
    )

    resultado = score_idea(señales, PESOS)

    assert resultado.score == 0.0
    assert resultado.missing == []  # cero medido no es lo mismo que sin medir


def test_el_score_siempre_cae_dentro_de_cero_y_cien():
    # Señales fuera de todo rango razonable: los techos son topes, no asíntotas.
    señales = Signals(
        top_video_views=[10**12],
        top_channel_subs=[0],
        top_video_ages_days=[100_000.0],
        news_last_7d=10_000,
        news_last_30d=1,
        reddit_engagement=10**9,
        wikipedia_monthly_views=[7, 7, 7],
        cpm_usd=1_000.0,
    )

    assert score_idea(señales, PESOS).score == 100.0


def test_los_pesos_de_configuracion_reales_producen_un_score_valido():
    from factory.core import config

    resultado = score_idea(SENALES_COMPLETAS, config.score_weights())

    assert 0.0 <= resultado.score <= 100.0


# ---------------------------------------------------------------------------
# Utilidades del test
# ---------------------------------------------------------------------------


def _sin(señales: Signals, *campos: str) -> Signals:
    """Copia de las señales con los campos indicados puestos a None."""
    return _reemplazar(señales, {campo: None for campo in campos})


_CEROS: dict[str, object] = {
    "top_video_views": [0],
    "top_channel_subs": [10_000_000],
    "top_video_ages_days": [0.0],
    "news_last_7d": 0,
    "news_last_30d": 0,
    "reddit_engagement": 0,
    "wikipedia_monthly_views": [0, 0, 0],
    "cpm_usd": 0.0,
}


def _con_cero(señales: Signals, campo: str) -> Signals:
    """Copia de las señales con ese campo en su valor mínimo medido (no ausente)."""
    return _reemplazar(señales, {campo: _CEROS[campo]})


def _reemplazar(señales: Signals, cambios: dict) -> Signals:
    actuales = {
        nombre: getattr(señales, nombre) for nombre in Signals.__dataclass_fields__
    }
    return Signals(**{**actuales, **cambios})
