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
    # Sin filtro por tema: aquí se prueba el parseo del feed entero, no qué
    # titulares hablan de la keyword.
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    items = news_source.headlines("hábitos", only_on_topic=False)

    assert len(items) == 7
    assert items[0]["title"] == "Los hábitos que la neurociencia sí respalda - El País"
    assert items[0]["link"].startswith("https://news.google.com/rss/articles/")
    assert items[0]["published"] == datetime(2026, 8, 5, 7, 15, tzinfo=timezone.utc)


@responses.activate
def test_headlines_ordena_los_titulares_del_mas_nuevo_al_mas_viejo(muestra):
    # La muestra llega desordenada a propósito: el más viejo va el primero.
    # Sin filtro, para que el orden se compruebe sobre los siete titulares.
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    fechas = [i["published"] for i in news_source.headlines("hábitos", only_on_topic=False)]

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
    # El momentum se cuenta sobre los titulares DEL TEMA: de los siete de la
    # muestra, solo tres nombran "hábitos" (05-ago, 03-ago y 17-jul).
    # Dentro de 7d: 05-ago y 03-ago. El de 17-jul (20 d) solo entra en los 30d.
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    señal = news_source.momentum_signal("hábitos")

    assert señal["last_7d"] == 2
    assert señal["last_30d"] == 3
    assert len(señal["headlines"]) == 3


@freeze_time("2026-08-06 12:00:00")
@responses.activate
def test_sin_filtro_el_momentum_cuenta_todos_los_titulares_del_feed(muestra):
    # Dentro de 7d: 05-ago, 03-ago y 30-jul 13:00. Fuera por una hora: 30-jul 11:00.
    # Dentro de 30d: esos cuatro más el 17-jul. Fuera: 07-jul (30.2 d) y 14-mar.
    responses.get(RSS_RE, body=muestra("news_google_rss.xml"), status=200)

    señal = news_source.momentum_signal("hábitos", only_on_topic=False)

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

    # Sin filtro: lo que se prueba es el orden de la lista, no qué entra en ella.
    titulares = news_source.momentum_signal("hábitos", only_on_topic=False)["headlines"]

    assert titulares[0].startswith("Los hábitos que la neurociencia")
    assert titulares[-1].startswith("El viejo debate")


@responses.activate
def test_momentum_signal_sin_titulares_es_senal_ausente_no_error(muestra):
    # Tema sin cobertura: eso es un dato, no una fuente caída.
    responses.get(RSS_RE, body=muestra("news_google_rss_vacio.xml"), status=200)

    assert news_source.momentum_signal("keyword sin cobertura") is None


# ---------------------------------------------------------------------------
# Filtro por tema
#
# Google News responde a cualquier consulta con lo que se le parece: titulares
# que no hablan del tema entran igual y acaban convertidos en ideas de video.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("keyword", ["fantasía", "fantasia", "FANTASÍA"])
@responses.activate
def test_el_filtro_ignora_tildes_y_mayusculas_en_los_dos_sentidos(muestra, keyword):
    # La muestra trae "Fantasia epica" (sin tilde) y "La fantasía medieval"
    # (con ella): las tres grafías de la keyword tienen que traer las dos.
    responses.get(RSS_RE, body=muestra("news_google_rss_filtro.xml"), status=200)

    titulos = [i["title"] for i in news_source.headlines(keyword)]

    assert titulos == [
        "Fantasia epica: por que el genero no pasa de moda - El Pais",
        "La fantasía medieval vuelve a la televisión - Clarín",
    ]


@responses.activate
def test_una_keyword_de_varias_palabras_exige_la_frase_entera(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss_filtro.xml"), status=200)

    titulos = [i["title"] for i in news_source.headlines("hábitos de sueño")]

    assert titulos == ["Habitos de sueño: lo que de verdad dice la ciencia - BBC News Mundo"]


@responses.activate
def test_la_keyword_no_casa_dentro_de_otra_palabra(muestra):
    # "arte" está dentro de "cuarteto", pero ese titular no es del tema.
    responses.get(RSS_RE, body=muestra("news_google_rss_filtro.xml"), status=200)

    titulos = [i["title"] for i in news_source.headlines("arte")]

    assert titulos == ["El arte de no hacer nada, según los daneses - Milenio"]


@responses.activate
def test_si_ningun_titular_es_del_tema_headlines_devuelve_lista_vacia(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss_filtro.xml"), status=200)

    assert news_source.headlines("criptomonedas") == []


@freeze_time("2026-08-06 12:00:00")
@responses.activate
def test_sin_titulares_del_tema_el_momentum_es_none_y_nunca_cero(muestra):
    # El caso más caro del proyecto: el feed responde con siete titulares
    # recientes, pero ninguno del tema. Un {last_7d: 0} sería un dato falso que
    # hunde el score; la ausencia se propaga como ausencia para que el scorer
    # re-normalice los pesos.
    responses.get(RSS_RE, body=muestra("news_google_rss_filtro.xml"), status=200)

    señal = news_source.momentum_signal("criptomonedas")

    assert señal is None


@responses.activate
def test_only_on_topic_false_devuelve_el_feed_entero_sin_filtrar(muestra):
    responses.get(RSS_RE, body=muestra("news_google_rss_filtro.xml"), status=200)

    items = news_source.headlines("criptomonedas", only_on_topic=False)

    assert len(items) == 7


@responses.activate
def test_una_keyword_que_se_queda_vacia_al_normalizar_no_filtra_nada(muestra):
    # Sin este guardarraíl, una keyword en blanco dejaría el feed sin titulares
    # o los dejaría todos según cómo case la regex vacía; aquí se fija cuál es.
    responses.get(RSS_RE, body=muestra("news_google_rss_filtro.xml"), status=200)

    items = news_source.headlines("   ")

    assert len(items) == 7
