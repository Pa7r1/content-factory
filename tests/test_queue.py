"""Cola de jobs: encolar, reclamar, completar, fallar, reintentar y competir.

El test que de verdad justifica la implementación es
`test_dos_workers_nunca_procesan_el_mismo_job`: va contra un fichero SQLite real
porque cada conexión a `:memory:` es una base distinta y el test pasaría sin
haber probado nada.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from factory.core import db, queue
from factory.core.models import Job
from factory.core.queue import JobWorker


# ---------------------------------------------------------------------------
# Encolar
# ---------------------------------------------------------------------------


def test_encolar_devuelve_el_id_y_deja_el_job_pendiente(conn: sqlite3.Connection):
    job_id = queue.enqueue("research_daily", {"niche": "historias_epicas"})

    assert job_id > 0
    assert queue.pending_count() == 1
    fila = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert fila["status"] == "pending"
    assert fila["attempts"] == 0
    assert fila["run_after"] is None


def test_el_payload_sobrevive_al_viaje_con_tildes_y_emojis(conn: sqlite3.Connection):
    payload = {"niche": "historias_épicas", "nota": "traición ⚔️", "n": 3, "ok": True}

    job_id = queue.enqueue("research_daily", payload)

    assert queue.claim_next().payload == payload
    crudo = conn.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    assert "épicas" in crudo  # ensure_ascii=False: legible en la base


def test_un_job_sin_payload_llega_con_un_diccionario_vacio(conn: sqlite3.Connection):
    queue.enqueue("fetch_metrics")

    assert queue.claim_next().payload == {}


def test_los_valores_por_defecto_de_prioridad_y_reintentos_son_los_declarados(
    conn: sqlite3.Connection,
):
    queue.enqueue("fetch_metrics")

    job = queue.claim_next()

    assert job.priority == 0
    assert job.max_attempts == 3


# ---------------------------------------------------------------------------
# Reclamar
# ---------------------------------------------------------------------------


def test_reclamar_marca_el_job_como_running_y_cuenta_el_intento(
    conn: sqlite3.Connection,
):
    job_id = queue.enqueue("research_daily")

    job = queue.claim_next()

    assert job.id == job_id
    assert job.status == "running"
    assert job.attempts == 1
    fila = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert fila["started_at"] is not None
    assert queue.pending_count() == 0


def test_reclamar_de_una_cola_vacia_devuelve_none(conn: sqlite3.Connection):
    assert queue.claim_next() is None


def test_reclamar_no_devuelve_dos_veces_el_mismo_job(conn: sqlite3.Connection):
    queue.enqueue("research_daily")

    primero = queue.claim_next()
    segundo = queue.claim_next()

    assert primero is not None
    assert segundo is None


def test_la_prioridad_alta_se_reclama_antes(conn: sqlite3.Connection):
    queue.enqueue("baja", priority=0)
    alta = queue.enqueue("alta", priority=10)

    assert queue.claim_next().id == alta


def test_a_igual_prioridad_se_reclama_el_mas_antiguo(conn: sqlite3.Connection):
    primero = queue.enqueue("a")
    queue.enqueue("b")

    assert queue.claim_next().id == primero


def test_un_job_con_run_after_en_el_futuro_no_se_reclama(conn: sqlite3.Connection):
    job_id = queue.enqueue("research_daily")
    conn.execute(
        "UPDATE jobs SET run_after = datetime('now', '+3600 seconds') WHERE id = ?",
        (job_id,),
    )

    assert queue.claim_next() is None
    assert queue.pending_count() == 1


def test_un_job_con_run_after_ya_vencido_vuelve_a_estar_disponible(
    conn: sqlite3.Connection,
):
    job_id = queue.enqueue("research_daily")
    conn.execute(
        "UPDATE jobs SET run_after = datetime('now', '-1 seconds') WHERE id = ?",
        (job_id,),
    )

    assert queue.claim_next().id == job_id


# ---------------------------------------------------------------------------
# Completar y fallar
# ---------------------------------------------------------------------------


def test_completar_deja_el_job_hecho_y_limpia_el_error(conn: sqlite3.Connection):
    job_id = queue.enqueue("research_daily")
    queue.claim_next()
    queue.fail(job_id, "un fallo anterior")

    queue.complete(job_id)

    fila = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert fila["status"] == "done"
    assert fila["error"] is None
    assert fila["finished_at"] is not None


def test_fallar_con_intentos_de_sobra_devuelve_el_job_a_la_cola(
    conn: sqlite3.Connection,
):
    job_id = queue.enqueue("research_daily", max_attempts=3)
    queue.claim_next()

    estado = queue.fail(job_id, "SourceUnavailable: 503")

    assert estado == "pending"
    fila = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert fila["status"] == "pending"
    assert fila["error"] == "SourceUnavailable: 503"
    assert fila["run_after"] is not None


@pytest.mark.parametrize(
    ("intento", "espera_segundos"),
    [(0, 30), (1, 30), (2, 60), (3, 120), (4, 240)],
)
def test_el_backoff_entre_reintentos_es_exponencial(
    conn: sqlite3.Connection, intento, espera_segundos
):
    # `run_after` lo escribe SQLite con datetime('now'), no Python: freezegun no
    # llega ahí, así que el retraso se mide contra el mismo reloj que lo generó.
    job_id = queue.enqueue("research_daily", max_attempts=10)
    conn.execute("UPDATE jobs SET attempts = ? WHERE id = ?", (intento, job_id))
    antes = _epoca(conn, "now")

    queue.fail(job_id, "cayó")

    run_after = conn.execute(
        "SELECT run_after FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()["run_after"]
    retraso = _epoca(conn, run_after) - antes
    assert espera_segundos <= retraso <= espera_segundos + 2


def _epoca(conn: sqlite3.Connection, instante: str) -> int:
    """Un instante en el formato de SQLite, en segundos epoch, según SQLite."""
    return int(
        conn.execute("SELECT CAST(strftime('%s', ?) AS INTEGER)", (instante,)).fetchone()[0]
    )


def test_al_agotar_max_attempts_el_job_queda_fallido_para_siempre(
    conn: sqlite3.Connection,
):
    job_id = queue.enqueue("research_daily", max_attempts=3)

    estados = []
    for _ in range(3):
        queue.claim_next()
        estados.append(queue.fail(job_id, "sigue cayendo"))
        conn.execute("UPDATE jobs SET run_after = NULL WHERE id = ?", (job_id,))

    assert estados == ["pending", "pending", "failed"]
    assert conn.execute(
        "SELECT attempts, status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()["attempts"] == 3
    assert queue.claim_next() is None


def test_un_job_con_un_solo_intento_falla_a_la_primera(conn: sqlite3.Connection):
    job_id = queue.enqueue("research_daily", max_attempts=1)
    queue.claim_next()

    assert queue.fail(job_id, "cayó") == "failed"


def test_fallar_sin_reintento_no_devuelve_el_job_a_la_cola(conn: sqlite3.Connection):
    job_id = queue.enqueue("research_daily", max_attempts=10)
    queue.claim_next()

    estado = queue.fail(job_id, "handler no registrado", retry=False)

    assert estado == "failed"
    assert conn.execute(
        "SELECT finished_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()["finished_at"] is not None


def test_fallar_un_job_inexistente_es_un_error_explicito(conn: sqlite3.Connection):
    with pytest.raises(ValueError, match="Job 999 no existe"):
        queue.fail(999, "da igual")


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------


def test_list_jobs_devuelve_los_mas_recientes_primero(conn: sqlite3.Connection):
    primero = queue.enqueue("a")
    ultimo = queue.enqueue("b")

    jobs = queue.list_jobs()

    assert [j.id for j in jobs] == [ultimo, primero]


def test_list_jobs_filtra_por_estado(conn: sqlite3.Connection):
    hecho = queue.enqueue("a")
    queue.enqueue("b")
    queue.claim_next()
    queue.complete(hecho)

    assert [j.id for j in queue.list_jobs("done")] == [hecho]


def test_list_jobs_respeta_el_limite(conn: sqlite3.Connection):
    for _ in range(5):
        queue.enqueue("a")

    assert len(queue.list_jobs(limit=2)) == 2


def test_pending_count_solo_cuenta_los_pendientes(conn: sqlite3.Connection):
    queue.enqueue("a")
    queue.enqueue("b")
    queue.claim_next()

    assert queue.pending_count() == 1


# ---------------------------------------------------------------------------
# Marcas de tiempo
#
# El dashboard las necesita para distinguir un fallo de hoy de uno de hace tres
# días. Las escribe SQLite con datetime('now'), así que aquí se comprueba el
# formato y qué marca está puesta en cada estado, no el instante exacto.
# ---------------------------------------------------------------------------

FORMATO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def test_un_job_recien_encolado_tiene_created_at_y_ninguna_marca_de_ejecucion(
    conn: sqlite3.Connection,
):
    queue.enqueue("research_daily")

    job = queue.list_jobs()[0]

    assert FORMATO_UTC.match(job.created_at or ""), job.created_at
    assert job.started_at is None
    assert job.finished_at is None


def test_claim_next_devuelve_el_job_con_created_at_y_started_at_puestos(
    conn: sqlite3.Connection,
):
    queue.enqueue("research_daily")

    job = queue.claim_next()

    assert FORMATO_UTC.match(job.created_at or ""), job.created_at
    assert FORMATO_UTC.match(job.started_at or ""), job.started_at
    assert job.finished_at is None


def test_list_jobs_ve_el_started_at_del_job_en_curso(conn: sqlite3.Connection):
    queue.enqueue("research_daily")
    reclamado = queue.claim_next()

    listado = queue.list_jobs("running")[0]

    assert FORMATO_UTC.match(listado.started_at or ""), listado.started_at
    assert listado.started_at == reclamado.started_at
    assert listado.finished_at is None


def test_al_completar_el_job_queda_con_finished_at_sin_perder_las_otras_marcas(
    conn: sqlite3.Connection,
):
    job_id = queue.enqueue("research_daily")
    reclamado = queue.claim_next()

    queue.complete(job_id)

    job = queue.list_jobs("done")[0]
    assert job.created_at == reclamado.created_at
    assert job.started_at == reclamado.started_at
    assert FORMATO_UTC.match(job.finished_at or ""), job.finished_at
    assert job.finished_at >= job.started_at


def test_un_job_que_falla_sin_reintento_tambien_queda_con_finished_at(
    conn: sqlite3.Connection,
):
    job_id = queue.enqueue("research_daily")
    queue.claim_next()

    queue.fail(job_id, "handler no registrado", retry=False)

    job = queue.list_jobs("failed")[0]
    assert FORMATO_UTC.match(job.finished_at or ""), job.finished_at


def test_un_job_devuelto_a_la_cola_para_reintentar_sigue_sin_finished_at(
    conn: sqlite3.Connection,
):
    # Mientras queden intentos el job no ha terminado: ponerle finished_at haría
    # que el dashboard lo diese por cerrado con un reintento aún por delante.
    job_id = queue.enqueue("research_daily", max_attempts=3)
    queue.claim_next()

    queue.fail(job_id, "timeout de la fuente")

    job = queue.list_jobs("pending")[0]
    assert job.finished_at is None


# ---------------------------------------------------------------------------
# JobWorker
# ---------------------------------------------------------------------------


def test_el_worker_ejecuta_el_handler_del_tipo_y_completa_el_job(
    conn: sqlite3.Connection,
):
    recibidos: list[Job] = []
    worker = JobWorker()
    worker.register("research_daily", recibidos.append)
    job_id = queue.enqueue("research_daily", {"niche": "historias_epicas"})

    worker._process_one()

    assert [j.id for j in recibidos] == [job_id]
    assert recibidos[0].payload == {"niche": "historias_epicas"}
    assert conn.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()["status"] == "done"


def test_el_worker_despacha_cada_job_al_handler_de_su_tipo(conn: sqlite3.Connection):
    llamados: list[str] = []
    worker = JobWorker()
    worker.register("research_daily", lambda job: llamados.append("research"))
    worker.register("fetch_metrics", lambda job: llamados.append("metrics"))
    queue.enqueue("fetch_metrics")
    queue.enqueue("research_daily", priority=5)

    worker._process_one()
    worker._process_one()

    assert llamados == ["research", "metrics"]


def test_un_handler_que_revienta_devuelve_el_job_a_la_cola_con_el_error(
    conn: sqlite3.Connection,
):
    worker = JobWorker()

    def revienta(job: Job) -> None:
        raise RuntimeError("la fuente no responde")

    worker.register("research_daily", revienta)
    job_id = queue.enqueue("research_daily", max_attempts=3)

    worker._process_one()

    fila = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert fila["status"] == "pending"
    assert fila["error"] == "RuntimeError: la fuente no responde"
    assert fila["attempts"] == 1


# StopRequested vale una cosa u otra según el worker esté parando o no, y la
# diferencia es la cota de ejecuciones del job: devolver el turno no gasta
# intento y deja `run_after` a NULL, así que fuera del apagado —con el bucle sin
# ninguna intención de salir— el job se reclamaría otra vez en la vuelta
# siguiente, para siempre y sin nada que lo corte.


def test_un_handler_que_se_retira_por_la_parada_devuelve_el_job_sin_gastar_intento(
    conn: sqlite3.Connection,
):
    worker = JobWorker()

    def se_retira(job: Job) -> None:
        raise queue.StopRequested("parada pedida antes de llamar al modelo")

    worker.register("research_daily", se_retira)
    job_id = queue.enqueue("research_daily", max_attempts=3)
    worker.stop()  # sin hilo arrancado, solo pide la parada

    worker._process_one()

    fila = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert fila["status"] == "pending"
    assert fila["attempts"] == 0
    assert fila["run_after"] is None  # nada que esperar: no ha fallado nada
    assert "parada pedida" in fila["error"]


def test_un_handler_que_se_retira_sin_parada_en_curso_gasta_intento_y_espera(
    conn: sqlite3.Connection,
):
    # El worker NO está parando: solo el handler cree que sí. Sin esta
    # distinción el job volvería gratis a la cola una y otra vez.
    worker = JobWorker()

    def se_retira(job: Job) -> None:
        raise queue.StopRequested("me retiro por mi cuenta")

    worker.register("research_daily", se_retira)
    job_id = queue.enqueue("research_daily", max_attempts=3)
    antes = _epoca(conn, "now")

    worker._process_one()

    fila = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert fila["status"] == "pending"  # le quedan 2 de 3 intentos
    assert fila["attempts"] == 1
    assert "StopRequested sin parada en curso" in fila["error"]
    assert 30 <= _epoca(conn, fila["run_after"]) - antes <= 32


def test_un_job_sin_handler_falla_sin_reintento(conn: sqlite3.Connection):
    # Reintentarlo no haría aparecer el handler: es gastar la cola en balde.
    worker = JobWorker()
    job_id = queue.enqueue("tipo_que_nadie_registro", max_attempts=5)

    worker._process_one()

    fila = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert fila["status"] == "failed"
    assert fila["attempts"] == 1
    assert "handler no registrado" in fila["error"]
    assert fila["run_after"] is None


def test_procesar_con_la_cola_vacia_devuelve_none_sin_tocar_nada(
    conn: sqlite3.Connection,
):
    worker = JobWorker()

    assert worker._process_one() is None


def test_registrar_dos_handlers_para_el_mismo_tipo_es_un_bug(conn: sqlite3.Connection):
    worker = JobWorker()
    worker.register("research_daily", lambda job: None)

    with pytest.raises(ValueError, match="Ya hay un handler registrado"):
        worker.register("research_daily", lambda job: None)


def test_el_worker_en_hilo_vacia_la_cola_y_para_limpiamente(conn: sqlite3.Connection):
    procesados: list[int] = []
    terminado = threading.Event()
    worker = JobWorker(poll_interval=0.01)

    def handler(job: Job) -> None:
        procesados.append(job.id)
        if len(procesados) == 3:
            terminado.set()

    worker.register("research_daily", handler)
    esperados = [queue.enqueue("research_daily") for _ in range(3)]

    worker.start()
    assert terminado.wait(timeout=10), "el worker no procesó los 3 jobs a tiempo"
    worker.stop(timeout=5)

    assert sorted(procesados) == sorted(esperados)
    assert queue.pending_count() == 0


# ---------------------------------------------------------------------------
# El hilo del worker no puede morir
# ---------------------------------------------------------------------------
#
# Si muere, la cola se queda 'pending' para siempre y en una máquina desatendida
# nadie se entera hasta que faltan tres días de videos. Los tres tests de abajo
# revientan la base en los tres únicos sitios donde el worker la toca fuera del
# handler: reclamar, completar y registrar el fallo.


def _que_revienta_la_primera_vez(real, llamadas: list[Any]):
    """Envuelve una función para que falle en su primera llamada y luego funcione."""

    def envuelta(*args, **kwargs):
        llamadas.append(args)
        if len(llamadas) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    return envuelta


def _procesar_dos_jobs(worker: JobWorker) -> tuple[list[int], threading.Event]:
    """Registra un handler que apunta los jobs y avisa al llegar al segundo."""
    procesados: list[int] = []
    terminado = threading.Event()

    def handler(job: Job) -> None:
        procesados.append(job.id)
        if len(procesados) == 2:
            terminado.set()

    worker.register("research_daily", handler)
    return procesados, terminado


def test_un_fallo_al_marcar_completado_no_mata_el_hilo_del_worker(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    llamadas: list[Any] = []
    monkeypatch.setattr(
        queue, "complete", _que_revienta_la_primera_vez(queue.complete, llamadas)
    )
    worker = JobWorker(poll_interval=0.01)
    procesados, terminado = _procesar_dos_jobs(worker)
    primero = queue.enqueue("research_daily")
    segundo = queue.enqueue("research_daily")

    worker.start()
    ok = terminado.wait(timeout=10)
    worker.stop(timeout=5)

    assert ok, "el worker no procesó el segundo job: su hilo murió al fallar complete()"
    assert procesados == [primero, segundo]
    assert _fila(conn, segundo)["status"] == "done"
    # El primero se queda 'running': nadie pudo escribir su final. Lo rescata
    # requeue_stale_running() en el siguiente arranque, que para eso está.
    assert _fila(conn, primero)["status"] == "running"


def test_un_fallo_al_registrar_el_error_tampoco_mata_el_hilo(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    llamadas: list[Any] = []
    monkeypatch.setattr(queue, "fail", _que_revienta_la_primera_vez(queue.fail, llamadas))
    worker = JobWorker(poll_interval=0.01)
    procesados: list[int] = []
    terminado = threading.Event()

    def handler(job: Job) -> None:
        procesados.append(job.id)
        if len(procesados) == 2:
            terminado.set()
        raise RuntimeError("la fuente no responde")

    worker.register("research_daily", handler)
    primero = queue.enqueue("research_daily")
    segundo = queue.enqueue("research_daily")

    worker.start()
    ok = terminado.wait(timeout=10)
    worker.stop(timeout=5)

    assert ok, "el worker no procesó el segundo job: su hilo murió al fallar fail()"
    assert procesados == [primero, segundo]
    assert _fila(conn, segundo)["status"] == "pending"  # el segundo sí se registró
    assert _fila(conn, segundo)["error"] == "RuntimeError: la fuente no responde"


def test_un_fallo_al_reclamar_no_mata_el_hilo_del_worker(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    llamadas: list[Any] = []
    monkeypatch.setattr(
        queue, "claim_next", _que_revienta_la_primera_vez(queue.claim_next, llamadas)
    )
    worker = JobWorker(poll_interval=0.01)
    procesado = threading.Event()
    worker.register("research_daily", lambda job: procesado.set())
    queue.enqueue("research_daily")

    worker.start()
    ok = procesado.wait(timeout=10)
    worker.stop(timeout=5)

    assert ok, "el worker no se recuperó de un error reclamando"


# ---------------------------------------------------------------------------
# requeue_stale_running: los jobs que dejó un proceso muerto
# ---------------------------------------------------------------------------


def _dejar_running(conn: sqlite3.Connection, job_id: int, *, attempts: int) -> None:
    """Deja un job como lo dejaría un corte de luz a mitad de ejecución."""
    conn.execute(
        "UPDATE jobs SET status = 'running', attempts = ?,"
        " started_at = datetime('now'), run_after = datetime('now', '+60 seconds')"
        " WHERE id = ?",
        (attempts, job_id),
    )


def test_un_job_interrumpido_vuelve_a_la_cola_sin_gastar_un_intento_extra(
    conn: sqlite3.Connection,
):
    # El intento ya lo contó el claim que se cortó: cobrárselo otra vez lo
    # dejaría fallido antes de tiempo por un apagón que no es culpa suya.
    job_id = queue.enqueue("research_daily", max_attempts=3)
    _dejar_running(conn, job_id, attempts=1)

    reencolados, abandonados = queue.requeue_stale_running(conn)

    fila = _fila(conn, job_id)
    assert (reencolados, abandonados) == ([job_id], [])
    assert fila["status"] == "pending"
    assert fila["attempts"] == 1
    assert fila["run_after"] is None      # reclamable ya, sin esperar al backoff
    assert fila["started_at"] is None
    assert "el proceso murió" in fila["error"]


def test_un_job_interrumpido_sin_intentos_restantes_se_abandona(
    conn: sqlite3.Connection,
):
    # Un job capaz de tumbar el proceso volvería a tumbarlo en cada arranque:
    # reencolarlo sería un bucle de crash, no una recuperación.
    job_id = queue.enqueue("research_daily", max_attempts=3)
    _dejar_running(conn, job_id, attempts=3)

    reencolados, abandonados = queue.requeue_stale_running(conn)

    fila = _fila(conn, job_id)
    assert (reencolados, abandonados) == ([], [job_id])
    assert fila["status"] == "failed"
    assert fila["attempts"] == 3
    assert fila["finished_at"] is not None
    assert "no quedaban intentos" in fila["error"]


def test_el_rescate_separa_los_reencolables_de_los_abandonados(
    conn: sqlite3.Connection,
):
    salvable = queue.enqueue("research_daily", max_attempts=3)
    agotado = queue.enqueue("research_daily", max_attempts=1)
    _dejar_running(conn, salvable, attempts=2)
    _dejar_running(conn, agotado, attempts=1)

    assert queue.requeue_stale_running(conn) == ([salvable], [agotado])


@pytest.mark.parametrize("estado", ["pending", "done", "failed"])
def test_el_rescate_no_toca_los_jobs_que_no_estaban_corriendo(
    conn: sqlite3.Connection, estado: str
):
    job_id = queue.enqueue("research_daily")
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (estado, job_id))

    assert queue.requeue_stale_running(conn) == ([], [])
    assert _fila(conn, job_id)["status"] == estado


def test_sin_jobs_colgados_el_rescate_no_hace_nada(conn: sqlite3.Connection):
    assert queue.requeue_stale_running(conn) == ([], [])


def test_al_arrancar_el_worker_rescata_y_termina_el_job_del_proceso_anterior(
    conn: sqlite3.Connection,
):
    job_id = queue.enqueue("research_daily", max_attempts=3)
    _dejar_running(conn, job_id, attempts=1)
    worker = JobWorker(poll_interval=0.01)
    procesado = threading.Event()
    worker.register("research_daily", lambda job: procesado.set())

    worker.start()
    ok = procesado.wait(timeout=10)
    worker.stop(timeout=5)

    fila = _fila(conn, job_id)
    assert ok, "el job huérfano no se volvió a ejecutar al arrancar el worker"
    assert fila["status"] == "done"
    assert fila["attempts"] == 2  # el rescate no cobró intento; el nuevo claim sí


def test_si_el_rescate_falla_el_worker_arranca_igual(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    def revienta(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(queue, "requeue_stale_running", revienta)
    worker = JobWorker(poll_interval=0.01)
    procesado = threading.Event()
    worker.register("research_daily", lambda job: procesado.set())
    queue.enqueue("research_daily")

    worker.start()
    ok = procesado.wait(timeout=10)
    worker.stop(timeout=5)

    assert ok, "un rescate fallido impidió que el worker arrancase"


def _fila(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


# ---------------------------------------------------------------------------
# Concurrencia real, sobre fichero
# ---------------------------------------------------------------------------


@pytest.mark.concurrencia
def test_dos_workers_nunca_procesan_el_mismo_job(conn: sqlite3.Connection, db_path: Path):
    total = 60
    hilos = 4
    encolados = [queue.enqueue("research_daily") for _ in range(total)]
    arranque = threading.Barrier(hilos)
    reclamados: list[list[int]] = [[] for _ in range(hilos)]

    def reclamar_todo(indice: int) -> None:
        # Conexión propia del hilo: get_conn() apunta a la MISMA base en disco.
        db.get_conn()
        arranque.wait()
        try:
            while True:
                job = queue.claim_next()
                if job is None:
                    return
                reclamados[indice].append(job.id)
                time.sleep(0.001)  # ventana real para que otro hilo intente pisarlo
        finally:
            db.close_conn()

    trabajadores = [
        threading.Thread(target=reclamar_todo, args=(i,)) for i in range(hilos)
    ]
    for hilo in trabajadores:
        hilo.start()
    for hilo in trabajadores:
        hilo.join(timeout=60)

    todos = [job_id for lote in reclamados for job_id in lote]
    assert sorted(todos) == sorted(encolados), "algún job se perdió o se duplicó"
    assert len(set(todos)) == total
    assert sum(1 for lote in reclamados if lote) > 1, "solo trabajó un hilo: sin carrera"


@pytest.mark.concurrencia
def test_dos_hilos_reservando_cuota_no_se_pasan_del_tope(
    conn: sqlite3.Connection, db_path: Path
):
    from factory.core import quota
    from factory.core.quota import QuotaExceeded

    presupuesto = 1_000          # tope efectivo: 800
    concedidas: list[int] = []
    candado = threading.Lock()
    arranque = threading.Barrier(8)

    def intentar() -> None:
        db.get_conn()
        arranque.wait()
        try:
            for _ in range(20):
                try:
                    quota.reserve("youtube", 10, presupuesto)
                except QuotaExceeded:
                    continue
                with candado:
                    concedidas.append(10)
        finally:
            db.close_conn()

    hilos = [threading.Thread(target=intentar) for _ in range(8)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=60)

    assert sum(concedidas) == 800
    assert quota.usage_today("youtube") == 800
