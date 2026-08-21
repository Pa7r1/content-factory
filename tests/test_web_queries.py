"""Acceso a datos del dashboard: ranking, desglose del score, cuota y decisiones.

Lo que se prueba aquí es lo que la plantilla no puede arreglar: que el ranking
enseñe lo que debe y en el orden que debe, que una señal ausente salga marcada
con SU motivo en vez de contar como cero, y que decidir sobre una idea respete
la whitelist de destinos y los estados terminales.

Aprobar es además la única escritura del dashboard que gasta dinero: encola un
guion, y cada guion es una generación completa de Gemini. Por eso su sección
prueba las tres cosas que lo evitan: el estado y el job salen juntos o no salen,
el segundo intento no encola nada y el job es el que el writer sabe atender.

La última sección es el checkpoint del guion, que es la mitigación de diseño
contra la política de contenido no auténtico de YouTube. Lo que se prueba ahí es
lo que no se puede deshacer: que la edición del humano no borre lo que escribió
el modelo, que los recuentos y las marcas se rehagan con el texto nuevo y que una
decisión ya tomada no la pise una segunda pestaña.

Base real en `tmp_path` (fixture `conn`), migraciones de producción, cero red.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from freezegun import freeze_time

from factory.core import models, queue
from factory.script import writer
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


def _jobs(conn: sqlite3.Connection) -> list[tuple[str, Any]]:
    """Todos los jobs de la cola como (tipo, payload). La cuenta importa: dos
    guiones encolados para la misma idea son dos llamadas a Gemini pagadas."""
    filas = conn.execute("SELECT type, payload FROM jobs ORDER BY id").fetchall()
    return [(fila["type"], json.loads(fila["payload"])) for fila in filas]


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


def test_descartar_una_idea_la_deja_descartada_y_devuelve_su_titulo(
    conn: sqlite3.Connection,
):
    idea_id = _insertar_idea(conn, title="Historias de la Biblia 📖")

    titulo = queries.update_idea_status(conn, idea_id, queries.REJECTED_STATUS)

    assert titulo == "Historias de la Biblia 📖"
    assert _estado(conn, idea_id) == "rejected"


def test_descartar_una_idea_no_encola_ningun_guion(conn: sqlite3.Connection):
    # El único camino que escribe en `jobs` es aprobar. Si la guarda compartida
    # acabase encolando también aquí, descartar costaría una llamada a Gemini.
    idea_id = _insertar_idea(conn)

    queries.update_idea_status(conn, idea_id, queries.REJECTED_STATUS)

    assert _jobs(conn) == []


def test_descartar_una_idea_actualiza_su_marca_de_tiempo(conn: sqlite3.Connection):
    idea_id = _insertar_idea(conn)
    conn.execute("UPDATE ideas SET updated_at = '2020-01-01 00:00:00' WHERE id = ?", (idea_id,))

    queries.update_idea_status(conn, idea_id, queries.REJECTED_STATUS)

    fila = conn.execute("SELECT updated_at FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    assert fila["updated_at"] > "2020-01-01 00:00:00"


@pytest.mark.parametrize(
    "destino", ["approved", "used", "new", "shortlisted", "borrada", ""]
)
def test_el_dashboard_solo_mueve_ideas_a_descartada_por_esta_via(
    conn: sqlite3.Connection, destino: str
):
    # 'approved' entra en la lista a propósito: aprobar escribe además en `jobs`
    # y tiene su propia función. Un camino que dejase la idea en 'approved' sin
    # guion en cola devolvería el estado ambiguo que A2 eliminó.
    idea_id = _insertar_idea(conn)

    with pytest.raises(ValueError, match="el dashboard no mueve ideas"):
        queries.update_idea_status(conn, idea_id, destino)

    assert _estado(conn, idea_id) == "new"


def test_descartar_una_idea_que_no_existe_es_un_error_explicito(
    conn: sqlite3.Connection,
):
    with pytest.raises(queries.IdeaNotFound, match="no existe la idea 404"):
        queries.update_idea_status(conn, 404, queries.REJECTED_STATUS)


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
        queries.update_idea_status(conn, bloqueada, queries.REJECTED_STATUS)

    assert queries.update_idea_status(conn, libre, queries.REJECTED_STATUS) == "Libre"


# ---------------------------------------------------------------------------
# approve_idea_for_script
# ---------------------------------------------------------------------------


def test_aprobar_una_idea_la_deja_usada_y_devuelve_su_titulo_y_su_job(
    conn: sqlite3.Connection,
):
    idea_id = _insertar_idea(conn, title="Historias de la Biblia 📖")

    aprobada = queries.approve_idea_for_script(conn, idea_id)

    assert aprobada.id == idea_id
    assert aprobada.title == "Historias de la Biblia 📖"
    assert _estado(conn, idea_id) == "used"
    assert [(tipo, payload) for tipo, payload in _jobs(conn)] == [
        ("write_script", {"idea_id": idea_id})
    ]


def test_el_job_que_se_encola_es_el_que_el_writer_sabe_atender(
    conn: sqlite3.Connection,
):
    # La web no puede importar de `factory.script`, así que la cadena está
    # duplicada. Este test es el único sitio donde las dos se miran a la cara:
    # si una se renombra, el job se encolaría sin handler y el guion no se
    # escribiría nunca, con el job muriendo en 'failed' sin que nadie mire.
    idea_id = _insertar_idea(conn)

    aprobada = queries.approve_idea_for_script(conn, idea_id)

    assert queries.WRITE_SCRIPT_JOB_TYPE == writer.JOB_TYPE
    fila = conn.execute(
        "SELECT type, payload, status FROM jobs WHERE id = ?", (aprobada.job_id,)
    ).fetchone()
    assert fila["type"] == writer.JOB_TYPE
    assert json.loads(fila["payload"]) == {"idea_id": idea_id}
    assert fila["status"] == "pending"


def test_el_estado_en_el_que_queda_la_idea_admite_guion_para_el_writer(
    conn: sqlite3.Connection,
):
    # El writer rechaza sin reintento las ideas cuyo estado no admite guion. Si
    # el dashboard dejase la idea en un estado que no está en esa lista, cada
    # aprobación encolaría un job condenado a 'failed'.
    assert queries.USED_STATUS in writer.WRITABLE_IDEA_STATUSES


def test_aprobar_dos_veces_no_encola_dos_guiones(conn: sqlite3.Connection):
    # Doble clic, dos pestañas o el botón "atrás": el segundo intento choca con
    # el estado dentro del BEGIN IMMEDIATE, antes de encolar nada.
    idea_id = _insertar_idea(conn)
    queries.approve_idea_for_script(conn, idea_id)

    with pytest.raises(queries.IdeaLocked, match="ya no se cambia"):
        queries.approve_idea_for_script(conn, idea_id)

    assert _jobs(conn) == [("write_script", {"idea_id": idea_id})]
    assert _estado(conn, idea_id) == "used"


def test_aprobar_una_idea_descartada_no_la_blanquea_ni_encola_su_guion(
    conn: sqlite3.Connection,
):
    # Descartar es definitivo, y esta es la única defensa de esa decisión:
    # 'rejected' está en LOCKED_STATUSES. Si saliese de ahí, aprobar una idea
    # ya descartada la devolvería a 'used' y pagaría una generación de Gemini
    # de algo que el humano dijo que no. Se afirman las tres consecuencias
    # —excepción, estado intacto y cola vacía— porque lo que cuesta dinero es
    # el job: lanzar DESPUÉS de encolar seguiría cobrando el guion.
    idea_id = _insertar_idea(conn, title="Ya descartada", status="rejected")

    with pytest.raises(queries.IdeaLocked, match="ya no se cambia"):
        queries.approve_idea_for_script(conn, idea_id)

    assert _estado(conn, idea_id) == "rejected"
    assert _jobs(conn) == []


def test_aprobar_una_idea_que_no_existe_no_encola_nada(conn: sqlite3.Connection):
    with pytest.raises(queries.IdeaNotFound, match="no existe la idea 404"):
        queries.approve_idea_for_script(conn, 404)

    assert _jobs(conn) == []


def test_si_el_encolado_del_guion_falla_la_idea_no_queda_aprobada(
    conn: sqlite3.Connection,
):
    # Lo que justifica la transacción única. Con el UPDATE y el INSERT en dos
    # transacciones, la idea quedaría en 'used' —bloqueada, invisible en el
    # ranking— sin guion en cola y sin forma de volver a pedirlo.
    idea_id = _insertar_idea(conn)
    conn.execute("DROP TABLE jobs")

    with pytest.raises(sqlite3.OperationalError, match="jobs"):
        queries.approve_idea_for_script(conn, idea_id)

    assert _estado(conn, idea_id) == "new"


def test_aprobar_no_crea_todavia_la_fila_del_video(conn: sqlite3.Connection):
    # La fila de `videos` la escribe el writer cuando el guion existe: una fila
    # vacía esperando guion le haría creer al writer que ya lo escribió.
    idea_id = _insertar_idea(conn)

    queries.approve_idea_for_script(conn, idea_id)

    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


def test_tras_un_bloqueo_al_aprobar_la_conexion_sigue_pudiendo_aprobar(
    conn: sqlite3.Connection,
):
    # Aprobar escribe dos veces dentro de la transacción: si el rollback del
    # camino bloqueado la dejase abierta, el dashboard quedaría de adorno.
    bloqueada = _insertar_idea(conn, title="Bloqueada", status="used")
    libre = _insertar_idea(conn, title="Libre")

    with pytest.raises(queries.IdeaLocked):
        queries.approve_idea_for_script(conn, bloqueada)

    assert queries.approve_idea_for_script(conn, libre).title == "Libre"
    assert _jobs(conn) == [("write_script", {"idea_id": libre})]


def _estado(conn: sqlite3.Connection, idea_id: int) -> str:
    return conn.execute("SELECT status FROM ideas WHERE id = ?", (idea_id,)).fetchone()["status"]


# ---------------------------------------------------------------------------
# Guiones: material del checkpoint humano
# ---------------------------------------------------------------------------


def _palabras(cuantas: int) -> str:
    """Un texto con exactamente ese número de palabras."""
    return " ".join(["palabra"] * cuantas)


# Un `script_json` tal como lo deja el writer: 5 palabras de gancho, capítulos
# de 10, 20 y 30 y 6 de cierre. Las marcas y los totales están escritos a mano
# —145 palabras por minuto— para que el test no dependa de la misma cuenta que
# comprueba. Son los mismos números que ancla `tests/test_pacing.py`.
GUION_DEL_MODELO: dict[str, Any] = {
    "format": "misterio",
    "model": "gemini-2.5-flash",
    "generated_at": "2026-08-14T10:00:00Z",
    "hook": _palabras(5),
    "chapters": [
        {"title": "El origen", "narration": _palabras(10), "words": 10, "start_sec": 0},
        {"title": "La grieta", "narration": _palabras(20), "words": 20, "start_sec": 6},
        {"title": "El final", "narration": _palabras(30), "words": 30, "start_sec": 14},
    ],
    "outro": _palabras(6),
    "word_count": 71,
    "words_per_minute": 145,
    "estimated_seconds": 29,
}

MARCADORES_DEL_MODELO = "00:00 El origen\n00:06 La grieta\n00:14 El final"


def _guion_del_modelo() -> dict[str, Any]:
    """Copia intacta del guion de muestra: ningún test le deja restos a otro."""
    return json.loads(json.dumps(GUION_DEL_MODELO))


def _insertar_video(
    conn: sqlite3.Connection,
    *,
    title: str = "Un guion por revisar",
    status: str = "script_draft",
    guion: Any = "el del modelo",
    idea_id: int | None = None,
    video_format: str | None = "misterio",
    updated_at: str | None = None,
) -> int:
    """Inserta un video con su guion. `guion` se serializa si es dict."""
    if guion == "el del modelo":
        guion = _guion_del_modelo()
    if isinstance(guion, dict):
        crudo: Any = json.dumps(guion, ensure_ascii=False)
        marcadores = "\n".join(
            f"{c['start_sec'] // 60:02d}:{c['start_sec'] % 60:02d} {c['title']}"
            for c in guion.get("chapters", [])
        )
    else:
        crudo, marcadores = guion, None
    cur = conn.execute(
        """
        INSERT INTO videos (idea_id, kind, format, title, chapters, script_json,
                            status, updated_at)
        VALUES (?, 'long', ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
        """,
        (idea_id, video_format, title, marcadores, crudo, status, updated_at),
    )
    return int(cur.lastrowid)


def _fila_video(conn: sqlite3.Connection, video_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()


def _guion_guardado(conn: sqlite3.Connection, video_id: int) -> dict[str, Any]:
    return json.loads(_fila_video(conn, video_id)["script_json"])


def _edicion(
    guion: dict[str, Any] | None = None,
    *,
    hook: str | None = None,
    outro: str | None = None,
    capitulos: list[tuple[str, str]] | None = None,
) -> queries.ScriptEdit:
    """Lo que devolvería el formulario: el guion tal cual, salvo lo que se cambie."""
    base = guion if guion is not None else GUION_DEL_MODELO
    return queries.ScriptEdit(
        hook=base["hook"] if hook is None else hook,
        outro=base["outro"] if outro is None else outro,
        chapters=tuple(
            queries.ChapterEdit(title=titulo, narration=narracion)
            for titulo, narracion in (
                capitulos
                if capitulos is not None
                else [(c["title"], c["narration"]) for c in base["chapters"]]
            )
        ),
    )


# ---------------------------------------------------------------------------
# scripts_awaiting_review
# ---------------------------------------------------------------------------


def test_la_lista_solo_trae_los_guiones_que_esperan_decision(conn: sqlite3.Connection):
    _insertar_video(conn, title="Esperando", status="script_draft")
    _insertar_video(conn, title="Ya aprobado", status="script_approved")
    _insertar_video(conn, title="Descartado", status="rejected")
    _insertar_video(conn, title="Sin guion todavía", status="idea_approved")

    lista = queries.scripts_awaiting_review(conn)

    assert [guion.title for guion in lista] == ["Esperando"]


def test_el_guion_que_lleva_mas_tiempo_esperando_sale_primero(conn: sqlite3.Connection):
    _insertar_video(conn, title="De ayer", updated_at="2026-08-13 09:00:00")
    _insertar_video(conn, title="De anteayer", updated_at="2026-08-12 09:00:00")
    _insertar_video(conn, title="De hoy", updated_at="2026-08-14 09:00:00")

    lista = queries.scripts_awaiting_review(conn)

    assert [guion.title for guion in lista] == ["De anteayer", "De ayer", "De hoy"]


def test_un_guion_sin_idea_detras_sigue_apareciendo_en_la_lista(
    conn: sqlite3.Connection,
):
    # `videos.idea_id` admite NULL. Con un JOIN normal el guion desaparecería de
    # la lista, y lo que el humano no ve no lo decide: el video se queda parado
    # para siempre.
    _insertar_video(conn, title="Huérfano", idea_id=None)

    lista = queries.scripts_awaiting_review(conn)

    assert [(guion.title, guion.niche) for guion in lista] == [("Huérfano", None)]


def test_la_linea_de_la_lista_trae_lo_que_pinta_la_plantilla(conn: sqlite3.Connection):
    idea_id = _insertar_idea(conn, niche="historias_epicas")
    video_id = _insertar_video(conn, title="Un guion por revisar", idea_id=idea_id)

    linea = queries.scripts_awaiting_review(conn)[0]

    assert linea.video_id == video_id
    assert linea.title == "Un guion por revisar"
    assert linea.niche == "historias_epicas"
    assert linea.format == "misterio"
    assert linea.chapter_count == 3
    assert linea.word_count == 71
    assert linea.estimated_minutes == 0.5
    assert linea.edited is False


def test_un_guion_ya_editado_se_ve_marcado_en_la_lista(conn: sqlite3.Connection):
    guion = _guion_del_modelo() | {"edited_by_human": True}
    _insertar_video(conn, guion=guion)

    assert queries.scripts_awaiting_review(conn)[0].edited is True


def test_un_script_json_ilegible_no_tumba_la_lista(conn: sqlite3.Connection):
    # El listado es la única puerta al checkpoint: una traza de 500 aquí deja
    # parados TODOS los guiones, no solo el que tiene el JSON roto.
    _insertar_video(conn, title="Con el JSON roto", guion="{esto no es json")

    linea = queries.scripts_awaiting_review(conn)[0]

    assert linea.title == "Con el JSON roto"
    assert linea.chapter_count == 0
    assert linea.word_count is None
    assert linea.estimated_minutes is None


# ---------------------------------------------------------------------------
# script_for_review
# ---------------------------------------------------------------------------


def test_el_detalle_trae_los_capitulos_numerados_y_con_su_marca(
    conn: sqlite3.Connection,
):
    video_id = _insertar_video(conn)

    detalle = queries.script_for_review(conn, video_id)

    assert [(c.number, c.title, c.start_mmss, c.words) for c in detalle.chapters] == [
        (1, "El origen", "00:00", 10),
        (2, "La grieta", "00:06", 20),
        (3, "El final", "00:14", 30),
    ]
    assert detalle.hook == _palabras(5)
    assert detalle.outro == _palabras(6)
    assert detalle.model == "gemini-2.5-flash"


def test_un_guion_ya_decidido_no_se_abre_en_el_editor(conn: sqlite3.Connection):
    video_id = _insertar_video(conn, status="script_approved")

    with pytest.raises(queries.ScriptLocked, match="script_approved"):
        queries.script_for_review(conn, video_id)


def test_un_video_que_no_existe_no_se_abre_en_el_editor(conn: sqlite3.Connection):
    with pytest.raises(queries.ScriptNotFound, match="no existe el video 404"):
        queries.script_for_review(conn, 404)


# ---------------------------------------------------------------------------
# save_script_draft: guardar sin decidir
# ---------------------------------------------------------------------------


def test_guardar_deja_el_texto_nuevo_y_el_guion_en_el_checkpoint(
    conn: sqlite3.Connection,
):
    video_id = _insertar_video(conn)

    titulo = queries.save_script_draft(conn, video_id, _edicion(hook="Otro gancho"))

    fila = _fila_video(conn, video_id)
    assert titulo == "Un guion por revisar"
    assert fila["status"] == "script_draft"
    assert _guion_guardado(conn, video_id)["hook"] == "Otro gancho"


def test_guardar_rehace_los_recuentos_y_las_marcas_con_el_texto_nuevo(
    conn: sqlite3.Connection,
):
    # El primer capítulo pasa de 10 a 40 palabras: 17 s en vez de 4. Todo lo que
    # va detrás se mueve. Los números son los de `pacing` escritos a mano.
    video_id = _insertar_video(conn)
    edicion = _edicion(
        capitulos=[
            ("El origen", _palabras(40)),
            ("La grieta", _palabras(20)),
            ("El final", _palabras(30)),
        ]
    )

    queries.save_script_draft(conn, video_id, edicion)

    guion = _guion_guardado(conn, video_id)
    assert [c["words"] for c in guion["chapters"]] == [40, 20, 30]
    assert [c["start_sec"] for c in guion["chapters"]] == [0, 19, 27]
    assert guion["word_count"] == 101
    assert guion["estimated_seconds"] == 42
    assert guion["words_per_minute"] == 145


def test_guardar_rehace_los_marcadores_que_van_a_youtube(conn: sqlite3.Connection):
    # `videos.chapters` es la lista que se pega en la descripción. Si no se
    # rehace, las marcas apuntan a segundos que ya no existen en el audio.
    video_id = _insertar_video(conn)
    edicion = _edicion(
        capitulos=[
            ("El origen", _palabras(40)),
            ("La grieta", _palabras(20)),
            ("Otro final", _palabras(30)),
        ]
    )

    queries.save_script_draft(conn, video_id, edicion)

    assert _fila_video(conn, video_id)["chapters"].splitlines() == [
        "00:00 El origen",
        "00:19 La grieta",
        "00:27 Otro final",
    ]


def test_la_primera_edicion_guarda_lo_que_habia_escrito_el_modelo(
    conn: sqlite3.Connection,
):
    video_id = _insertar_video(conn)

    queries.save_script_draft(conn, video_id, _edicion(hook="Lo reescribo yo"))

    guion = _guion_guardado(conn, video_id)
    assert guion["original"] == GUION_DEL_MODELO
    assert guion["edited_by_human"] is True
    assert guion["edited_at"].endswith("Z")


@freeze_time(AHORA)
def test_la_marca_de_la_edicion_se_escribe_como_la_del_modelo(
    conn: sqlite3.Connection,
):
    # `edited_at` y `generated_at` los va a leer el mismo análisis del hito 5:
    # con dos formatos distintos, comparar cuánto tardó el humano en corregir
    # exige adivinar cuál es cuál.
    video_id = _insertar_video(conn)

    queries.save_script_draft(conn, video_id, _edicion(hook="Lo reescribo yo"))

    assert _guion_guardado(conn, video_id)["edited_at"] == "2026-08-06T07:30:00Z"


def test_la_segunda_edicion_no_pisa_lo_que_habia_escrito_el_modelo(
    conn: sqlite3.Connection,
):
    # El dato con el que el hito 5 va a medir cuánto hay que corregirle al
    # modelo. Pisado una vez, no vuelve: no hay copia en ningún otro sitio.
    video_id = _insertar_video(conn)
    queries.save_script_draft(conn, video_id, _edicion(hook="Primera pasada"))

    primera = _guion_guardado(conn, video_id)
    queries.save_script_draft(conn, video_id, _edicion(primera, hook="Segunda pasada"))

    guion = _guion_guardado(conn, video_id)
    assert guion["hook"] == "Segunda pasada"
    assert guion["original"] == GUION_DEL_MODELO
    assert "original" not in guion["original"]


def test_guardar_sin_tocar_una_coma_no_marca_el_guion_como_editado(
    conn: sqlite3.Connection,
):
    # Abrir un guion, leerlo y pulsar guardar no es una edición. Marcarlo como
    # tal mentiría justo en la señal que mide la calidad del modelo.
    video_id = _insertar_video(conn)

    queries.save_script_draft(conn, video_id, _edicion())

    assert _guion_guardado(conn, video_id) == GUION_DEL_MODELO


def test_guardar_sin_cambios_tampoco_estrena_el_original(conn: sqlite3.Connection):
    video_id = _insertar_video(conn)

    queries.save_script_draft(conn, video_id, _edicion())

    assert "original" not in _guion_guardado(conn, video_id)


@pytest.mark.parametrize(
    "edicion, motivo",
    [
        (_edicion(hook=""), "gancho"),
        (_edicion(outro=""), "cierre"),
        (_edicion(capitulos=[]), "sin capítulos"),
        (_edicion(capitulos=[("", _palabras(10))]), "sin título"),
        (_edicion(capitulos=[("El origen", "")]), "sin narración"),
    ],
)
def test_una_edicion_con_un_hueco_no_se_guarda(
    conn: sqlite3.Connection, edicion: queries.ScriptEdit, motivo: str
):
    # El hito 3 locuta este texto tal cual: un hueco es un video mudo a medias.
    video_id = _insertar_video(conn)

    with pytest.raises(queries.ScriptInvalid, match=motivo):
        queries.save_script_draft(conn, video_id, edicion)

    assert _guion_guardado(conn, video_id) == GUION_DEL_MODELO


def test_una_edicion_invalida_deja_el_guion_entero_como_estaba(
    conn: sqlite3.Connection,
):
    video_id = _insertar_video(conn)

    with pytest.raises(queries.ScriptInvalid):
        queries.save_script_draft(
            conn,
            video_id,
            _edicion(capitulos=[("El origen", _palabras(40)), ("La grieta", "")]),
        )

    fila = _fila_video(conn, video_id)
    assert fila["status"] == "script_draft"
    assert fila["chapters"] == MARCADORES_DEL_MODELO
    assert json.loads(fila["script_json"]) == GUION_DEL_MODELO


def test_guardar_sobre_un_guion_ya_decidido_no_lo_pisa(conn: sqlite3.Connection):
    video_id = _insertar_video(conn, status="script_approved")

    with pytest.raises(queries.ScriptLocked, match="script_approved"):
        queries.save_script_draft(conn, video_id, _edicion(hook="Llego tarde"))

    assert _guion_guardado(conn, video_id) == GUION_DEL_MODELO


def test_guardar_en_un_video_que_no_existe_es_un_error_explicito(
    conn: sqlite3.Connection,
):
    with pytest.raises(queries.ScriptNotFound, match="no existe el video 404"):
        queries.save_script_draft(conn, 404, _edicion())


# ---------------------------------------------------------------------------
# approve_script
# ---------------------------------------------------------------------------


def test_aprobar_guarda_la_edicion_y_mueve_el_video_en_la_misma_transaccion(
    conn: sqlite3.Connection,
):
    video_id = _insertar_video(conn)

    titulo = queries.approve_script(conn, video_id, _edicion(hook="Con mis cambios"))

    fila = _fila_video(conn, video_id)
    assert titulo == "Un guion por revisar"
    assert fila["status"] == "script_approved"
    assert json.loads(fila["script_json"])["hook"] == "Con mis cambios"


def test_aprobar_conserva_tambien_lo_que_escribio_el_modelo(conn: sqlite3.Connection):
    # Aprobar es la última oportunidad de guardar el original: después el video
    # ya no vuelve al checkpoint.
    video_id = _insertar_video(conn)

    queries.approve_script(conn, video_id, _edicion(hook="Con mis cambios"))

    assert _guion_guardado(conn, video_id)["original"] == GUION_DEL_MODELO


def test_los_estados_del_checkpoint_son_transiciones_legales_del_modelo():
    # Las cadenas de `queries` y la máquina de estados de `core/models` están
    # escritas por separado. Si dejan de casar, aprobar reventaría con
    # IllegalTransition delante del único humano que puede desbloquear el video.
    assert models.can_transition(
        queries.SCRIPT_DRAFT_STATUS, queries.SCRIPT_APPROVED_STATUS
    )
    assert models.can_transition(
        queries.SCRIPT_DRAFT_STATUS, queries.SCRIPT_REJECTED_STATUS
    )


def test_aprobar_dos_veces_no_reabre_el_guion_ni_pisa_el_texto(
    conn: sqlite3.Connection,
):
    # La segunda pestaña, el doble clic y el botón "atrás".
    video_id = _insertar_video(conn)
    queries.approve_script(conn, video_id, _edicion(hook="La buena"))

    with pytest.raises(queries.ScriptLocked, match="script_approved"):
        queries.approve_script(conn, video_id, _edicion(hook="La tardía"))

    fila = _fila_video(conn, video_id)
    assert fila["status"] == "script_approved"
    assert json.loads(fila["script_json"])["hook"] == "La buena"


def test_una_edicion_invalida_no_aprueba_el_guion(conn: sqlite3.Connection):
    video_id = _insertar_video(conn)

    with pytest.raises(queries.ScriptInvalid):
        queries.approve_script(conn, video_id, _edicion(hook=""))

    assert _fila_video(conn, video_id)["status"] == "script_draft"


def test_aprobar_un_video_que_no_existe_es_un_error_explicito(
    conn: sqlite3.Connection,
):
    with pytest.raises(queries.ScriptNotFound, match="no existe el video 404"):
        queries.approve_script(conn, 404, _edicion())


def test_tras_un_guion_bloqueado_la_conexion_sigue_pudiendo_aprobar(
    conn: sqlite3.Connection,
):
    # Aprobar escribe dentro de una transacción: si el rollback del camino
    # bloqueado la dejase abierta, el checkpoint entero quedaría de adorno.
    bloqueado = _insertar_video(conn, title="Bloqueado", status="script_approved")
    libre = _insertar_video(conn, title="Libre")

    with pytest.raises(queries.ScriptLocked):
        queries.approve_script(conn, bloqueado, _edicion())

    assert queries.approve_script(conn, libre, _edicion()) == "Libre"
    assert _fila_video(conn, libre)["status"] == "script_approved"


# ---------------------------------------------------------------------------
# reject_script
# ---------------------------------------------------------------------------


def test_descartar_un_guion_mueve_el_video_y_no_toca_el_texto(
    conn: sqlite3.Connection,
):
    # Quien descarta no quiere conservar su edición: escribirla dejaría en la
    # base un texto que nadie va a volver a mirar como si fuera el guion bueno.
    video_id = _insertar_video(conn)

    titulo = queries.reject_script(conn, video_id)

    fila = _fila_video(conn, video_id)
    assert titulo == "Un guion por revisar"
    assert fila["status"] == "rejected"
    assert json.loads(fila["script_json"]) == GUION_DEL_MODELO
    assert fila["chapters"] == MARCADORES_DEL_MODELO


def test_descartar_dos_veces_el_mismo_guion_da_bloqueado(conn: sqlite3.Connection):
    video_id = _insertar_video(conn)
    queries.reject_script(conn, video_id)

    with pytest.raises(queries.ScriptLocked, match="rejected"):
        queries.reject_script(conn, video_id)


def test_descartar_un_guion_ya_aprobado_no_lo_devuelve_atras(
    conn: sqlite3.Connection,
):
    video_id = _insertar_video(conn, status="script_approved")

    with pytest.raises(queries.ScriptLocked, match="script_approved"):
        queries.reject_script(conn, video_id)

    assert _fila_video(conn, video_id)["status"] == "script_approved"


def test_descartar_un_video_que_no_existe_es_un_error_explicito(
    conn: sqlite3.Connection,
):
    with pytest.raises(queries.ScriptNotFound, match="no existe el video 404"):
        queries.reject_script(conn, 404)


# ---------------------------------------------------------------------------
# rejected_scripts / rewrite_script
# ---------------------------------------------------------------------------


def test_la_lista_de_rechazados_trae_los_mas_recientes_primero(
    conn: sqlite3.Connection,
):
    idea_vieja = _insertar_idea(conn, title="Idea vieja")
    idea_nueva = _insertar_idea(conn, title="Idea nueva")
    _insertar_video(
        conn, title="El primero en rechazarse", status="rejected",
        idea_id=idea_vieja, updated_at="2026-08-01 10:00:00",
    )
    _insertar_video(
        conn, title="El último en rechazarse", status="rejected",
        idea_id=idea_nueva, updated_at="2026-08-10 10:00:00",
    )

    rechazados = queries.rejected_scripts(conn)

    assert [r.title for r in rechazados] == [
        "El último en rechazarse", "El primero en rechazarse",
    ]


def test_un_rechazado_sin_idea_detras_no_aparece_en_la_lista(
    conn: sqlite3.Connection,
):
    # Sin idea_id no hay a qué idea reencolarle un guion nuevo: no hay ninguna
    # acción posible sobre esa fila, así que no tiene sentido ofrecerla.
    _insertar_video(conn, status="rejected", idea_id=None)

    assert queries.rejected_scripts(conn) == []


def test_un_guion_en_borrador_no_aparece_entre_los_rechazados(
    conn: sqlite3.Connection,
):
    idea_id = _insertar_idea(conn)
    _insertar_video(conn, status="script_draft", idea_id=idea_id)

    assert queries.rejected_scripts(conn) == []


def test_reescribir_encola_un_guion_nuevo_para_la_idea_del_video_rechazado(
    conn: sqlite3.Connection,
):
    idea_id = _insertar_idea(conn, title="Hábitos que no se pegan")
    video_id = _insertar_video(conn, status="rejected", idea_id=idea_id)

    job_id = queries.rewrite_script(conn, video_id)

    assert _jobs(conn) == [("write_script", {"idea_id": idea_id})]
    assert queue.claim_next(conn).id == job_id
    # El guion viejo se queda de historial: reescribir no lo toca.
    assert _fila_video(conn, video_id)["status"] == "rejected"


def test_reescribir_un_guion_que_no_esta_rechazado_da_bloqueado(
    conn: sqlite3.Connection,
):
    idea_id = _insertar_idea(conn)
    video_id = _insertar_video(conn, status="script_draft", idea_id=idea_id)

    with pytest.raises(queries.ScriptLocked, match="script_draft"):
        queries.rewrite_script(conn, video_id)
    assert _jobs(conn) == []


def test_reescribir_un_guion_rechazado_sin_idea_da_bloqueado(
    conn: sqlite3.Connection,
):
    video_id = _insertar_video(conn, status="rejected", idea_id=None)

    with pytest.raises(queries.ScriptLocked, match="no tiene una idea asociada"):
        queries.rewrite_script(conn, video_id)


def test_reescribir_un_video_que_no_existe_es_un_error_explicito(
    conn: sqlite3.Connection,
):
    with pytest.raises(queries.ScriptNotFound, match="no existe el video 404"):
        queries.rewrite_script(conn, 404)


def test_reescribir_dos_veces_no_encola_dos_guiones(conn: sqlite3.Connection):
    # Mismo criterio que aprobar una idea: dos clics no pueden pagar dos
    # generaciones completas de Gemini por el mismo guion.
    idea_id = _insertar_idea(conn)
    video_id = _insertar_video(conn, status="rejected", idea_id=idea_id)

    queries.rewrite_script(conn, video_id)
    with pytest.raises(queries.ScriptLocked, match="ya hay un guion en camino"):
        queries.rewrite_script(conn, video_id)

    assert _jobs(conn) == [("write_script", {"idea_id": idea_id})]


def test_reescribir_una_idea_que_ya_tiene_otro_guion_no_encola_nada(
    conn: sqlite3.Connection,
):
    # La reescritura anterior ya terminó: la idea tiene una fila viva y el job
    # que se encolase aquí no escribiría nada (el writer lo descarta con un
    # WARNING). Sin este corte, el humano lee "en reescritura" y no pasa nada
    # más. Sucede con la página sin refrescar y con el botón "atrás", que
    # siguen enseñando la fila descartada con su botón.
    idea_id = _insertar_idea(conn)
    rechazado = _insertar_video(conn, status="rejected", idea_id=idea_id)
    nuevo = _insertar_video(conn, status="script_draft", idea_id=idea_id)

    with pytest.raises(queries.ScriptLocked, match=f"video {nuevo}"):
        queries.rewrite_script(conn, rechazado)

    assert _jobs(conn) == []


def test_un_guion_ya_aprobado_tampoco_deja_reescribir_el_rechazado_anterior(
    conn: sqlite3.Connection,
):
    idea_id = _insertar_idea(conn)
    rechazado = _insertar_video(conn, status="rejected", idea_id=idea_id)
    _insertar_video(conn, status="script_approved", idea_id=idea_id)

    with pytest.raises(queries.ScriptLocked, match="script_approved"):
        queries.rewrite_script(conn, rechazado)

    assert _jobs(conn) == []


def test_dos_rechazados_de_la_misma_idea_solo_encolan_una_reescritura(
    conn: sqlite3.Connection,
):
    # Se rechazó un guion, se reescribió, y el segundo también se rechazó: la
    # idea tiene dos filas 'rejected' y ninguna viva. Reescribir vuelve a valer,
    # pero una sola vez.
    idea_id = _insertar_idea(conn)
    primero = _insertar_video(conn, status="rejected", idea_id=idea_id)
    segundo = _insertar_video(conn, status="rejected", idea_id=idea_id)

    queries.rewrite_script(conn, segundo)
    with pytest.raises(queries.ScriptLocked, match="ya hay un guion en camino"):
        queries.rewrite_script(conn, primero)

    assert _jobs(conn) == [("write_script", {"idea_id": idea_id})]


# ---------------------------------------------------------------------------
# retry_job
# ---------------------------------------------------------------------------


def _job_fallido(conn: sqlite3.Connection, *, payload: dict[str, Any] | None = None) -> int:
    """Un `write_script` que agotó sus intentos y quedó 'failed'."""
    job_id = queue.enqueue(
        "write_script", payload or {"idea_id": 1}, max_attempts=1, conn=conn
    )
    queue.claim_next(conn)
    queue.fail(job_id, "el modelo no respondió", conn=conn)
    return job_id


def test_reintentar_encola_un_job_nuevo_con_el_mismo_tipo_y_payload(
    conn: sqlite3.Connection,
):
    job_id = _job_fallido(conn, payload={"idea_id": 7})

    nuevo_id = queries.retry_job(conn, job_id)

    assert nuevo_id != job_id
    nuevo = conn.execute(
        "SELECT type, status, payload FROM jobs WHERE id = ?", (nuevo_id,)
    ).fetchone()
    assert nuevo["type"] == "write_script"
    assert nuevo["status"] == "pending"
    assert json.loads(nuevo["payload"]) == {"idea_id": 7}
    # El job viejo no se toca: sigue 'failed' con su motivo, como rastro.
    assert conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()["status"] == "failed"


def test_reintentar_un_job_que_no_existe_es_un_error_explicito(
    conn: sqlite3.Connection,
):
    with pytest.raises(queries.JobNotFound, match="no existe el job 404"):
        queries.retry_job(conn, 404)


def test_reintentar_un_job_pendiente_da_no_reintentable(conn: sqlite3.Connection):
    job_id = queue.enqueue("write_script", {"idea_id": 1}, conn=conn)

    with pytest.raises(queries.JobNotRetryable, match="solo se reintenta"):
        queries.retry_job(conn, job_id)


def test_reintentar_dos_veces_el_mismo_job_no_duplica_el_reintento(
    conn: sqlite3.Connection,
):
    # El primer reintento deja un job nuevo 'pending' con el mismo payload; el
    # segundo clic sobre el job viejo se encuentra ya con uno en curso.
    job_id = _job_fallido(conn, payload={"idea_id": 9})
    queries.retry_job(conn, job_id)

    with pytest.raises(queries.JobNotRetryable, match="ya hay un job"):
        queries.retry_job(conn, job_id)

    assert len(_jobs(conn)) == 2
