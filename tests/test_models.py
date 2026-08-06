"""Máquina de estados de `videos`: toda transición ilegal probada como rechazada.

El caso que justifica este fichero entero es `published → script_draft`: si esa
transición pasa, un vídeo ya publicado vuelve a producción y se republica solo.
Aquí se enumeran las 121 combinaciones posibles y se comprueba una a una.
"""

from __future__ import annotations

import itertools
import sqlite3

import pytest

from factory.core.models import (
    IDEA_STATUSES,
    JOB_STATUSES,
    VIDEO_STATUSES,
    VIDEO_TRANSITIONS,
    IllegalTransition,
    assert_transition,
    can_transition,
)

LEGALES = sorted(
    (origen, destino)
    for origen, destinos in VIDEO_TRANSITIONS.items()
    for destino in destinos
)
ILEGALES = sorted(
    set(itertools.product(sorted(VIDEO_STATUSES), repeat=2)) - set(LEGALES)
)


# ---------------------------------------------------------------------------
# Transiciones
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("origen", "destino"), LEGALES, ids=lambda v: v)
def test_las_transiciones_legales_se_aceptan(origen, destino):
    assert can_transition(origen, destino) is True
    assert_transition(origen, destino)  # no debe lanzar


@pytest.mark.parametrize(("origen", "destino"), ILEGALES, ids=lambda v: v)
def test_toda_transicion_no_declarada_se_rechaza(origen, destino):
    assert can_transition(origen, destino) is False
    with pytest.raises(IllegalTransition):
        assert_transition(origen, destino)


def test_hay_exactamente_diecisiete_transiciones_legales_de_ciento_veintiuna():
    # Cerrojo contra ampliaciones accidentales de la máquina de estados: si
    # alguien añade un camino, este test obliga a declararlo aquí a conciencia.
    assert len(LEGALES) == 17
    assert len(ILEGALES) == 121 - 17
    assert len(VIDEO_STATUSES) == 11


def test_un_video_publicado_no_puede_volver_a_guion():
    with pytest.raises(IllegalTransition, match="published"):
        assert_transition("published", "script_draft")


def test_un_video_publicado_no_puede_volver_a_publicarse():
    with pytest.raises(IllegalTransition):
        assert_transition("published", "published")


@pytest.mark.parametrize("terminal", ["measured", "rejected"])
def test_los_estados_terminales_no_tienen_salida(terminal):
    assert VIDEO_TRANSITIONS[terminal] == frozenset()
    with pytest.raises(IllegalTransition, match="terminal"):
        assert_transition(terminal, "producing")


def test_desde_failed_solo_se_reintenta_produccion_o_programacion():
    assert VIDEO_TRANSITIONS["failed"] == frozenset({"producing", "scheduled"})


def test_un_estado_de_origen_desconocido_se_rechaza_nombrandolo():
    with pytest.raises(IllegalTransition, match="Estado de origen desconocido"):
        assert_transition("en_revision", "script_draft")


def test_un_estado_de_destino_desconocido_se_rechaza_nombrandolo():
    with pytest.raises(IllegalTransition, match="Estado de destino desconocido"):
        assert_transition("script_draft", "casi_listo")


def test_can_transition_con_estado_desconocido_devuelve_false_sin_lanzar():
    assert can_transition("inventado", "script_draft") is False


def test_illegal_transition_es_un_valueerror():
    # Los llamadores que ya capturan ValueError no se quedan sin red.
    assert issubclass(IllegalTransition, ValueError)


def test_todo_estado_del_camino_feliz_es_alcanzable_desde_idea_approved():
    camino = [
        "idea_approved", "script_draft", "script_approved", "producing",
        "video_ready", "video_approved", "scheduled", "published", "measured",
    ]
    for origen, destino in itertools.pairwise(camino):
        assert_transition(origen, destino)


# ---------------------------------------------------------------------------
# Los estados de los modelos y los del esquema no pueden divergir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado", sorted(VIDEO_STATUSES))
def test_todo_estado_de_video_del_modelo_lo_acepta_el_esquema(conn, estado):
    conn.execute("INSERT INTO videos (title, status) VALUES (?, ?)", ("t", estado))

    fila = conn.execute("SELECT status FROM videos ORDER BY id DESC LIMIT 1").fetchone()
    assert fila["status"] == estado


def test_el_esquema_rechaza_un_estado_de_video_que_el_modelo_no_conoce(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO videos (title, status) VALUES ('t', 'en_revision')")


@pytest.mark.parametrize("estado", sorted(IDEA_STATUSES))
def test_todo_estado_de_idea_del_modelo_lo_acepta_el_esquema(conn, estado):
    conn.execute(
        "INSERT INTO ideas (title, niche, status) VALUES ('t', 'n', ?)", (estado,)
    )

    fila = conn.execute("SELECT status FROM ideas ORDER BY id DESC LIMIT 1").fetchone()
    assert fila["status"] == estado


@pytest.mark.parametrize("estado", sorted(JOB_STATUSES))
def test_todo_estado_de_job_del_modelo_lo_acepta_el_esquema(conn, estado):
    conn.execute("INSERT INTO jobs (type, status) VALUES ('t', ?)", (estado,))

    fila = conn.execute("SELECT status FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    assert fila["status"] == estado
