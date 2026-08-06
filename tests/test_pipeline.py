"""Orquestador `research_daily`: de keywords semilla a ideas puntuadas en la base.

Las cuatro fuentes se falsean a nivel de módulo (cada una tiene su propio
fichero de tests contra HTTP real interceptado). Lo que se prueba aquí es lo que
el pipeline añade encima: aislamiento por fuente, motivo de cada señal ausente,
temas concretos, dedupe y respeto a lo que el humano ya decidió.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import pytest
import responses
from freezegun import freeze_time

from factory.core.models import Job
from factory.research import (
    news_source,
    pipeline,
    reddit_source,
    wikipedia_source,
    youtube_source,
)
from factory.research.http_util import SourceUnavailable

AHORA = "2026-08-06 07:30:00"

# Reddit se falsea a nivel de módulo como las demás fuentes, salvo en los dos
# tests que comprueban qué pasa cuando responde 200 sin posts: esos necesitan la
# función de verdad, capturada aquí antes de que el fixture `fuentes` la sustituya.
COLLECT_TOP_POSTS_REAL = reddit_source.collect_top_posts
REDDIT_TOP_RE = re.compile(r"https://www\.reddit\.com/r/[^/]+/top\.json.*")

SETTINGS = {
    "niches": {
        "pruebas": {
            "name": "Pruebas",
            "language": "es",
            "keywords": ["hábitos"],
            "subreddits": ["DesarrolloPersonal"],
        }
    },
    "score_weights": {"demand": 0.35, "competition": 0.30, "evergreen": 0.20, "cpm": 0.15},
    "cpm_by_niche": {"pruebas": 4.0},
    "quotas": {"youtube": {"daily_budget": 4000}},
    "schedule": {"research_daily": "07:30", "fetch_metrics": "09:00"},
    "queue": {"poll_interval_seconds": 2, "max_attempts": 3},
}

BUSQUEDA = [
    {
        "video_id": "v1",
        "title": "  El hábito de   levantarse temprano  ",
        "channel_id": "c1",
        "channel_title": "Canal Uno",
        "published_at": "2025-08-06T07:30:00Z",
    },
    {
        "video_id": "v2",
        "title": "Por qué fracasan tus hábitos 🔥",
        "channel_id": "c2",
        "channel_title": "Canal Dos",
        "published_at": "2025-08-06T07:30:00Z",
    },
]
DETALLES = {
    "v1": {
        "title": "  El hábito de   levantarse temprano  ",
        "channel_id": "c1",
        "published_at": "2025-08-06T07:30:00Z",
        "views": 1_000,
        "likes": 10,
        "duration_sec": 600,
    },
    "v2": {
        "title": "Por qué fracasan tus hábitos 🔥",
        "channel_id": "c2",
        "published_at": "2025-08-06T07:30:00Z",
        "views": 5_000,
        "likes": 90,
        "duration_sec": 700,
    },
}
CANALES = {
    "c1": {"name": "Canal Uno", "subscribers": 50_000, "video_count": 10, "uploads_playlist": "UU1"},
    "c2": {"name": "Canal Dos", "subscribers": 50_000, "video_count": 20, "uploads_playlist": "UU2"},
}
NOTICIAS = {
    "last_7d": 15,
    "last_30d": 15,
    "headlines": [
        "Un estudio desmonta la regla de los 21 días - El País",
        "Otro titular cualquiera - Infobae",
    ],
}
WIKI = {"article": "Hábito (psicología)", "monthly_views": [100, 100, 100]}
POSTS_REDDIT = [
    {"title": "¿Cómo mantuvisteis vuestros hábitos?", "ups": 842, "num_comments": 213},
    {"title": "Nada que ver con el tema", "ups": 10, "num_comments": 1},
]

SCORE_COMPLETO = 79.36        # calculado en test_scorer con estas mismas señales
SCORE_SIN_YOUTUBE = 85.63


@pytest.fixture
def fuentes(monkeypatch: pytest.MonkeyPatch):
    """Instala las cuatro fuentes falsas y devuelve un ajustador por nombre."""
    monkeypatch.setattr(pipeline.time, "sleep", lambda segundos: None)

    estado: dict[str, Any] = {
        "search": lambda *a, **k: list(BUSQUEDA),
        "details": lambda ids: dict(DETALLES),
        "channels": lambda ids: dict(CANALES),
        "news": lambda keyword: dict(NOTICIAS),
        "wiki": lambda keyword: dict(WIKI),
        "reddit": lambda subs: list(POSTS_REDDIT),
    }

    monkeypatch.setattr(youtube_source, "search_videos", lambda *a, **k: estado["search"](*a, **k))
    monkeypatch.setattr(youtube_source, "videos_details", lambda ids: estado["details"](ids))
    monkeypatch.setattr(youtube_source, "channels_details", lambda ids: estado["channels"](ids))
    monkeypatch.setattr(news_source, "momentum_signal", lambda kw: estado["news"](kw))
    monkeypatch.setattr(wikipedia_source, "demand_signal", lambda kw: estado["wiki"](kw))
    monkeypatch.setattr(reddit_source, "collect_top_posts", lambda subs: estado["reddit"](subs))

    def ajustar(**cambios: Any) -> None:
        estado.update(cambios)

    return ajustar


@pytest.fixture
def entorno(conn: sqlite3.Connection, settings_falsas, fuentes):
    """Base migrada + configuración de prueba + fuentes falsas."""
    settings_falsas(SETTINGS)
    return fuentes


def _ideas(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM ideas ORDER BY id").fetchall())


def _detalles(fila: sqlite3.Row) -> dict[str, Any]:
    return json.loads(fila["score_details"])


# ---------------------------------------------------------------------------
# Pasada feliz
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_una_pasada_escribe_una_idea_por_tema_concreto(conn, entorno):
    escritas = pipeline.research_niche("pruebas")

    assert escritas == 3  # 2 temas de YouTube + 1 titular de News
    assert [f["source"] for f in _ideas(conn)] == ["youtube", "youtube", "news"]


@freeze_time(AHORA)
def test_las_ideas_llevan_el_score_y_sus_componentes(conn, entorno):
    pipeline.research_niche("pruebas")

    fila = _ideas(conn)[0]
    assert fila["score"] == SCORE_COMPLETO
    assert fila["demand"] == pytest.approx(0.7532, abs=1e-4)
    assert fila["competition"] == pytest.approx(0.85)
    assert fila["evergreen"] == pytest.approx(1.0)
    assert fila["cpm_factor"] == pytest.approx(0.5)


@freeze_time(AHORA)
def test_todos_los_temas_de_una_keyword_comparten_su_score(conn, entorno):
    pipeline.research_niche("pruebas")

    assert {f["score"] for f in _ideas(conn)} == {SCORE_COMPLETO}


@freeze_time(AHORA)
def test_los_temas_de_youtube_salen_ordenados_por_outlier_ratio(conn, entorno):
    # v2 (5000 vistas) se salió más de la media del top que v1 (1000).
    pipeline.research_niche("pruebas")

    de_youtube = [f for f in _ideas(conn) if f["source"] == "youtube"]
    assert de_youtube[0]["title"] == "Por qué fracasan tus hábitos 🔥"
    assert _detalles(de_youtube[0])["topic"]["outlier_ratio"] == pytest.approx(1.67, abs=0.01)


@freeze_time(AHORA)
def test_al_titular_de_news_se_le_quita_el_medio_y_se_marca_como_noticias(conn, entorno):
    pipeline.research_niche("pruebas")

    fila = [f for f in _ideas(conn) if f["source"] == "news"][0]
    assert fila["title"] == "Un estudio desmonta la regla de los 21 días"
    assert fila["suggested_format"] == "noticias"


@freeze_time(AHORA)
def test_los_titulos_llegan_normalizados_sin_espacios_sobrantes(conn, entorno):
    pipeline.research_niche("pruebas")

    titulos = [f["title"] for f in _ideas(conn)]
    assert "El hábito de levantarse temprano" in titulos


@freeze_time(AHORA)
def test_las_ideas_nacen_en_estado_new_con_su_nicho_y_keyword(conn, entorno):
    pipeline.research_niche("pruebas")

    for fila in _ideas(conn):
        assert fila["status"] == "new"
        assert fila["niche"] == "pruebas"
        assert fila["keyword"] == "hábitos"


@freeze_time(AHORA)
def test_score_details_guarda_las_senales_crudas_para_poder_explicar_el_numero(
    conn, entorno
):
    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert detalles["signals"]["youtube"]["vistas"] == [1_000, 5_000]
    assert detalles["signals"]["youtube"]["antiguedad_dias"] == [365.0, 365.0]
    assert detalles["signals"]["wikipedia"]["vistas_mensuales"] == [100, 100, 100]
    assert detalles["signals"]["reddit"]["engagement"] == 1_055
    assert detalles["signals"]["cpm_usd"] == 4.0
    assert detalles["missing_signals"] == []
    assert detalles["collected_at"] == "2026-08-06T07:30:00Z"


@freeze_time(AHORA)
def test_score_details_guarda_los_pesos_con_los_que_se_calculo(conn, entorno):
    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert detalles["weights_used"] == SETTINGS["score_weights"]


# ---------------------------------------------------------------------------
# Una fuente caída = señal ausente con motivo
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_youtube_caido_deja_la_senal_ausente_con_su_motivo_y_no_hunde_el_score(
    conn, entorno
):
    def caido(*a, **k):
        raise SourceUnavailable("HTTP 503 (no transitorio)", status=503)

    entorno(search=caido)

    escritas = pipeline.research_niche("pruebas")

    assert escritas == 1  # solo queda el tema de News
    detalles = _detalles(_ideas(conn)[0])
    assert "SourceUnavailable" in detalles["missing_reasons"]["youtube"]
    assert "503" in detalles["missing_reasons"]["youtube"]
    assert sorted(detalles["missing_signals"]) == ["age", "room_left", "small_channels", "views"]
    assert _ideas(conn)[0]["score"] == SCORE_SIN_YOUTUBE
    assert _ideas(conn)[0]["demand"] is not None


@pytest.mark.parametrize(
    ("fuente", "nombre"),
    [("search", "youtube"), ("news", "news"), ("wiki", "wikipedia")],
)
@freeze_time(AHORA)
def test_cualquier_fuente_caida_queda_registrada_por_su_nombre(
    conn, entorno, fuente, nombre
):
    def caido(*a, **k):
        raise SourceUnavailable("la fuente no responde")

    entorno(**{fuente: caido})

    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert nombre in detalles["missing_reasons"]


@freeze_time(AHORA)
def test_una_fuente_que_revienta_de_forma_inesperada_tampoco_tumba_la_pasada(
    conn, entorno
):
    def bug(*a, **k):
        raise KeyError("un campo que el parser daba por seguro")

    entorno(wiki=bug)

    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert "error inesperado" in detalles["missing_reasons"]["wikipedia"]
    assert "KeyError" in detalles["missing_reasons"]["wikipedia"]


@freeze_time(AHORA)
def test_una_fuente_sin_datos_de_la_keyword_se_distingue_de_una_caida(conn, entorno):
    entorno(wiki=lambda kw: None)

    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert detalles["missing_reasons"]["wikipedia"] == (
        "la fuente respondió pero no tiene datos de esta keyword"
    )


@freeze_time(AHORA)
def test_reddit_caido_deja_sin_senal_a_todas_las_keywords_del_nicho(conn, entorno):
    def caido(subs):
        raise SourceUnavailable("ningún subreddit respondió")

    entorno(reddit=caido)

    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert "SourceUnavailable" in detalles["missing_reasons"]["reddit"]
    assert "reddit" in detalles["missing_signals"]


@responses.activate
@freeze_time(AHORA)
def test_reddit_sin_posts_deja_la_senal_ausente_y_no_la_cuenta_como_cero(
    conn, entorno, sin_esperas
):
    # Regresión de operación: Reddit contestaba 200 con `children` vacío (sub
    # vacío o cuerpo de error bajo rate-limit) y la señal entraba valiendo 0,
    # hundiendo el score de TODAS las keywords del nicho. Debe quedar ausente
    # con su motivo para que el scorer re-normalice.
    # Aquí NO se falsea `collect_top_posts`: se falsea la respuesta HTTP, que es
    # justo la frontera donde apareció el fallo.
    responses.get(REDDIT_TOP_RE, json={"kind": "Listing", "data": {"children": []}}, status=200)
    entorno(reddit=COLLECT_TOP_POSTS_REAL)

    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert "ninguno trajo posts" in detalles["missing_reasons"]["reddit"]
    assert "reddit" in detalles["missing_signals"]
    assert detalles["sub_metrics"]["reddit"] is None       # ausente, no 0.0
    assert "reddit" not in detalles["signals"]


@responses.activate
@freeze_time(AHORA)
def test_sin_la_senal_de_reddit_el_peso_se_reparte_en_vez_de_contarla_como_cero(
    conn, entorno, sin_esperas
):
    # La demanda pasa a ser la media ponderada SOLO de las señales que llegaron
    # (views 0.5 + momentum 0.3, re-escaladas sobre 0.8). Contar la ausencia
    # como un 0 daría el numerador sin re-escalar, que es sensiblemente menor.
    responses.get(REDDIT_TOP_RE, json={"kind": "Listing", "data": {"children": []}}, status=200)
    entorno(reddit=COLLECT_TOP_POSTS_REAL)

    pipeline.research_niche("pruebas")

    fila = _ideas(conn)[0]
    sub = _detalles(fila)["sub_metrics"]
    si_valiese_cero = sub["views"] * 0.5 + sub["momentum"] * 0.3
    assert fila["demand"] == pytest.approx(si_valiese_cero / 0.8)
    assert fila["demand"] == pytest.approx(0.7372, abs=1e-4)
    assert fila["demand"] > si_valiese_cero
    assert _detalles(fila)["weights_used"] == SETTINGS["score_weights"]


@freeze_time(AHORA)
def test_un_nicho_sin_subreddits_configurados_lo_dice_sin_llamar_a_reddit(
    conn, settings_falsas, fuentes
):
    def no_deberia_llamarse(subs):
        raise AssertionError("no hay subreddits: no debería consultarse Reddit")

    sin_subs = json.loads(json.dumps(SETTINGS))
    sin_subs["niches"]["pruebas"].pop("subreddits")
    settings_falsas(sin_subs)
    fuentes(reddit=no_deberia_llamarse)

    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert detalles["missing_reasons"]["reddit"] == "el nicho no tiene subreddits configurados"


@freeze_time(AHORA)
def test_un_nicho_sin_cpm_en_la_tabla_lo_registra_como_senal_ausente(
    conn, settings_falsas, fuentes
):
    sin_cpm = json.loads(json.dumps(SETTINGS))
    sin_cpm["cpm_by_niche"] = {}
    settings_falsas(sin_cpm)

    pipeline.research_niche("pruebas")

    detalles = _detalles(_ideas(conn)[0])
    assert detalles["missing_reasons"]["cpm"] == "no hay CPM configurado para el nicho pruebas"
    assert "cpm" in detalles["missing_signals"]


# ---------------------------------------------------------------------------
# La keyword como sonda
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_sin_ningun_tema_concreto_se_graba_la_keyword_para_no_perder_la_medicion(
    conn, entorno
):
    entorno(search=lambda *a, **k: [], news=lambda kw: None)

    escritas = pipeline.research_niche("pruebas")

    assert escritas == 1
    fila = _ideas(conn)[0]
    assert fila["title"] == "hábitos"
    assert fila["source"] == "keyword"
    assert fila["score"] is not None
    assert "se guarda la sonda" in _detalles(fila)["topic"]["note"]


@freeze_time(AHORA)
def test_si_ninguna_fuente_responde_la_idea_sonda_se_graba_con_score_cero(
    conn, settings_falsas, fuentes
):
    def caido(*a, **k):
        raise SourceUnavailable("todo abajo")

    settings_falsas({**SETTINGS, "cpm_by_niche": {}})
    fuentes(search=caido, news=caido, wiki=caido, reddit=caido)

    escritas = pipeline.research_niche("pruebas")

    assert escritas == 1
    fila = _ideas(conn)[0]
    assert fila["score"] == 0.0
    assert fila["demand"] is None
    assert len(_detalles(fila)["missing_signals"]) == 8


@freeze_time(AHORA)
def test_un_video_sin_detalles_no_cuenta_como_tema(conn, entorno):
    # videos.list a veces no devuelve un id que search.list sí traía.
    entorno(details=lambda ids: {"v1": DETALLES["v1"]})

    pipeline.research_niche("pruebas")

    titulos = [f["title"] for f in _ideas(conn)]
    assert "Por qué fracasan tus hábitos 🔥" not in titulos
    assert "El hábito de levantarse temprano" in titulos


# ---------------------------------------------------------------------------
# Dedupe y respeto a lo que el humano decidió
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_dos_pasadas_actualizan_la_misma_idea_en_vez_de_duplicarla(conn, entorno):
    pipeline.research_niche("pruebas")
    antes = [f["id"] for f in _ideas(conn)]

    pipeline.research_niche("pruebas")

    assert [f["id"] for f in _ideas(conn)] == antes


@freeze_time(AHORA)
def test_la_segunda_pasada_refresca_el_score_de_la_idea_existente(conn, entorno):
    pipeline.research_niche("pruebas")
    conn.execute("UPDATE ideas SET score = 1.0")

    pipeline.research_niche("pruebas")

    assert {f["score"] for f in _ideas(conn)} == {SCORE_COMPLETO}


@freeze_time(AHORA)
def test_una_idea_rechazada_no_se_vuelve_a_proponer(conn, entorno):
    pipeline.research_niche("pruebas")
    conn.execute("UPDATE ideas SET status = 'rejected'")

    escritas = pipeline.research_niche("pruebas")

    assert escritas == 0
    assert len(_ideas(conn)) == 3
    assert {f["status"] for f in _ideas(conn)} == {"rejected"}


@freeze_time(AHORA)
def test_una_idea_ya_usada_no_se_vuelve_a_proponer(conn, entorno):
    pipeline.research_niche("pruebas")
    conn.execute("UPDATE ideas SET status = 'used'")

    escritas = pipeline.research_niche("pruebas")

    assert escritas == 0
    assert len(_ideas(conn)) == 3


@pytest.mark.parametrize("estado", ["shortlisted", "approved"])
@freeze_time(AHORA)
def test_una_idea_que_el_humano_ya_movio_no_se_toca(conn, entorno, estado):
    pipeline.research_niche("pruebas")
    conn.execute("UPDATE ideas SET status = ?, score = 1.0", (estado,))

    escritas = pipeline.research_niche("pruebas")

    assert escritas == 0
    assert {f["score"] for f in _ideas(conn)} == {1.0}
    assert len(_ideas(conn)) == 3


@freeze_time(AHORA)
def test_el_dedupe_es_por_nicho_keyword_y_titulo(conn, entorno):
    pipeline.research_niche("pruebas")
    conn.execute("UPDATE ideas SET niche = 'otro_nicho'")

    pipeline.research_niche("pruebas")

    assert len(_ideas(conn)) == 6


# ---------------------------------------------------------------------------
# Handler de la cola
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_el_handler_investiga_el_nicho_que_pide_el_payload(conn, entorno):
    pipeline.handle_research_daily(Job(id=1, type="research_daily", payload={"niche": "pruebas"}))

    assert len(_ideas(conn)) == 3


@freeze_time(AHORA)
def test_el_handler_sin_nicho_recorre_todos_los_configurados(
    conn, settings_falsas, fuentes
):
    dos_nichos = json.loads(json.dumps(SETTINGS))
    dos_nichos["niches"]["segundo"] = {
        "name": "Segundo", "language": "es", "keywords": ["disciplina"], "subreddits": ["x"],
    }
    dos_nichos["cpm_by_niche"]["segundo"] = 2.0
    settings_falsas(dos_nichos)

    pipeline.handle_research_daily(Job(id=1, type="research_daily", payload={}))

    assert {f["niche"] for f in _ideas(conn)} == {"pruebas", "segundo"}


@freeze_time(AHORA)
def test_un_nicho_desconocido_en_el_payload_es_un_error_del_llamador(conn, entorno):
    job = Job(id=1, type="research_daily", payload={"niche": "no_existe"})

    with pytest.raises(ValueError, match="nicho desconocido"):
        pipeline.handle_research_daily(job)


@freeze_time(AHORA)
def test_si_ninguna_keyword_se_puede_sondear_el_job_falla(
    conn, settings_falsas, fuentes
):
    # Pesos mal configurados: revienta dentro de cada keyword, no en una fuente.
    rotas = json.loads(json.dumps(SETTINGS))
    rotas["score_weights"] = {"demand": 0.9, "competition": 0.9, "evergreen": 0.9, "cpm": 0.9}
    settings_falsas(rotas)

    with pytest.raises(RuntimeError, match="no se pudo sondear ninguna keyword"):
        pipeline.research_niche("pruebas")

    assert _ideas(conn) == []


@freeze_time(AHORA)
def test_una_keyword_rota_no_cancela_las_demas_del_nicho(
    conn, settings_falsas, fuentes, monkeypatch
):
    dos_keywords = json.loads(json.dumps(SETTINGS))
    dos_keywords["niches"]["pruebas"]["keywords"] = ["rota", "hábitos"]
    settings_falsas(dos_keywords)

    original = pipeline.collect_keyword_signals

    def falla_solo_la_primera(context, keyword):
        if keyword == "rota":
            raise RuntimeError("esta keyword revienta")
        return original(context, keyword)

    monkeypatch.setattr(pipeline, "collect_keyword_signals", falla_solo_la_primera)

    escritas = pipeline.research_niche("pruebas")

    assert escritas == 3
    assert {f["keyword"] for f in _ideas(conn)} == {"hábitos"}


def test_register_engancha_el_handler_al_worker():
    from factory.core.queue import JobWorker

    worker = JobWorker()

    pipeline.register(worker)

    assert worker._handlers[pipeline.JOB_TYPE] is pipeline.handle_research_daily


@freeze_time(AHORA)
def test_se_hace_una_pausa_entre_keywords_para_no_provocar_un_429(
    conn, settings_falsas, fuentes, monkeypatch
):
    # Verificado en vivo: ~19 keywords seguidas hacen que es.wikipedia responda 429.
    esperas: list[float] = []
    monkeypatch.setattr(pipeline.time, "sleep", esperas.append)
    tres = json.loads(json.dumps(SETTINGS))
    tres["niches"]["pruebas"]["keywords"] = ["uno", "dos", "tres"]
    settings_falsas(tres)

    pipeline.research_niche("pruebas")

    assert esperas == [pipeline.PAUSE_BETWEEN_KEYWORDS] * 2


# ---------------------------------------------------------------------------
# Utilidades puras
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
    assert pipeline._strip_outlet(titular) == esperado


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("  hola   mundo  ", "hola mundo"),
        ("con\nsaltos\tde línea", "con saltos de línea"),
        ("Traición ⚔️ y venganza", "Traición ⚔️ y venganza"),
        ("", ""),
    ],
)
def test_clean_title_normaliza_espacios_sin_perder_tildes_ni_emojis(crudo, esperado):
    assert pipeline._clean_title(crudo) == esperado


def test_clean_title_acota_la_longitud_del_titulo():
    largo = "á" * 500

    limpio = pipeline._clean_title(largo)

    assert len(limpio) == pipeline.MAX_TITLE_CHARS


@freeze_time(AHORA)
@pytest.mark.parametrize(
    ("publicado", "dias"),
    [
        ("2025-08-06T07:30:00Z", 365.0),
        ("2026-08-06T07:30:00Z", 0.0),
        ("2026-08-05T07:30:00+00:00", 1.0),
        ("2027-01-01T00:00:00Z", 0.0),      # fecha futura: no cuenta negativo
        # Sin fecha legible la señal es AUSENTE (None). Un 0.0 diría "recién
        # subido" y contaría como saturación del top.
        ("no es una fecha", None),
        ("", None),
        (None, None),
    ],
)
def test_age_days_convierte_la_fecha_de_youtube_en_dias(publicado, dias):
    from datetime import datetime, timezone

    ahora = datetime(2026, 8, 6, 7, 30, tzinfo=timezone.utc)

    resultado = pipeline._age_days(publicado, ahora)

    if dias is None:
        assert resultado is None
    else:
        assert resultado == pytest.approx(dias)


@freeze_time(AHORA)
def test_rfc3339_da_el_formato_que_pide_youtube():
    from datetime import datetime, timezone

    momento = datetime(2026, 8, 6, 7, 30, 0, tzinfo=timezone.utc)

    assert pipeline._rfc3339(momento) == "2026-08-06T07:30:00Z"
