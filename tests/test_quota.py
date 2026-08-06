"""Presupuesto de cuota: corte al 80%, reserva → reconciliación, cambio de día.

El reloj se congela en todos los tests que dependen del día: sin eso, un test
que reserve a las 23:59:59 UTC contaría contra un día distinto al que comprueba.
"""

from __future__ import annotations

import sqlite3

import pytest
from freezegun import freeze_time

from factory.core import quota
from factory.core.quota import QuotaExceeded

PRESUPUESTO = 4_000          # el de config/settings.yaml para youtube
LIMITE_EFECTIVO = 3_200      # int(4000 * 0.8)


# ---------------------------------------------------------------------------
# El corte al 80%
# ---------------------------------------------------------------------------


def test_el_corte_efectivo_es_el_ochenta_por_ciento_del_presupuesto():
    assert quota.CUTOFF_RATIO == 0.8
    assert int(PRESUPUESTO * quota.CUTOFF_RATIO) == LIMITE_EFECTIVO


def test_una_reserva_dentro_del_presupuesto_se_apunta(conn: sqlite3.Connection):
    reserva = quota.reserve("youtube", 100, PRESUPUESTO, detail="search.list")

    assert reserva > 0
    assert quota.usage_today("youtube") == 100


def test_se_puede_reservar_justo_hasta_el_limite_efectivo(conn: sqlite3.Connection):
    quota.reserve("youtube", LIMITE_EFECTIVO - 1, PRESUPUESTO)

    quota.reserve("youtube", 1, PRESUPUESTO)

    assert quota.usage_today("youtube") == LIMITE_EFECTIVO


def test_pasarse_una_sola_unidad_del_limite_lanza_quota_exceeded(
    conn: sqlite3.Connection,
):
    quota.reserve("youtube", LIMITE_EFECTIVO, PRESUPUESTO)

    with pytest.raises(QuotaExceeded, match="superaría el tope de 3200"):
        quota.reserve("youtube", 1, PRESUPUESTO)


def test_queda_margen_de_cuota_real_por_encima_del_corte(conn: sqlite3.Connection):
    # La gracia del 80%: al cortar sigue habiendo 800 unidades reales sin gastar
    # para lo que se escapó de la contabilidad.
    with pytest.raises(QuotaExceeded):
        quota.reserve("youtube", LIMITE_EFECTIVO + 1, PRESUPUESTO)

    assert quota.usage_today("youtube") == 0


def test_una_reserva_rechazada_no_deja_rastro_en_la_tabla(conn: sqlite3.Connection):
    quota.reserve("youtube", 3_000, PRESUPUESTO)

    with pytest.raises(QuotaExceeded):
        quota.reserve("youtube", 500, PRESUPUESTO)

    assert quota.usage_today("youtube") == 3_000
    assert conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 1


@pytest.mark.parametrize("unidades", [0, -1, -100])
def test_reservar_unidades_no_positivas_es_un_error_de_programacion(
    conn: sqlite3.Connection, unidades
):
    with pytest.raises(ValueError, match="units debe ser positivo"):
        quota.reserve("youtube", unidades, PRESUPUESTO)


def test_el_gasto_de_una_api_no_consume_el_presupuesto_de_otra(
    conn: sqlite3.Connection,
):
    quota.reserve("youtube", LIMITE_EFECTIVO, PRESUPUESTO)

    quota.reserve("gemini", 100, PRESUPUESTO)

    assert quota.usage_today("youtube") == LIMITE_EFECTIVO
    assert quota.usage_today("gemini") == 100


def test_usage_today_de_una_api_sin_gasto_es_cero(conn: sqlite3.Connection):
    assert quota.usage_today("jamas_usada") == 0


# ---------------------------------------------------------------------------
# Reserva → reconciliación
# ---------------------------------------------------------------------------


def test_la_reserva_nace_en_estado_reserved_con_su_detalle(conn: sqlite3.Connection):
    reserva = quota.reserve("youtube", 100, PRESUPUESTO, detail="search.list q='fe'")

    fila = conn.execute(
        "SELECT status, units, detail FROM api_usage WHERE id = ?", (reserva,)
    ).fetchone()
    assert fila["status"] == "reserved"
    assert fila["units"] == 100
    assert fila["detail"] == "search.list q='fe'"


def test_reconciliar_sin_coste_real_da_la_reserva_por_buena(conn: sqlite3.Connection):
    reserva = quota.reserve("youtube", 100, PRESUPUESTO)

    quota.reconcile(reserva)

    fila = conn.execute(
        "SELECT status, units FROM api_usage WHERE id = ?", (reserva,)
    ).fetchone()
    assert fila["status"] == "settled"
    assert fila["units"] == 100
    assert quota.usage_today("youtube") == 100


def test_reconciliar_a_cero_devuelve_la_cuota_de_una_llamada_que_no_llego_a_contar(
    conn: sqlite3.Connection,
):
    reserva = quota.reserve("youtube", 100, PRESUPUESTO)

    quota.reconcile(reserva, 0)

    assert quota.usage_today("youtube") == 0
    assert (
        conn.execute("SELECT status FROM api_usage WHERE id = ?", (reserva,)).fetchone()[
            "status"
        ]
        == "settled"
    )


def test_reconciliar_al_alza_corrige_el_coste_hacia_arriba(conn: sqlite3.Connection):
    reserva = quota.reserve("youtube", 1, PRESUPUESTO)

    quota.reconcile(reserva, 3)

    assert quota.usage_today("youtube") == 3


def test_reconciliar_con_unidades_negativas_es_un_error(conn: sqlite3.Connection):
    reserva = quota.reserve("youtube", 100, PRESUPUESTO)

    with pytest.raises(ValueError, match="actual_units debe ser >= 0"):
        quota.reconcile(reserva, -1)


def test_una_reserva_sin_reconciliar_sigue_contando_contra_el_presupuesto(
    conn: sqlite3.Connection,
):
    # El proceso muere entre la reserva y la llamada: contar de más es seguro.
    quota.reserve("youtube", LIMITE_EFECTIVO, PRESUPUESTO)

    with pytest.raises(QuotaExceeded):
        quota.reserve("youtube", 1, PRESUPUESTO)


# ---------------------------------------------------------------------------
# El día
# ---------------------------------------------------------------------------


def test_el_dia_se_escribe_como_iso_corto_igual_que_lo_busca_usage_today():
    # El formato no es cosmética: `usage_today` filtra por igualdad de cadena.
    # Cambiarlo a %d-%m-%Y dejaría el contador de gasto a cero para siempre.
    with freeze_time("2026-08-06 23:30:00"):
        assert quota._today() == "2026-08-06"


def test_el_dia_de_la_cuota_se_lee_del_reloj_utc(conn: sqlite3.Connection):
    # Con la máquina en UTC+14 y las 22:00 UTC, el día local ya es el siguiente.
    # La cuota de Google se resetea por UTC, así que la fila debe ir al día UTC.
    with freeze_time("2026-08-06 22:00:00"):
        quota.reserve("youtube", 100, PRESUPUESTO)

    dia = conn.execute("SELECT day FROM api_usage").fetchone()["day"]
    assert dia == "2026-08-06"


def test_el_gasto_de_ayer_no_consume_el_presupuesto_de_hoy(conn: sqlite3.Connection):
    with freeze_time("2026-08-06 23:59:00"):
        quota.reserve("youtube", LIMITE_EFECTIVO, PRESUPUESTO)
        assert quota.usage_today("youtube") == LIMITE_EFECTIVO

    with freeze_time("2026-08-07 00:01:00"):
        assert quota.usage_today("youtube") == 0
        quota.reserve("youtube", LIMITE_EFECTIVO, PRESUPUESTO)
        assert quota.usage_today("youtube") == LIMITE_EFECTIVO

    assert conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 2


def test_el_gasto_del_dia_se_acumula_entre_reservas(conn: sqlite3.Connection):
    with freeze_time("2026-08-06 07:30:00"):
        quota.reserve("youtube", 100, PRESUPUESTO)
        quota.reserve("youtube", 1, PRESUPUESTO)
        quota.reserve("youtube", 1, PRESUPUESTO)

        assert quota.usage_today("youtube") == 102
