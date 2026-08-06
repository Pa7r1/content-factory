"""Acceso a datos del dashboard: ranking, desglose del score, cuota y decisiones.

Lo que se prueba aquí es lo que la plantilla no puede arreglar: que el ranking
enseñe lo que debe y en el orden que debe, que una señal ausente salga marcada
con SU motivo en vez de contar como cero, y que mover una idea respete la
whitelist de destinos y los estados terminales.

Base real en `tmp_path` (fixture `conn`), migraciones de producción, cero red.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from freezegun import freeze_time

from factory.web import queries

AHORA = "2026-08-06 07:30:00"

# Desglose tal como lo escribe el pipeline cuando dos fuentes se caen: Reddit
# (sub-señal de demanda) y Wikipedia (el componente evergreen entero).
DETALLES_CON_AUSENCIAS: dict[str, Any] = {
    "components": {"demand": 0.82, "competition": 0.5, "evergreen": None, "cpm": 0.5},
    "weights_used": {"demand": 0.4375, "competition": 0.375, "cpm": 0.1875},
    "missing_signals": ["reddit", "evergreen"],
    "missing_reasons": {
        "reddit": "SourceUnavailable: los subreddits respondieron pero ninguno trajo posts: r/Biblia",
        "wikipedia": "SourceUnavailable: HTTP 429 tras 3 intentos",
    },
}


def _insertar_idea(
    conn: sqlite3.Connection,
    *,
    title: str = "Una idea",
    niche: str = "pruebas",
    keyword: str | None = "hábitos",
    source: str | None = "youtube",
    score: float | None = 50.0,
    status: str = "new",
    suggested_format: str | None = None,
    score_details: Any = None,
) -> int:
    """Inserta una idea y devuelve su id. `score_details` se serializa si es dict."""
    if isinstance(score_details, (dict, list)):
        detalles = json.dumps(score_details, ensure_ascii=False)
    else:
        detalles = score_details
    cur = conn.execute(
        """
        INSERT INTO ideas (title, niche, keyword, source, score, status,
                           suggested_format, score_details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (title, niche, keyword, source, score, status, suggested_format, detalles),
    )
    return int(cur.lastrowid)


def _componente(vista: queries.IdeaView, nombre: str) -> queries.ComponentView:
    return next(c for c in vista.components if c.name == nombre)


# ---------------------------------------------------------------------------
# ranked_ideas
# ---------------------------------------------------------------------------


def test_el_ranking_ordena_de_mayor_a_menor_score(conn: sqlite3.Connection):
    _insertar_idea(conn, title="Floja", score=12.5)
    _insertar_idea(conn, title="Buena", score=91.0)
    _insertar_idea(conn, title="Regular", score=55.0)

    ranking = queries.ranked_ideas(conn)

    assert [idea.title for idea in ranking] == ["Buena", "Regular", "Floja"]


def test_a_igual_score_gana_la_idea_mas_antigua(conn: sqlite3.Connection):
    primera = _insertar_idea(conn, title="Primera", score=70.0)
    segunda = _insertar_idea(conn, title="Segunda", score=70.0)

    ranking = queries.ranked_ideas(conn)

    assert [idea.id for idea in ranking] == [primera, segunda]


@pytest.mark.parametrize("estado", ["new", "shortlisted"])
def test_el_ranking_muestra_las_ideas_pendientes_de_decision(
    conn: sqlite3.Connection, estado: str
):
    _insertar_idea(conn, title="Pendiente", status=estado)

    ranking = queries.ranked_ideas(conn)

    assert [idea.title for idea in ranking] == ["Pendiente"]


@pytest.mark.parametrize("estado", ["approved", "rejected", "used"])
def test_una_idea_ya_decidida_no_vuelve_al_ranking(
    conn: sqlite3.Connection, estado: str
):
    _insertar_idea(conn, title="Ya decidida", status=estado)

    assert queries.ranked_ideas(conn) == []


def test_se_pueden_pedir_otros_estados_explicitamente(conn: sqlite3.Connection):
    _insertar_idea(conn, title="Aprobada", status="approved")
    _insertar_idea(conn, title="Sin decidir", status="new")

    ranking = queries.ranked_ideas(conn, statuses=("approved",))

    assert [idea.title for idea in ranking] == ["Aprobada"]


def test_el_ranking_respeta_el_limite_quedandose_con_las_mejores(
    conn: sqlite3.Connection,
):
    for puntos in (10.0, 20.0, 30.0):
        _insertar_idea(conn, title=f"Idea {puntos}", score=puntos)

    ranking = queries.ranked_ideas(conn, limit=2)

    assert [idea.score for idea in ranking] == [30.0, 20.0]


def test_una_base_sin_ideas_da_un_ranking_vacio(conn: sqlite3.Connection):
    assert queries.ranked_ideas(conn) == []


def test_una_idea_sin_score_puntua_cero_en_vez_de_reventar(conn: sqlite3.Connection):
    _insertar_idea(conn, title="Sin puntuar", score=None)

    assert queries.ranked_ideas(conn)[0].score == 0.0


def test_la_fila_del_ranking_trae_todo_lo_que_pinta_la_plantilla(
    conn: sqlite3.Connection,
):
    idea_id = _insertar_idea(
        conn,
        title="Por qué fracasan tus hábitos 🔥",
        niche="crecimiento_personal",
        keyword="hábitos",
        source="news",
        score=79.4,
        suggested_format="noticias",
    )

    vista = queries.ranked_ideas(conn)[0]

    assert vista.id == idea_id
    assert vista.title == "Por qué fracasan tus hábitos 🔥"
    assert vista.niche == "crecimiento_personal"
    assert vista.keyword == "hábitos"
    assert vista.source == "news"
    assert vista.score == 79.4
    assert vista.status == "new"
    assert vista.suggested_format == "noticias"
    assert vista.updated_at is not None


# ---------------------------------------------------------------------------
# Desglose por componente
# ---------------------------------------------------------------------------


def test_el_desglose_trae_los_cuatro_componentes_con_su_etiqueta(
    conn: sqlite3.Connection,
):
    _insertar_idea(conn, score_details=DETALLES_CON_AUSENCIAS)

    componentes = queries.ranked_ideas(conn)[0].components

    assert [c.name for c in componentes] == ["demand", "competition", "evergreen", "cpm"]
    assert [c.label for c in componentes] == [
        "Demanda", "Hueco de competencia", "Evergreen", "CPM del nicho",
    ]


def test_cada_componente_lleva_su_valor_y_su_peso_efectivo(conn: sqlite3.Connection):
    _insertar_idea(conn, score_details=DETALLES_CON_AUSENCIAS)

    demanda = _componente(queries.ranked_ideas(conn)[0], "demand")

    assert demanda.value == 0.82
    assert demanda.weight == 0.4375


def test_una_senal_ausente_sale_con_su_motivo_y_no_hunde_el_componente(
    conn: sqlite3.Connection,
):
    # El caso que importa: Reddit no respondió. La demanda vale lo que valen las
    # señales que SÍ llegaron (0.82), y la ausencia se explica aparte.
    _insertar_idea(conn, score_details=DETALLES_CON_AUSENCIAS)

    demanda = _componente(queries.ranked_ideas(conn)[0], "demand")

    assert demanda.value == 0.82
    assert [ausente.signal for ausente in demanda.missing] == ["reddit"]
    assert demanda.missing[0].source == "reddit"
    assert "ninguno trajo posts" in demanda.missing[0].reason


def test_un_componente_entero_sin_senales_no_cuenta_y_lo_dice(
    conn: sqlite3.Connection,
):
    # Wikipedia caída: evergreen se queda sin valor y sin peso (se re-normalizó
    # sobre los otros tres), y el motivo es el de SU fuente, no el de Reddit.
    _insertar_idea(conn, score_details=DETALLES_CON_AUSENCIAS)

    evergreen = _componente(queries.ranked_ideas(conn)[0], "evergreen")

    assert evergreen.value is None
    assert evergreen.weight is None
    assert [ausente.signal for ausente in evergreen.missing] == ["evergreen"]
    assert evergreen.missing[0].source == "wikipedia"
    assert evergreen.missing[0].reason == "SourceUnavailable: HTTP 429 tras 3 intentos"


def test_los_componentes_completos_no_arrastran_ausencias_ajenas(
    conn: sqlite3.Connection,
):
    _insertar_idea(conn, score_details=DETALLES_CON_AUSENCIAS)

    vista = queries.ranked_ideas(conn)[0]

    assert _componente(vista, "competition").missing == []
    assert _componente(vista, "cpm").missing == []


@pytest.mark.parametrize(
    ("senal", "componente", "fuente"),
    [
        ("views", "demand", "youtube"),
        ("momentum", "demand", "news"),
        ("reddit", "demand", "reddit"),
        ("small_channels", "competition", "youtube"),
        ("age", "competition", "youtube"),
        ("room_left", "competition", "youtube"),
        ("evergreen", "evergreen", "wikipedia"),
        ("cpm", "cpm", "cpm"),
    ],
)
def test_cada_senal_ausente_se_atribuye_a_su_componente_y_a_su_fuente(
    conn: sqlite3.Connection, senal: str, componente: str, fuente: str
):
    _insertar_idea(
        conn,
        score_details={
            "missing_signals": [senal],
            "missing_reasons": {fuente: "la fuente no responde"},
        },
    )

    vista = queries.ranked_ideas(conn)[0]

    assert [a.signal for a in _componente(vista, componente).missing] == [senal]
    assert _componente(vista, componente).missing[0].source == fuente
    otros = [c for c in vista.components if c.name != componente]
    assert all(c.missing == [] for c in otros)


def test_una_ausencia_sin_motivo_registrado_lo_dice_en_vez_de_callarse(
    conn: sqlite3.Connection,
):
    _insertar_idea(conn, score_details={"missing_signals": ["views"], "missing_reasons": {}})

    demanda = _componente(queries.ranked_ideas(conn)[0], "demand")

    assert demanda.missing[0].reason == "sin motivo registrado"


def test_un_motivo_de_una_fuente_que_no_falto_no_ensucia_el_desglose(
    conn: sqlite3.Connection,
):
    # `missing_reasons` puede traer fuentes que al final sí dieron dato parcial.
    _insertar_idea(
        conn,
        score_details={
            "missing_signals": [],
            "missing_reasons": {"reddit": "SourceUnavailable: 403"},
        },
    )

    vista = queries.ranked_ideas(conn)[0]

    assert all(c.missing == [] for c in vista.components)


@pytest.mark.parametrize(
    "detalles", [None, "", "{no es json}", "[1, 2, 3]", '"un texto"'],
)
def test_un_score_details_ilegible_no_tumba_el_ranking(
    conn: sqlite3.Connection, detalles: str | None
):
    _insertar_idea(conn, title="Idea con detalles rotos", score_details=detalles)

    vista = queries.ranked_ideas(conn)[0]

    assert vista.title == "Idea con detalles rotos"
    assert [c.value for c in vista.components] == [None] * 4
    assert [c.weight for c in vista.components] == [None] * 4
    assert all(c.missing == [] for c in vista.components)


@pytest.mark.parametrize("basura", ["N/A", None, {}, []])
def test_un_valor_que_no_es_numero_se_trata_como_ausente(
    conn: sqlite3.Connection, basura: Any
):
    _insertar_idea(conn, score_details={"components": {"demand": basura}})

    assert _componente(queries.ranked_ideas(conn)[0], "demand").value is None


def test_un_valor_numerico_escrito_como_texto_se_convierte(conn: sqlite3.Connection):
    _insertar_idea(conn, score_details={"components": {"demand": "0.75"}})

    assert _componente(queries.ranked_ideas(conn)[0], "demand").value == 0.75


# ---------------------------------------------------------------------------
# count_ideas_by_status
# ---------------------------------------------------------------------------


def test_el_recuento_agrupa_las_ideas_por_estado(conn: sqlite3.Connection):
    _insertar_idea(conn, title="a", status="new")
    _insertar_idea(conn, title="b", status="new")
    _insertar_idea(conn, title="c", status="approved")
    _insertar_idea(conn, title="d", status="used")

    assert queries.count_ideas_by_status(conn) == {"new": 2, "approved": 1, "used": 1}


def test_el_recuento_de_una_base_vacia_es_un_diccionario_vacio(
    conn: sqlite3.Connection,
):
    assert queries.count_ideas_by_status(conn) == {}


# ---------------------------------------------------------------------------
# Cuota del día
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_la_cuota_de_hoy_compara_lo_gastado_con_el_corte_del_ochenta_por_ciento(
    conn: sqlite3.Connection, settings_falsas
):
    settings_falsas({"quotas": {"youtube": {"daily_budget": 4000}}})
    conn.execute(
        "INSERT INTO api_usage (api, day, units) VALUES ('youtube', '2026-08-06', 1600)"
    )

    cuota = queries.quota_today(conn)[0]

    assert cuota.api == "youtube"
    assert cuota.used == 1600
    assert cuota.budget == 4000
    assert cuota.cutoff == 3200
    assert cuota.pct_of_cutoff == 50.0


@freeze_time(AHORA)
def test_sin_consumo_hoy_la_cuota_esta_a_cero(conn: sqlite3.Connection, settings_falsas):
    settings_falsas({"quotas": {"youtube": {"daily_budget": 4000}}})

    cuota = queries.quota_today(conn)[0]

    assert cuota.used == 0
    assert cuota.pct_of_cutoff == 0.0


@freeze_time(AHORA)
def test_el_gasto_de_ayer_no_cuenta_contra_el_presupuesto_de_hoy(
    conn: sqlite3.Connection, settings_falsas
):
    settings_falsas({"quotas": {"youtube": {"daily_budget": 4000}}})
    conn.execute(
        "INSERT INTO api_usage (api, day, units) VALUES ('youtube', '2026-08-05', 3000)"
    )

    assert queries.quota_today(conn)[0].used == 0


@freeze_time(AHORA)
def test_un_presupuesto_de_cero_no_divide_por_cero(
    conn: sqlite3.Connection, settings_falsas
):
    settings_falsas({"quotas": {"youtube": {"daily_budget": 0}}})
    conn.execute(
        "INSERT INTO api_usage (api, day, units) VALUES ('youtube', '2026-08-06', 10)"
    )

    cuota = queries.quota_today(conn)[0]

    assert cuota.cutoff == 0
    assert cuota.pct_of_cutoff == 0.0


@freeze_time(AHORA)
def test_las_apis_salen_en_orden_alfabetico(conn: sqlite3.Connection, settings_falsas):
    settings_falsas(
        {"quotas": {"youtube": {"daily_budget": 4000}, "gemini": {"daily_budget": 100}}}
    )

    assert [c.api for c in queries.quota_today(conn)] == ["gemini", "youtube"]


def test_sin_apis_con_presupuesto_la_tabla_de_cuota_queda_vacia(
    conn: sqlite3.Connection, settings_falsas
):
    settings_falsas({"niches": {}})

    assert queries.quota_today(conn) == []


# ---------------------------------------------------------------------------
# update_idea_status
# ---------------------------------------------------------------------------


def test_aprobar_una_idea_la_deja_aprobada_y_devuelve_su_titulo(
    conn: sqlite3.Connection,
):
    idea_id = _insertar_idea(conn, title="Historias de la Biblia 📖")

    titulo = queries.update_idea_status(conn, idea_id, queries.APPROVED_STATUS)

    assert titulo == "Historias de la Biblia 📖"
    assert _estado(conn, idea_id) == "approved"


def test_descartar_una_idea_la_deja_descartada(conn: sqlite3.Connection):
    idea_id = _insertar_idea(conn)

    queries.update_idea_status(conn, idea_id, queries.REJECTED_STATUS)

    assert _estado(conn, idea_id) == "rejected"


def test_decidir_una_idea_actualiza_su_marca_de_tiempo(conn: sqlite3.Connection):
    idea_id = _insertar_idea(conn)
    conn.execute("UPDATE ideas SET updated_at = '2020-01-01 00:00:00' WHERE id = ?", (idea_id,))

    queries.update_idea_status(conn, idea_id, queries.APPROVED_STATUS)

    fila = conn.execute("SELECT updated_at FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    assert fila["updated_at"] > "2020-01-01 00:00:00"


@pytest.mark.parametrize("destino", ["used", "new", "shortlisted", "borrada", ""])
def test_el_dashboard_solo_mueve_ideas_a_aprobada_o_descartada(
    conn: sqlite3.Connection, destino: str
):
    idea_id = _insertar_idea(conn)

    with pytest.raises(ValueError, match="el dashboard no mueve ideas"):
        queries.update_idea_status(conn, idea_id, destino)

    assert _estado(conn, idea_id) == "new"


def test_decidir_una_idea_que_no_existe_es_un_error_explicito(
    conn: sqlite3.Connection,
):
    with pytest.raises(queries.IdeaNotFound, match="no existe la idea 404"):
        queries.update_idea_status(conn, 404, queries.APPROVED_STATUS)


def test_una_idea_ya_usada_esta_bloqueada_y_no_cambia_de_estado(
    conn: sqlite3.Connection,
):
    # Cambiarla dejaría huérfano al video que ya salió de ella.
    idea_id = _insertar_idea(conn, status="used")

    with pytest.raises(queries.IdeaLocked, match="ya no se cambia"):
        queries.update_idea_status(conn, idea_id, queries.REJECTED_STATUS)

    assert _estado(conn, idea_id) == "used"


def test_tras_un_bloqueo_la_conexion_sigue_pudiendo_escribir(
    conn: sqlite3.Connection,
):
    # Si la transacción se quedase abierta, el siguiente BEGIN IMMEDIATE de este
    # hilo fallaría para siempre y el dashboard quedaría de adorno.
    bloqueada = _insertar_idea(conn, title="Bloqueada", status="used")
    libre = _insertar_idea(conn, title="Libre")

    with pytest.raises(queries.IdeaLocked):
        queries.update_idea_status(conn, bloqueada, queries.APPROVED_STATUS)

    assert queries.update_idea_status(conn, libre, queries.APPROVED_STATUS) == "Libre"


def test_una_idea_descartada_se_puede_rehabilitar(conn: sqlite3.Connection):
    # 'rejected' no está en LOCKED_STATUSES: descartar por error tiene arreglo.
    idea_id = _insertar_idea(conn, status="rejected")

    queries.update_idea_status(conn, idea_id, queries.APPROVED_STATUS)

    assert _estado(conn, idea_id) == "approved"


def _estado(conn: sqlite3.Connection, idea_id: int) -> str:
    return conn.execute("SELECT status FROM ideas WHERE id = ?", (idea_id,)).fetchone()["status"]
