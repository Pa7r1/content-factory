"""Rutas del dashboard, contra la app montada de verdad con TestClient.

No se arranca la aplicación real (`app.py`): se monta un FastAPI con el mismo
router y el mismo directorio estático, y `db.get_conn()` aterriza en la base de
`tmp_path` gracias al fixture `db_path`. Así los tests no tocan `data/factory.db`
ni levantan el scheduler ni el worker.

Los endpoints son `def`, así que Starlette los ejecuta en el threadpool: cada
petición abre su propia conexión a la MISMA base de fichero. Eso es exactamente
lo que pasa en producción, y por eso las aserciones se hacen desde la conexión
del hilo del test leyendo lo que la petición dejó escrito.

Nada de seguir los redirects por defecto: el 303 y su Location son parte del
comportamiento que se está probando (POST/Redirect/GET sin una línea de JS).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from factory.core import queue
from factory.web.server import STATIC_DIR, router

SETTINGS = {
    "niches": {
        "crecimiento_personal": {"name": "Crecimiento personal", "keywords": ["hábitos"]},
        "historias_epicas": {"name": "Historias épicas", "keywords": ["leyendas"]},
    },
    "quotas": {"youtube": {"daily_budget": 4000}},
    "schedule": {"research_daily": "07:30", "fetch_metrics": "09:00"},
}


@pytest.fixture
def app(conn: sqlite3.Connection) -> FastAPI:
    """La app del dashboard: mismo router y mismos estáticos que `app.py`.

    Sin lifespan a propósito: ni scheduler ni worker. El fixture `conn` deja la
    base de `tmp_path` migrada y puesta como base por defecto del proceso.
    """
    aplicacion = FastAPI()
    aplicacion.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    aplicacion.include_router(router)
    return aplicacion


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, follow_redirects=False) as cliente:
        yield cliente


@pytest.fixture
def config_de_prueba(settings_falsas) -> None:
    settings_falsas(SETTINGS)


def _insertar_idea(
    conn: sqlite3.Connection,
    *,
    title: str = "Una idea",
    score: float = 50.0,
    status: str = "new",
    score_details: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO ideas (title, niche, keyword, source, score, status, score_details)
        VALUES (?, 'pruebas', 'hábitos', 'youtube', ?, ?, ?)
        """,
        (title, score, status, json.dumps(score_details or {}, ensure_ascii=False)),
    )
    return int(cur.lastrowid)


def _estado(conn: sqlite3.Connection, idea_id: int) -> str:
    return conn.execute(
        "SELECT status FROM ideas WHERE id = ?", (idea_id,)
    ).fetchone()["status"]


def _mensaje_del_redirect(respuesta) -> str:
    """El texto del aviso que viaja en el `Location` del 303."""
    location = respuesta.headers["location"]
    return unquote(location.split("mensaje=", 1)[1])


# ---------------------------------------------------------------------------
# El router expone lo que el dashboard necesita
# ---------------------------------------------------------------------------


def test_el_router_expone_las_dos_vistas_y_las_tres_acciones():
    rutas = {(ruta.path, tuple(sorted(ruta.methods))) for ruta in router.routes}

    assert rutas == {
        ("/", ("GET",)),
        ("/system", ("GET",)),
        ("/ideas/{idea_id}/approve", ("POST",)),
        ("/ideas/{idea_id}/reject", ("POST",)),
        ("/research/run", ("POST",)),
    }


# ---------------------------------------------------------------------------
# Vista de ideas
# ---------------------------------------------------------------------------


def test_la_vista_de_ideas_lista_las_pendientes_de_mayor_a_menor_score(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    _insertar_idea(conn, title="La floja", score=12.0)
    _insertar_idea(conn, title="La buena", score=88.0)

    html = client.get("/").text

    assert html.index("La buena") < html.index("La floja")


def test_la_vista_de_ideas_responde_html_completo(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    respuesta = client.get("/")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/html")
    assert "<h1 class=\"titulo-vista\">Ideas de hoy</h1>" in respuesta.text


@pytest.mark.parametrize("estado", ["approved", "rejected", "used"])
def test_una_idea_ya_decidida_no_aparece_en_la_vista(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba, estado: str
):
    _insertar_idea(conn, title="Idea ya decidida", status=estado)

    assert "Idea ya decidida" not in client.get("/").text


def test_la_cabecera_resume_cuantas_ideas_hay_en_cada_estado(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    _insertar_idea(conn, title="a", status="new")
    _insertar_idea(conn, title="b", status="new")
    _insertar_idea(conn, title="c", status="approved")

    html = client.get("/").text

    assert "2 sin decidir" in html
    assert "1 aprobada" in html


def test_sin_ideas_la_vista_lo_dice_en_vez_de_quedarse_en_blanco(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    html = client.get("/").text

    assert "Todavía no hay nada que decidir" in html
    assert "La base todavía está vacía." in html


def test_el_desglose_muestra_el_motivo_de_la_senal_que_falto(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    _insertar_idea(
        conn,
        score_details={
            "components": {"demand": 0.8},
            "weights_used": {"demand": 0.44},
            "missing_signals": ["reddit"],
            "missing_reasons": {"reddit": "SourceUnavailable: ninguno trajo posts"},
        },
    )

    html = client.get("/").text

    assert "SourceUnavailable: ninguno trajo posts" in html
    assert 'class="ausencia-senal">reddit<' in html
    assert 'class="ausencia-fuente">(reddit)<' in html


def test_un_titulo_con_tilde_y_emoji_llega_intacto_a_la_pagina(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    _insertar_idea(conn, title="Por qué fracasan tus hábitos 🔥")

    assert "Por qué fracasan tus hábitos 🔥" in client.get("/").text


def test_un_titulo_con_html_se_escapa_en_vez_de_inyectarse(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    _insertar_idea(conn, title="<script>alert(1)</script>")

    html = client.get("/").text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_el_mensaje_de_la_url_se_pinta_como_aviso(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    html = client.get("/", params={"mensaje": "Idea aprobada: Hábitos"}).text

    assert 'class="aviso" role="status"' in html
    assert "Idea aprobada: Hábitos" in html


# ---------------------------------------------------------------------------
# Aprobar y descartar
# ---------------------------------------------------------------------------


def test_descartar_una_idea_la_deja_descartada_y_redirige_al_ranking(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba,
):
    idea_id = _insertar_idea(conn, title="Hábitos de gente disciplinada")

    respuesta = client.post(f"/ideas/{idea_id}/reject")

    assert respuesta.status_code == 303
    assert respuesta.headers["location"].startswith("/?mensaje=")
    assert _mensaje_del_redirect(respuesta) == (
        "Idea descartada: Hábitos de gente disciplinada"
    )
    assert _estado(conn, idea_id) == "rejected"


def test_aprobar_una_idea_la_marca_usada_y_encola_su_guion(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba,
):
    idea_id = _insertar_idea(conn, title="Hábitos de gente disciplinada")

    respuesta = client.post(f"/ideas/{idea_id}/approve")

    encolados = queue.unfinished_by_type("write_script", conn)
    assert respuesta.status_code == 303
    assert respuesta.headers["location"].startswith("/?mensaje=")
    assert [job.payload for job in encolados] == [{"idea_id": idea_id}]
    assert _mensaje_del_redirect(respuesta) == (
        "Idea aprobada: Hábitos de gente disciplinada"
        f" - guion en cola (job {encolados[0].id})"
    )
    assert _estado(conn, idea_id) == "used"


def test_dos_clics_en_aprobar_no_encolan_dos_guiones(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba,
):
    # El segundo clic llega con la idea ya en 'used': 409 y ni un job más. Cada
    # guion de más es una generación completa de Gemini pagada dos veces.
    idea_id = _insertar_idea(conn, title="Leyendas medievales")

    primera = client.post(f"/ideas/{idea_id}/approve")
    segunda = client.post(f"/ideas/{idea_id}/approve")

    assert primera.status_code == 303
    assert segunda.status_code == 409
    assert "ya no se cambia" in segunda.json()["detail"]
    assert len(queue.unfinished_by_type("write_script", conn)) == 1


@pytest.mark.parametrize("accion", ["approve", "reject"])
def test_la_idea_decidida_desaparece_del_ranking_y_el_aviso_se_ve_al_volver(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba, accion: str
):
    # El ciclo completo del formulario sin JavaScript: POST → 303 → GET.
    idea_id = _insertar_idea(conn, title="Leyendas medievales")

    respuesta = client.post(f"/ideas/{idea_id}/{accion}", follow_redirects=True)

    assert respuesta.status_code == 200
    assert 'class="aviso"' in respuesta.text
    assert "Leyendas medievales" in respuesta.text.split('class="aviso"', 1)[1].split("</p>", 1)[0]
    assert f'id="idea-{idea_id}"' not in respuesta.text


def test_el_aviso_conserva_tildes_y_emojis_al_pasar_por_la_url(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    idea_id = _insertar_idea(conn, title="Por qué fracasan tus hábitos 🔥")

    respuesta = client.post(f"/ideas/{idea_id}/approve", follow_redirects=True)

    aviso = respuesta.text.split('class="aviso"', 1)[1].split("</p>", 1)[0]
    assert "Idea aprobada: Por qué fracasan tus hábitos 🔥" in aviso


@pytest.mark.parametrize("accion", ["approve", "reject"])
def test_decidir_una_idea_que_no_existe_da_404(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba, accion: str
):
    respuesta = client.post(f"/ideas/9999/{accion}")

    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == "no existe la idea 9999"


@pytest.mark.parametrize("accion", ["approve", "reject"])
def test_decidir_una_idea_ya_usada_da_409_y_la_deja_como_estaba(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba, accion: str
):
    idea_id = _insertar_idea(conn, title="Ya convertida en video", status="used")

    respuesta = client.post(f"/ideas/{idea_id}/{accion}")

    assert respuesta.status_code == 409
    assert "ya no se cambia" in respuesta.json()["detail"]
    assert _estado(conn, idea_id) == "used"


@pytest.mark.parametrize("accion", ["approve", "reject"])
def test_un_get_no_decide_nada(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba, accion: str
):
    # Un prefetch del navegador o un rastreador no pueden aprobar ideas.
    idea_id = _insertar_idea(conn)

    respuesta = client.get(f"/ideas/{idea_id}/{accion}")

    assert respuesta.status_code == 405
    assert _estado(conn, idea_id) == "new"


@pytest.mark.concurrencia
def test_seis_aprobaciones_simultaneas_encolan_un_solo_guion(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba,
):
    # El dedupe no puede vivir en un SELECT previo: seis peticiones en vuelo lo
    # pasarían las seis y encolarían seis guiones. Lo decide el UPDATE dentro
    # del BEGIN IMMEDIATE, así que solo una gana y las otras cinco ven 'used'.
    idea_id = _insertar_idea(conn, title="Leyendas medievales")
    arranque = threading.Barrier(6)
    codigos: list[int] = []

    def clic() -> None:
        # Una petición de calentamiento antes de la barrera: así el hilo del
        # threadpool ya tiene su conexión abierta y las seis aprobaciones caen
        # de verdad a la vez, en vez de serializarse creando conexiones.
        client.get("/")
        arranque.wait()
        codigos.append(client.post(f"/ideas/{idea_id}/approve").status_code)

    hilos = [threading.Thread(target=clic) for _ in range(6)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=30)

    assert sorted(codigos) == [303, 409, 409, 409, 409, 409]
    assert [job.payload for job in queue.unfinished_by_type("write_script", conn)] == [
        {"idea_id": idea_id}
    ]
    assert _estado(conn, idea_id) == "used"


def test_un_id_que_no_es_un_numero_no_llega_a_la_base(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    respuesta = client.post("/ideas/borra-todo/approve")

    assert respuesta.status_code == 422


# ---------------------------------------------------------------------------
# Vista de sistema
# ---------------------------------------------------------------------------


def test_la_vista_de_sistema_muestra_los_jobs_recientes_y_los_pendientes(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    queue.enqueue("research_daily", {"niche": "historias_epicas"}, conn=conn)

    html = client.get("/system").text

    assert "research_daily" in html
    assert "historias_epicas" in html
    assert "1 pendiente(s) en la cola." in html


def test_la_vista_de_sistema_muestra_la_cuota_de_hoy(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    conn.execute(
        "INSERT INTO api_usage (api, day, units)"
        " VALUES ('youtube', strftime('%Y-%m-%d', 'now'), 1600)"
    )

    html = client.get("/system").text

    assert "youtube" in html
    assert "1600" in html
    assert "50.0%" in html


def test_sin_jobs_la_tabla_lo_dice(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    assert "La cola todavía no ha tenido trabajo." in client.get("/system").text


def test_la_vista_de_sistema_no_pinta_mas_jobs_de_los_que_caben(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    from factory.web import server

    for _ in range(server.RECENT_JOBS_LIMIT + 5):
        queue.enqueue("research_daily", conn=conn)

    html = client.get("/system").text

    assert html.count('class="job-fila') == server.RECENT_JOBS_LIMIT


def test_sin_scheduler_la_proxima_pasada_se_declara_sin_programar(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    # Es el caso de un arranque sin lifespan: mentir con una fecha sería peor.
    html = client.get("/system").text

    assert "sin programar (el scheduler está parado)" in html


def test_con_el_scheduler_en_marcha_se_anuncia_la_proxima_pasada(
    client: TestClient, app: FastAPI, conn: sqlite3.Connection, config_de_prueba
):
    from factory.core.scheduler import RESEARCH_JOB_ID

    class JobFalso:
        next_run_time = datetime(2026, 8, 7, 7, 30)

    class SchedulerFalso:
        def get_job(self, job_id: str):
            return JobFalso() if job_id == RESEARCH_JOB_ID else None

    app.state.scheduler = SchedulerFalso()

    html = client.get("/system").text

    assert "07/08 a las 07:30" in html


def test_si_el_scheduler_no_tiene_ese_job_tampoco_se_inventa_una_fecha(
    client: TestClient, app: FastAPI, conn: sqlite3.Connection, config_de_prueba
):
    class SchedulerSinJobs:
        def get_job(self, job_id: str):
            return None

    app.state.scheduler = SchedulerSinJobs()

    assert "sin programar" in client.get("/system").text


def test_el_mensaje_de_la_url_se_pinta_tambien_en_sistema(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    html = client.get("/system", params={"mensaje": "2 job(s) encolados"}).text

    assert "2 job(s) encolados" in html


# ---------------------------------------------------------------------------
# Investigar ahora (con dedupe)
# ---------------------------------------------------------------------------


def test_investigar_ahora_encola_un_job_por_nicho_y_lo_cuenta_en_el_aviso(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    respuesta = client.post("/research/run")

    encolados = queue.unfinished_by_type("research_daily", conn)
    assert respuesta.status_code == 303
    assert respuesta.headers["location"].startswith("/system?mensaje=")
    assert [job.payload["niche"] for job in encolados] == [
        "crecimiento_personal", "historias_epicas",
    ]
    mensaje = _mensaje_del_redirect(respuesta)
    assert mensaje == "2 job(s) de investigación encolados: %s" % ", ".join(
        str(job.id) for job in encolados
    )


def test_dos_clics_seguidos_no_encolan_la_investigacion_dos_veces(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    # Una pasada cuesta ~1900 unidades de cuota de YouTube sobre un corte de
    # 3200: el segundo clic reventaría el presupuesto del día.
    primera = client.post("/research/run")
    segunda = client.post("/research/run")

    assert len(queue.unfinished_by_type("research_daily", conn)) == 2
    assert "2 job(s) de investigación encolados" in _mensaje_del_redirect(primera)
    assert _mensaje_del_redirect(segunda) == (
        "ya había investigación en cola para: crecimiento_personal, historias_epicas"
    )


def test_el_dedupe_tambien_cuenta_la_investigacion_que_ya_esta_corriendo(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    queue.enqueue("research_daily", {"niche": "crecimiento_personal"}, conn=conn)
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = 1")

    respuesta = client.post("/research/run")

    encolados = queue.unfinished_by_type("research_daily", conn)
    assert [job.payload["niche"] for job in encolados] == [
        "crecimiento_personal", "historias_epicas",
    ]
    assert _mensaje_del_redirect(respuesta) == (
        "1 job(s) de investigación encolados: 2"
        " · ya había investigación en cola para: crecimiento_personal"
    )


def test_una_investigacion_ya_terminada_no_bloquea_la_siguiente(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    queue.enqueue("research_daily", {"niche": "crecimiento_personal"}, conn=conn)
    conn.execute("UPDATE jobs SET status = 'done' WHERE id = 1")

    client.post("/research/run")

    pendientes = queue.unfinished_by_type("research_daily", conn)
    assert [job.payload["niche"] for job in pendientes] == [
        "crecimiento_personal", "historias_epicas",
    ]


def test_sin_nichos_configurados_no_se_encola_nada_y_se_avisa(
    client: TestClient, conn: sqlite3.Connection, settings_falsas
):
    settings_falsas({**SETTINGS, "niches": {}})

    respuesta = client.post("/research/run")

    assert _mensaje_del_redirect(respuesta) == "no hay nichos configurados que investigar"
    assert queue.pending_count(conn) == 0


def test_el_ciclo_completo_del_boton_devuelve_a_sistema_con_el_aviso(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    respuesta = client.post("/research/run", follow_redirects=True)

    assert respuesta.status_code == 200
    assert "Sala de máquinas" in respuesta.text
    assert "job(s) de investigación encolados" in respuesta.text


@pytest.mark.concurrencia
def test_un_doble_clic_simultaneo_no_encola_la_investigacion_dos_veces(
    client: TestClient, conn: sqlite3.Connection, config_de_prueba
):
    arranque = threading.Barrier(2)

    def clic() -> None:
        arranque.wait()
        client.post("/research/run")

    hilos = [threading.Thread(target=clic) for _ in range(2)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=30)

    encolados = queue.unfinished_by_type("research_daily", conn)
    assert sorted(job.payload["niche"] for job in encolados) == [
        "crecimiento_personal", "historias_epicas",
    ]
