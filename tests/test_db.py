"""Conexión, pragmas, migraciones y `transaction()`.

Todos los demás tests con base de datos dependen de que `migrate()` produzca el
esquema real, así que esto se verifica aquí una vez y en serio.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from factory.core import db

TABLAS_ESPERADAS = {
    "ideas", "competitors", "competitor_videos", "videos",
    "jobs", "metrics", "knowledge", "api_usage",
}


def test_migrate_crea_el_esquema_completo_y_sube_user_version(db_path: Path):
    conexion = db.get_conn()

    version = db.migrate(conexion)

    assert version == len(db.MIGRATIONS)
    assert db.schema_version(conexion) == version
    tablas = {
        fila[0]
        for fila in conexion.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert TABLAS_ESPERADAS <= tablas


def test_migrate_dos_veces_no_vuelve_a_aplicar_nada(conn: sqlite3.Connection):
    conn.execute("INSERT INTO ideas (title, niche) VALUES ('sobrevive', 'n')")

    version = db.migrate(conn)

    assert version == len(db.MIGRATIONS)
    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1


def test_migrate_crea_el_directorio_de_la_base_si_no_existe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ruta = tmp_path / "no" / "existe" / "factory.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", ruta)
    db.close_conn()

    try:
        db.migrate(db.get_conn())
        assert ruta.exists()
    finally:
        db.close_conn()


@pytest.mark.parametrize(
    ("pragma", "esperado"),
    [("journal_mode", "wal"), ("foreign_keys", 1), ("synchronous", 1)],
)
def test_cada_conexion_nueva_trae_sus_pragmas_puestos(db_path: Path, pragma, esperado):
    conexion = db.get_conn()

    valor = conexion.execute(f"PRAGMA {pragma}").fetchone()[0]

    assert valor == esperado


def test_get_conn_devuelve_la_misma_conexion_dentro_del_mismo_hilo(db_path: Path):
    assert db.get_conn() is db.get_conn()


def test_cada_hilo_recibe_su_propia_conexion(db_path: Path):
    principal = db.get_conn()
    del_hilo: list[sqlite3.Connection] = []

    def trabajar() -> None:
        del_hilo.append(db.get_conn())
        db.close_conn()

    hilo = threading.Thread(target=trabajar)
    hilo.start()
    hilo.join()

    assert del_hilo[0] is not principal


def test_close_conn_dos_veces_seguidas_no_revienta(db_path: Path):
    db.get_conn()
    db.close_conn()
    db.close_conn()  # no debe lanzar


def test_transaction_confirma_los_cambios_al_salir_bien(conn: sqlite3.Connection):
    with db.transaction(conn):
        conn.execute("INSERT INTO ideas (title, niche) VALUES ('a', 'n')")
        conn.execute("INSERT INTO ideas (title, niche) VALUES ('b', 'n')")

    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 2


def test_transaction_deshace_todo_el_bloque_si_algo_lanza(conn: sqlite3.Connection):
    with pytest.raises(RuntimeError, match="a mitad"):
        with db.transaction(conn):
            conn.execute("INSERT INTO ideas (title, niche) VALUES ('a', 'n')")
            raise RuntimeError("revienta a mitad de la escritura")

    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 0


def test_transaction_deshace_tambien_ante_un_keyboardinterrupt(conn: sqlite3.Connection):
    # Se captura BaseException a propósito: un Ctrl-C a mitad de escritura no
    # puede dejar media transacción aplicada.
    with pytest.raises(KeyboardInterrupt):
        with db.transaction(conn):
            conn.execute("INSERT INTO ideas (title, niche) VALUES ('a', 'n')")
            raise KeyboardInterrupt

    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 0


def test_las_claves_foraneas_se_aplican_de_verdad(conn: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO competitor_videos (competitor_id, yt_video_id)"
            " VALUES (99999, 'abc')"
        )
