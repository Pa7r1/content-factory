"""Competidores: outlier ratio (lógica pura) y upsert idempotente.

`analyze_channel` se ejecuta contra las tres llamadas reales de YouTube
interceptadas en la frontera de transporte, no falseando `youtube_source`: así
el test cubre también el pegado entre módulos, que es donde se rompen las cosas.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

import pytest
import responses
from freezegun import freeze_time

from factory.research import competitor
from factory.research.http_util import SourceUnavailable

CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

CANAL_ID = "UCuAXFkgsw1L7xaCfnd5JJOw"
UPLOADS = "UUuAXFkgsw1L7xaCfnd5JJOw"


@pytest.fixture
def con_clave(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "clave-de-prueba-no-real")


def _video(vid: str, views: int, publicado: str, duracion: str = "PT10M") -> dict:
    return {
        "kind": "youtube#video",
        "id": vid,
        "snippet": {
            "publishedAt": publicado,
            "channelId": CANAL_ID,
            "title": f"Vídeo {vid}",
            "channelTitle": "Mentalidad Diaria",
        },
        "contentDetails": {"duration": duracion},
        "statistics": {"viewCount": str(views), "likeCount": "10"},
    }


@pytest.fixture
def youtube_falso(muestra):
    """Registra las tres respuestas que necesita `analyze_channel`."""

    def registrar(videos: list[dict]) -> None:
        responses.get(CHANNELS_URL, json=muestra("youtube_channels_list.json"), status=200)
        responses.get(
            PLAYLIST_URL,
            json={
                "items": [{"contentDetails": {"videoId": v["id"]}} for v in videos]
            },
            status=200,
        )
        responses.get(VIDEOS_URL, json={"items": videos}, status=200)

    return registrar


# ---------------------------------------------------------------------------
# summarize_uploads: lógica pura
# ---------------------------------------------------------------------------


@freeze_time("2026-08-06 12:00:00")
def test_el_outlier_ratio_es_las_vistas_frente_a_la_media_del_canal():
    videos = [
        {"views": 1_000, "duration_sec": 600, "published_at": "2026-07-01T00:00:00Z", "title": "a"},
        {"views": 2_000, "duration_sec": 600, "published_at": "2026-07-15T00:00:00Z", "title": "b"},
        {"views": 6_000, "duration_sec": 600, "published_at": "2026-08-01T00:00:00Z", "title": "c"},
    ]

    stats = competitor.summarize_uploads(videos)

    assert stats["avg_views"] == 3_000
    assert [v["outlier_ratio"] for v in stats["videos"]] == [1 / 3, 2 / 3, 2.0]


@freeze_time("2026-08-06 12:00:00")
def test_un_canal_sin_vistas_no_divide_por_cero():
    videos = [
        {"views": 0, "duration_sec": 60, "published_at": "2026-07-01T00:00:00Z", "title": "a"},
        {"views": 0, "duration_sec": 60, "published_at": "2026-07-02T00:00:00Z", "title": "b"},
    ]

    stats = competitor.summarize_uploads(videos)

    assert stats["avg_views"] == 0.0
    assert all(v["outlier_ratio"] == 0.0 for v in stats["videos"])


def test_summarize_uploads_con_lista_vacia_devuelve_ceros():
    stats = competitor.summarize_uploads([])

    assert stats == {
        "videos": [],
        "avg_views": 0.0,
        "avg_duration_sec": 0.0,
        "upload_frequency": 0.0,
    }


@freeze_time("2026-08-06 12:00:00")
def test_las_vistas_por_dia_de_un_video_subido_hoy_no_se_inflan():
    # Sin el suelo de 1 día, un vídeo de hace 10 minutos daría vistas/día absurdas.
    videos = [
        {"views": 500, "duration_sec": 60, "published_at": "2026-08-06T11:00:00Z", "title": "a"}
    ]

    stats = competitor.summarize_uploads(videos)

    assert stats["videos"][0]["views_per_day"] == 500.0


@freeze_time("2026-08-06 12:00:00")
def test_la_frecuencia_de_subida_se_mide_en_videos_por_semana():
    # 5 vídeos repartidos en 28 días exactos → 5 / 4 semanas = 1.25 por semana.
    videos = [
        {"views": 1, "duration_sec": 60, "published_at": f"2026-07-{dia:02d}T00:00:00Z", "title": "x"}
        for dia in (2, 9, 16, 23, 30)
    ]

    assert competitor.summarize_uploads(videos)["upload_frequency"] == 1.25


def test_con_un_solo_video_no_hay_frecuencia_que_medir():
    videos = [
        {"views": 1, "duration_sec": 60, "published_at": "2026-07-01T00:00:00Z", "title": "x"}
    ]

    assert competitor.summarize_uploads(videos)["upload_frequency"] == 0.0


def test_varios_videos_publicados_el_mismo_instante_no_dividen_por_cero():
    videos = [
        {"views": 1, "duration_sec": 60, "published_at": "2026-07-01T00:00:00Z", "title": "x"},
        {"views": 1, "duration_sec": 60, "published_at": "2026-07-01T00:00:00Z", "title": "y"},
    ]

    assert competitor.summarize_uploads(videos)["upload_frequency"] == 2.0


@freeze_time("2026-08-06 12:00:00")
def test_una_fecha_ilegible_no_tumba_el_resumen():
    videos = [
        {"views": 100, "duration_sec": 60, "published_at": "ayer por la tarde", "title": "x"},
        {"views": 100, "duration_sec": 60, "published_at": None, "title": "y"},
    ]

    stats = competitor.summarize_uploads(videos)

    assert stats["videos"][0]["views_per_day"] == 100.0
    assert stats["upload_frequency"] == 0.0


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("2026-08-06T10:00:00Z", datetime(2026, 8, 6, 10, tzinfo=timezone.utc)),
        ("2026-08-06T10:00:00+00:00", datetime(2026, 8, 6, 10, tzinfo=timezone.utc)),
        ("", None),
        (None, None),
        ("no es una fecha", None),
    ],
)
def test_parse_rfc3339(crudo, esperado):
    assert competitor._parse_rfc3339(crudo) == esperado


# ---------------------------------------------------------------------------
# analyze_channel: persistencia idempotente
# ---------------------------------------------------------------------------


@responses.activate
def test_analizar_un_canal_guarda_el_competidor_y_sus_videos(
    conn: sqlite3.Connection, con_clave, youtube_falso
):
    youtube_falso([
        _video("v1", 1_000, "2026-07-01T00:00:00Z"),
        _video("v2", 5_000, "2026-07-20T00:00:00Z"),
    ])

    resumen = competitor.analyze_channel(CANAL_ID, niche="crecimiento_personal")

    assert resumen["name"] == "Mentalidad Diaria"
    assert resumen["subscribers"] == 87_400
    assert resumen["videos_analyzed"] == 2
    assert resumen["avg_views"] == 3_000
    fila = conn.execute("SELECT * FROM competitors").fetchone()
    assert fila["channel_id"] == CANAL_ID
    assert fila["niche"] == "crecimiento_personal"
    assert conn.execute("SELECT COUNT(*) FROM competitor_videos").fetchone()[0] == 2


@responses.activate
def test_el_outlier_ratio_llega_a_la_base(
    conn: sqlite3.Connection, con_clave, youtube_falso
):
    youtube_falso([
        _video("v1", 1_000, "2026-07-01T00:00:00Z"),
        _video("v2", 5_000, "2026-07-20T00:00:00Z"),
    ])

    competitor.analyze_channel(CANAL_ID)

    ratios = {
        f["yt_video_id"]: f["outlier_ratio"]
        for f in conn.execute("SELECT yt_video_id, outlier_ratio FROM competitor_videos")
    }
    assert ratios == {"v1": pytest.approx(1 / 3), "v2": pytest.approx(5 / 3)}


@responses.activate
def test_analizar_dos_veces_el_mismo_canal_actualiza_en_vez_de_duplicar(
    conn: sqlite3.Connection, con_clave, youtube_falso
):
    youtube_falso([_video("v1", 1_000, "2026-07-01T00:00:00Z")])
    competitor.analyze_channel(CANAL_ID, niche="crecimiento_personal")

    responses.reset()
    youtube_falso([_video("v1", 9_000, "2026-07-01T00:00:00Z")])
    competitor.analyze_channel(CANAL_ID, niche="crecimiento_personal")

    assert conn.execute("SELECT COUNT(*) FROM competitors").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM competitor_videos").fetchone()[0] == 1
    assert conn.execute("SELECT views FROM competitor_videos").fetchone()["views"] == 9_000


@responses.activate
def test_reanalizar_sin_nicho_no_borra_el_nicho_ya_asignado(
    conn: sqlite3.Connection, con_clave, youtube_falso
):
    youtube_falso([_video("v1", 1_000, "2026-07-01T00:00:00Z")])
    competitor.analyze_channel(CANAL_ID, niche="crecimiento_personal")

    responses.reset()
    youtube_falso([_video("v1", 1_000, "2026-07-01T00:00:00Z")])
    competitor.analyze_channel(CANAL_ID)  # sin nicho

    fila = conn.execute("SELECT niche FROM competitors").fetchone()
    assert fila["niche"] == "crecimiento_personal"


@responses.activate
def test_un_canal_con_la_playlist_de_uploads_vacia_es_fuente_no_disponible(
    conn: sqlite3.Connection, con_clave, youtube_falso
):
    youtube_falso([])

    with pytest.raises(SourceUnavailable, match="playlist de uploads vino vacía"):
        competitor.analyze_channel(CANAL_ID)

    assert conn.execute("SELECT COUNT(*) FROM competitors").fetchone()[0] == 0


@responses.activate
def test_un_canal_que_youtube_no_devuelve_es_fuente_no_disponible(
    conn: sqlite3.Connection, con_clave
):
    responses.get(CHANNELS_URL, json={"items": []}, status=200)

    with pytest.raises(SourceUnavailable, match="YouTube no lo devolvió"):
        competitor.analyze_channel("UCcanalborrado")


@responses.activate
def test_analizar_un_canal_cuesta_tres_unidades_de_cuota(
    conn: sqlite3.Connection, con_clave, youtube_falso
):
    from factory.core import quota

    youtube_falso([_video("v1", 1_000, "2026-07-01T00:00:00Z")])

    competitor.analyze_channel(CANAL_ID)

    # channels.list + playlistItems.list + videos.list, nunca search.list (100).
    assert quota.usage_today("youtube") == 3
