"""Wikipedia Pageviews: resolución de artículo, mes en curso y el 404 que no es caída."""

from __future__ import annotations

import re

import pytest
import responses
from freezegun import freeze_time

from factory.research import wikipedia_source
from factory.research.http_util import SourceUnavailable

SEARCH_URL = "https://es.wikipedia.org/w/api.php"
PAGEVIEWS_RE = re.compile(r"https://wikimedia\.org/api/rest_v1/metrics/pageviews/.*")

# Los cinco meses completos de la muestra; el sexto (202608) es el mes en curso.
MESES_COMPLETOS = [41_230, 39_880, 43_110, 61_044, 38_210]


# ---------------------------------------------------------------------------
# find_article
# ---------------------------------------------------------------------------


@responses.activate
def test_find_article_devuelve_el_primer_titulo_de_opensearch(muestra):
    responses.get(SEARCH_URL, json=muestra("wikipedia_opensearch.json"), status=200)

    assert wikipedia_source.find_article("hábitos") == "Hábito (psicología)"


@responses.activate
def test_find_article_sin_resultados_devuelve_none():
    responses.get(SEARCH_URL, json=["keyword rarísima", [], [], []], status=200)

    assert wikipedia_source.find_article("keyword rarísima") is None


@responses.activate
def test_find_article_soporta_una_respuesta_con_forma_inesperada():
    # La API ha devuelto objetos de error donde debería ir la lista de opensearch.
    responses.get(SEARCH_URL, json={"error": "algo"}, status=200)

    assert wikipedia_source.find_article("hábitos") is None


@responses.activate
def test_find_article_manda_el_user_agent_que_exige_wikimedia(muestra):
    responses.get(SEARCH_URL, json=muestra("wikipedia_opensearch.json"), status=200)

    wikipedia_source.find_article("hábitos")

    assert "content-factory" in responses.calls[0].request.headers["User-Agent"]


# ---------------------------------------------------------------------------
# monthly_pageviews
# ---------------------------------------------------------------------------


@freeze_time("2026-08-06 10:00:00")
@responses.activate
def test_monthly_pageviews_descarta_el_mes_en_curso(muestra):
    # El mes a medias dispararía la varianza y hundiría el evergreen sin motivo.
    responses.get(PAGEVIEWS_RE, json=muestra("wikipedia_pageviews.json"), status=200)

    vistas = wikipedia_source.monthly_pageviews("Hábito (psicología)")

    assert vistas == MESES_COMPLETOS
    assert 9_004 not in vistas  # el dato de agosto de 2026, incompleto


@freeze_time("2026-09-01 00:05:00")
@responses.activate
def test_al_cambiar_de_mes_el_anterior_ya_cuenta_como_completo(muestra):
    responses.get(PAGEVIEWS_RE, json=muestra("wikipedia_pageviews.json"), status=200)

    vistas = wikipedia_source.monthly_pageviews("Hábito (psicología)")

    assert vistas == [*MESES_COMPLETOS, 9_004]


@freeze_time("2026-08-06 10:00:00")
@responses.activate
def test_el_titulo_va_codificado_con_guiones_bajos_en_la_url(muestra):
    responses.get(PAGEVIEWS_RE, json=muestra("wikipedia_pageviews.json"), status=200)

    wikipedia_source.monthly_pageviews("Hábito (psicología)")

    url = responses.calls[0].request.url
    assert "H%C3%A1bito_%28psicolog%C3%ADa%29" in url
    # La ventana arranca el día 1 del mes que queda 12 meses atrás.
    assert "/monthly/20250801/20260806" in url


@freeze_time("2026-08-06 10:00:00")
@responses.activate
def test_la_ventana_de_doce_meses_arranca_doce_meses_atras(muestra):
    # `monthly_pageviews(months=12)` pide exactamente 12 meses: la ventana
    # arranca el 1 de agosto de 2025 y, descartado agosto de 2026 por
    # incompleto, quedan los 12 meses completos que promete la firma.
    responses.get(PAGEVIEWS_RE, json=muestra("wikipedia_pageviews.json"), status=200)

    wikipedia_source.monthly_pageviews("Hábito (psicología)", months=12)

    assert "/monthly/20250801/" in responses.calls[0].request.url


@responses.activate
def test_un_404_de_pageviews_significa_sin_datos_no_fuente_caida(muestra):
    responses.get(PAGEVIEWS_RE, json=muestra("wikipedia_pageviews_404.json"), status=404)

    assert wikipedia_source.monthly_pageviews("Artículo inexistente") == []


@responses.activate
def test_un_500_de_pageviews_si_es_fuente_caida_y_se_propaga(sin_esperas, muestra):
    for _ in range(3):
        responses.get(PAGEVIEWS_RE, json={}, status=500)

    with pytest.raises(SourceUnavailable):
        wikipedia_source.monthly_pageviews("Hábito (psicología)")


@responses.activate
def test_un_articulo_sin_ningun_item_devuelve_lista_vacia():
    responses.get(PAGEVIEWS_RE, json={"items": []}, status=200)

    assert wikipedia_source.monthly_pageviews("Recién creado") == []


# ---------------------------------------------------------------------------
# demand_signal
# ---------------------------------------------------------------------------


@freeze_time("2026-08-06 10:00:00")
@responses.activate
def test_demand_signal_junta_articulo_y_serie_mensual(muestra):
    responses.get(SEARCH_URL, json=muestra("wikipedia_opensearch.json"), status=200)
    responses.get(PAGEVIEWS_RE, json=muestra("wikipedia_pageviews.json"), status=200)

    señal = wikipedia_source.demand_signal("hábitos")

    assert señal == {
        "article": "Hábito (psicología)",
        "monthly_views": MESES_COMPLETOS,
    }


@responses.activate
def test_demand_signal_sin_articulo_es_senal_ausente_no_error(muestra):
    responses.get(SEARCH_URL, json=["nada", [], [], []], status=200)

    assert wikipedia_source.demand_signal("tema sin artículo") is None


@responses.activate
def test_demand_signal_con_articulo_pero_sin_pageviews_es_senal_ausente(muestra):
    responses.get(SEARCH_URL, json=muestra("wikipedia_opensearch.json"), status=200)
    responses.get(PAGEVIEWS_RE, json=muestra("wikipedia_pageviews_404.json"), status=404)

    assert wikipedia_source.demand_signal("hábitos") is None


@responses.activate
def test_demand_signal_propaga_la_caida_de_la_busqueda(sin_esperas):
    for _ in range(3):
        responses.get(SEARCH_URL, json={}, status=503)

    with pytest.raises(SourceUnavailable):
        wikipedia_source.demand_signal("hábitos")
