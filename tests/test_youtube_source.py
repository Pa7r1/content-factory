"""Cliente de YouTube Data API v3: parseo de respuestas reales y coste en cuota.

Las respuestas son muestras guardadas en `tests/fixtures/` con la forma que
devuelve la API de verdad (incluidos sus defectos: `likeCount` ausente cuando
los likes están ocultos, resultados de tipo canal colados en un search.list).
"""

from __future__ import annotations

import sqlite3

import pytest
import responses

from factory.core import quota
from factory.research import youtube_source
from factory.research.http_util import SourceUnavailable

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_URL = "https://www.googleapis.com/youtube/v3/playlistItems"


@pytest.fixture
def con_clave(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "clave-de-prueba-no-real")


# ---------------------------------------------------------------------------
# Duración ISO 8601: pura
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "segundos"),
    [
        ("PT4M13S", 253),
        ("PT1H2M3S", 3723),
        ("P1DT2H3M4S", 93_784),      # directos largos
        ("PT58S", 58),               # un short
        ("PT0S", 0),                 # vídeo sin duración declarada
        ("PT10M", 600),
        ("PT2H", 7200),
        ("", 0),                     # campo ausente
        ("basura", 0),
        ("P3D", 0),                  # sin la 'T' no casa el patrón
    ],
)
def test_parse_iso8601_duration(crudo, segundos):
    assert youtube_source.parse_iso8601_duration(crudo) == segundos


def test_parse_iso8601_duration_con_none_no_revienta():
    assert youtube_source.parse_iso8601_duration(None) == 0


# ---------------------------------------------------------------------------
# Sin clave
# ---------------------------------------------------------------------------


def test_sin_clave_en_el_entorno_la_fuente_no_esta_disponible(conn: sqlite3.Connection):
    with pytest.raises(SourceUnavailable, match="YOUTUBE_API_KEY"):
        youtube_source.search_videos("hábitos", "2024-01-01T00:00:00Z")


def test_sin_clave_no_se_gasta_ni_una_unidad_de_cuota(conn: sqlite3.Connection):
    with pytest.raises(SourceUnavailable):
        youtube_source.search_videos("hábitos", "2024-01-01T00:00:00Z")

    assert quota.usage_today("youtube") == 0


def test_una_clave_en_blanco_cuenta_como_ausente(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("YOUTUBE_API_KEY", "   ")

    with pytest.raises(SourceUnavailable, match="YOUTUBE_API_KEY"):
        youtube_source.search_videos("hábitos", "2024-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# search.list
# ---------------------------------------------------------------------------


@responses.activate
def test_search_videos_extrae_los_campos_utiles_de_cada_resultado(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.get(SEARCH_URL, json=muestra("youtube_search_list.json"), status=200)

    resultados = youtube_source.search_videos("hábitos", "2023-08-06T00:00:00Z", 10)

    assert resultados[0] == {
        "video_id": "5MgBikgcWnY",
        "title": "Cómo construir hábitos que SÍ duran (y por qué fallan los demás)",
        "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
        "channel_title": "Mentalidad Diaria",
        "published_at": "2023-05-14T10:00:04Z",
    }


@responses.activate
def test_search_videos_descarta_los_resultados_que_no_son_video(
    conn: sqlite3.Connection, con_clave, muestra
):
    # La muestra trae tres items; el tercero es un canal, sin id.videoId.
    responses.get(SEARCH_URL, json=muestra("youtube_search_list.json"), status=200)

    resultados = youtube_source.search_videos("hábitos", "2023-08-06T00:00:00Z")

    assert [r["video_id"] for r in resultados] == ["5MgBikgcWnY", "j2K0kZ9pQxA"]


@responses.activate
def test_search_videos_envia_los_parametros_de_busqueda_acordados(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.get(SEARCH_URL, json=muestra("youtube_search_list.json"), status=200)

    youtube_source.search_videos("hábitos", "2023-08-06T00:00:00Z", 10)

    url = responses.calls[0].request.url
    assert "order=viewCount" in url
    assert "type=video" in url
    assert "relevanceLanguage=es" in url
    assert "publishedAfter=2023-08-06T00%3A00%3A00Z" in url
    assert "key=clave-de-prueba-no-real" in url


@responses.activate
def test_search_videos_sin_resultados_devuelve_lista_vacia(
    conn: sqlite3.Connection, con_clave
):
    responses.get(SEARCH_URL, json={"kind": "youtube#searchListResponse", "items": []})

    assert youtube_source.search_videos("keyword rarísima", "2023-08-06T00:00:00Z") == []


@responses.activate
def test_una_busqueda_reserva_y_liquida_sus_cien_unidades(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.get(SEARCH_URL, json=muestra("youtube_search_list.json"), status=200)

    youtube_source.search_videos("hábitos", "2023-08-06T00:00:00Z")

    assert quota.usage_today("youtube") == youtube_source.COST_SEARCH
    fila = conn.execute("SELECT status, detail FROM api_usage").fetchone()
    assert fila["status"] == "settled"
    assert "search.list" in fila["detail"]


@responses.activate
def test_si_la_llamada_falla_la_reserva_queda_sin_liquidar_contando_de_mas(
    conn: sqlite3.Connection, con_clave, sin_esperas
):
    for _ in range(3):
        responses.get(SEARCH_URL, json={"error": {}}, status=503)

    with pytest.raises(SourceUnavailable):
        youtube_source.search_videos("hábitos", "2023-08-06T00:00:00Z")

    assert quota.usage_today("youtube") == 100
    assert conn.execute("SELECT status FROM api_usage").fetchone()["status"] == "reserved"


@responses.activate
def test_con_la_cuota_agotada_no_se_llega_a_llamar_a_youtube(
    conn: sqlite3.Connection, con_clave
):
    quota.reserve("youtube", 3_200, 4_000)  # tope efectivo alcanzado
    responses.get(SEARCH_URL, json={"items": []}, status=200)

    with pytest.raises(quota.QuotaExceeded):
        youtube_source.search_videos("hábitos", "2023-08-06T00:00:00Z")

    assert len(responses.calls) == 0


# ---------------------------------------------------------------------------
# videos.list
# ---------------------------------------------------------------------------


@responses.activate
def test_videos_details_normaliza_estadisticas_y_duracion(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.get(VIDEOS_URL, json=muestra("youtube_videos_list.json"), status=200)

    detalles = youtube_source.videos_details(["5MgBikgcWnY", "j2K0kZ9pQxA"])

    assert detalles["5MgBikgcWnY"] == {
        "title": "Cómo construir hábitos que SÍ duran (y por qué fallan los demás)",
        "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
        "published_at": "2023-05-14T10:00:04Z",
        "views": 1_543_210,
        "likes": 48_210,
        "duration_sec": 754,
    }


@responses.activate
def test_un_video_con_los_likes_ocultos_cuenta_cero_likes(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.get(VIDEOS_URL, json=muestra("youtube_videos_list.json"), status=200)

    detalles = youtube_source.videos_details(["j2K0kZ9pQxA"])

    assert detalles["j2K0kZ9pQxA"]["likes"] == 0
    assert detalles["j2K0kZ9pQxA"]["views"] == 87_421


@responses.activate
def test_videos_details_sin_ids_no_llama_ni_gasta_cuota(
    conn: sqlite3.Connection, con_clave
):
    assert youtube_source.videos_details([]) == {}
    assert len(responses.calls) == 0
    assert quota.usage_today("youtube") == 0


@responses.activate
def test_videos_details_trocea_en_lotes_de_cincuenta(
    conn: sqlite3.Connection, con_clave
):
    responses.get(VIDEOS_URL, json={"items": []}, status=200)
    ids = [f"vid{i:03d}" for i in range(120)]

    youtube_source.videos_details(ids)

    assert len(responses.calls) == 3  # 50 + 50 + 20
    assert quota.usage_today("youtube") == 3 * youtube_source.COST_LIST


# ---------------------------------------------------------------------------
# channels.list
# ---------------------------------------------------------------------------


@responses.activate
def test_channels_details_extrae_subs_y_playlist_de_uploads(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.get(CHANNELS_URL, json=muestra("youtube_channels_list.json"), status=200)

    canales = youtube_source.channels_details(["UCuAXFkgsw1L7xaCfnd5JJOw"])

    assert canales["UCuAXFkgsw1L7xaCfnd5JJOw"] == {
        "name": "Mentalidad Diaria",
        "subscribers": 87_400,
        "video_count": 312,
        "uploads_playlist": "UUuAXFkgsw1L7xaCfnd5JJOw",
    }


@responses.activate
def test_un_canal_con_los_subs_ocultos_cuenta_cero(
    conn: sqlite3.Connection, con_clave, muestra
):
    # hiddenSubscriberCount=true: la API omite subscriberCount por completo.
    responses.get(CHANNELS_URL, json=muestra("youtube_channels_list.json"), status=200)

    canales = youtube_source.channels_details(["UCX6b17PVsYBQ0ip5gyeme-Q"])

    assert canales["UCX6b17PVsYBQ0ip5gyeme-Q"]["subscribers"] == 0


@responses.activate
def test_channels_details_no_pide_dos_veces_el_mismo_canal(
    conn: sqlite3.Connection, con_clave, muestra
):
    # El top de una keyword suele repetir canal: pagar dos veces sería tirar cuota.
    responses.get(CHANNELS_URL, json=muestra("youtube_channels_list.json"), status=200)
    repetidos = ["UCuAXFkgsw1L7xaCfnd5JJOw"] * 5

    youtube_source.channels_details(repetidos)

    assert responses.calls[0].request.url.count("UCuAXFkgsw1L7xaCfnd5JJOw") == 1


# ---------------------------------------------------------------------------
# playlistItems.list
# ---------------------------------------------------------------------------


@responses.activate
def test_playlist_video_ids_pagina_hasta_completar_el_maximo(
    conn: sqlite3.Connection, con_clave
):
    def pagina(ids: list[str], token: str | None) -> dict:
        cuerpo: dict = {
            "items": [{"contentDetails": {"videoId": v}} for v in ids],
        }
        if token:
            cuerpo["nextPageToken"] = token
        return cuerpo

    responses.get(
        PLAYLIST_URL, json=pagina([f"a{i}" for i in range(50)], "PAGINA2"), status=200
    )
    responses.get(PLAYLIST_URL, json=pagina([f"b{i}" for i in range(10)], None), status=200)

    ids = youtube_source.playlist_video_ids("UUuAXFkgsw1L7xaCfnd5JJOw", 60)

    assert len(ids) == 60
    assert ids[0] == "a0" and ids[-1] == "b9"
    assert "pageToken=PAGINA2" in responses.calls[1].request.url


@responses.activate
def test_playlist_video_ids_para_cuando_no_hay_mas_paginas(
    conn: sqlite3.Connection, con_clave
):
    responses.get(
        PLAYLIST_URL,
        json={"items": [{"contentDetails": {"videoId": "solo1"}}]},
        status=200,
    )

    assert youtube_source.playlist_video_ids("UUxxx", 50) == ["solo1"]
    assert len(responses.calls) == 1


@responses.activate
def test_leer_los_uploads_cuesta_una_unidad_por_pagina_no_cien(
    conn: sqlite3.Connection, con_clave
):
    # Usar search.list para esto costaría 100 unidades por canal.
    responses.get(PLAYLIST_URL, json={"items": []}, status=200)

    youtube_source.playlist_video_ids("UUxxx", 50)

    assert quota.usage_today("youtube") == 1
