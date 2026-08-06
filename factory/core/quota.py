"""Presupuesto de cuota por API y día sobre la tabla `api_usage`.

Patrón reservar → llamar → reconciliar: el coste se apunta ANTES de disparar la
petición. Si el proceso muere a mitad de llamada, el contador queda por encima
del gasto real (seguro), nunca por debajo (pasarse del límite sin saberlo).

Se corta al 80% del presupuesto configurado: el margen es para lo que se escapó.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from factory.core.db import get_conn, transaction

logger = logging.getLogger(__name__)

CUTOFF_RATIO = 0.8


class QuotaExceeded(RuntimeError):
    """La reserva superaría el tope diario efectivo (presupuesto × 0.8)."""


def _today() -> str:
    """Día actual en UTC (YYYY-MM-DD): mismo reloj que usan las cuotas de Google."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def usage_today(api: str, conn: sqlite3.Connection | None = None) -> int:
    """Unidades consumidas (reservadas + liquidadas) hoy por una API."""
    conn = conn or get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM api_usage WHERE api = ? AND day = ?",
        (api, _today()),
    ).fetchone()
    return int(row[0])


def reserve(
    api: str,
    units: int,
    daily_budget: int,
    *,
    detail: str = "",
    conn: sqlite3.Connection | None = None,
) -> int:
    """Reserva `units` contra el presupuesto de hoy ANTES de hacer la llamada.

    Devuelve el id de la reserva (para reconciliar después). Lanza
    `QuotaExceeded` si la reserva superaría el 80% del presupuesto.
    """
    if units <= 0:
        raise ValueError(f"units debe ser positivo, no {units}")
    conn = conn or get_conn()
    limite = int(daily_budget * CUTOFF_RATIO)
    # Un solo `_today()` para toda la transacción: si el día UTC cambiase entre
    # la comprobación y el INSERT, se contaría contra el presupuesto de ayer y
    # se apuntaría en el de hoy.
    dia = _today()
    with transaction(conn):
        gastado_row = conn.execute(
            "SELECT COALESCE(SUM(units), 0) FROM api_usage WHERE api = ? AND day = ?",
            (api, dia),
        ).fetchone()
        gastado = int(gastado_row[0])
        if gastado + units > limite:
            raise QuotaExceeded(
                f"{api}: {gastado} + {units} unidades superaría el tope de {limite}"
                f" ({CUTOFF_RATIO:.0%} de {daily_budget})"
            )
        cur = conn.execute(
            "INSERT INTO api_usage (api, day, units, status, detail)"
            " VALUES (?, ?, ?, 'reserved', ?)",
            (api, dia, units, detail),
        )
    return int(cur.lastrowid)


def reconcile(
    reservation_id: int,
    actual_units: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Liquida una reserva tras la llamada.

    Con `actual_units` se corrige el coste (p. ej. la llamada falló antes de
    contar: 0 unidades). Sin él, la reserva se da por buena tal cual.

    Liquidar una reserva que no existe no lanza —el llamador ya está en su
    camino de limpieza— pero deja un WARNING: significa que hay gasto real sin
    apuntar, y en silencio nadie se enteraría.
    """
    conn = conn or get_conn()
    if actual_units is not None and actual_units < 0:
        raise ValueError(f"actual_units debe ser >= 0, no {actual_units}")

    if actual_units is None:
        cur = conn.execute(
            "UPDATE api_usage SET status = 'settled' WHERE id = ?", (reservation_id,)
        )
    else:
        cur = conn.execute(
            "UPDATE api_usage SET status = 'settled', units = ? WHERE id = ?",
            (actual_units, reservation_id),
        )

    if cur.rowcount == 0:
        logger.warning(
            "No existe la reserva de cuota %d: no se ha liquidado nada", reservation_id
        )
