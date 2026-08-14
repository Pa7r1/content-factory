"""Google News RSS: parseo del feed y conteo de momentum con el reloj congelado.

Un test de conteo "últimos 7 días" que no congela el tiempo caduca solo: la
muestra envejece y el test empieza a fallar un martes cualquiera.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
import responses
from freezegun import freeze_time

from factory.research import news_source
from factory.core.http_util import SourceUnavailable

RSS_RE = re.compile(r"https://news\.google\.com/rss/search.*")


# ---------------------------------------------------------------------------
# headlines
# ---------------------------------------------------------------------------


@responses.activate
def test_headlines_extrae_titulo_enlace_y_fecha_de_cada_item(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    items = news_source.headlines("hábitos")

    assert len(items) == 7
    assert items[0]["title"] == "Los hábitos que la neurociencia sí respalda - El País"
    assert items[0]["link"].startswith("https://news.google.com/rss/articles/")
    assert items[0]["published"] == datetime(2026, 8, 5, 7, 15, tzinfo=timezone.utc)


@responses.activate
def test_headlines_ordena_los_titulares_del_mas_nuevo_al_mas_viejo(muestra):
    # La muestra llega desordenada a propósito: el más viejo va el primero.
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    fechas = [i["published"] for i in news_source.headlines("hábitos")]

    assert fechas == sorted(fechas, reverse=True)
    assert fechas[0] == datetime(2026, 8, 5, 7, 15, tzinfo=timezone.utc)
    assert fechas[-1] == datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)


@responses.activate
def test_headlines_respeta_el_maximo_de_titulares(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    assert len(news_source.headlines("hábitos", max_items=2)) == 2


@responses.activate
def test_el_recorte_deberia_quedarse_con_los_titulares_mas_recientes(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    items = news_source.headlines("hábitos", max_items=2)

    assert [i["published"] for i in items] == [
        datetime(2026, 8, 5, 7, 15, tzinfo=timezone.utc),
        datetime(2026, 8, 3, 22, 40, tzinfo=timezone.utc),
    ]


@responses.activate
def test_headlines_codifica_la_consulta_en_la_url(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    news_source.headlines("traición y venganza")

    assert "traici%C3%B3n%20y%20venganza" in responses.calls[0].request.url


@responses.activate
def test_un_feed_valido_pero_sin_items_devuelve_lista_vacia(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss_vacio.xml"), status=200)

    assert news_source.headlines("keyword sin cobertura") == []


@responses.activate
def test_un_cuerpo_que_no_es_un_feed_es_fuente_no_disponible():
    responses.get(RSS_RE, body="no soy XML ni de lejos {", status=200)

    with pytest.raises(SourceUnavailable, match="feed ilegible"):
        news_source.headlines("hábitos")


@responses.activate
def test_un_503_del_feed_se_reintenta_y_acaba_en_fuente_no_disponible(sin_esperas):
    for _ in range(3):
        responses.get(RSS_RE, body="", status=503)

    with pytest.raises(SourceUnavailable):
        news_source.headlines("hábitos")

    assert len(responses.calls) == 3


# ---------------------------------------------------------------------------
# momentum_signal
# ---------------------------------------------------------------------------


@freeze_time("2026-08-06 12:00:00")
@responses.activate
def test_momentum_signal_cuenta_los_titulares_de_siete_y_treinta_dias(muestra):
    # Dentro de 7d: 05-ago, 03-ago y 30-jul 13:00. Fuera por una hora: 30-jul 11:00.
    # Dentro de 30d: esos cuatro más el 17-jul. Fuera: 07-jul (30.2 d) y 14-mar.
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    señal = news_source.momentum_signal("hábitos")

    assert señal["last_7d"] == 3
    assert señal["last_30d"] == 5
    assert len(señal["headlines"]) == 7


@freeze_time("2026-08-06 12:00:00")
@pytest.mark.parametrize(
    ("publicado", "dias", "cuenta"),
    [
        (datetime(2026, 7, 30, 13, 0, tzinfo=timezone.utc), 7, 1),   # 6h 59m dentro
        (datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc), 7, 0),   # 1h fuera
        (datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc), 7, 1),    # justo ahora
        (datetime(2026, 7, 7, 13, 0, tzinfo=timezone.utc), 30, 1),
        (datetime(2026, 7, 7, 8, 0, tzinfo=timezone.utc), 30, 0),
        (None, 7, 0),                                                 # sin fecha
    ],
)
def test_el_borde_de_la_ventana_de_conteo_es_estricto(publicado, dias, cuenta):
    items = [{"title": "x", "link": "", "published": publicado}]

    assert news_source._count_newer_than(items, days=dias) == cuenta


@freeze_time("2026-09-15 12:00:00")
@responses.activate
def test_un_mes_despues_el_momentum_del_mismo_feed_se_apaga(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    señal = news_source.momentum_signal("hábitos")

    assert señal["last_7d"] == 0
    assert señal["last_30d"] == 0


@freeze_time("2026-08-06 12:00:00")
@responses.activate
def test_momentum_signal_devuelve_los_titulares_en_orden_de_recencia(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    titulares = news_source.momentum_signal("hábitos")["headlines"]

    assert titulares[0].startswith("Los hábitos que la neurociencia")
    assert titulares[-1].startswith("El viejo debate")


@responses.activate
def test_momentum_signal_sin_titulares_es_senal_ausente_no_error(muestra):
    # Tema sin cobertura: eso es un dato, no una fuente caída.
    responses.get(RSS_RE, body=muestra("news_google_rss_vacio.xml"), status=200)

    assert news_source.momentum_signal("keyword sin cobertura") is None
