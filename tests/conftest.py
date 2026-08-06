"""Fixtures compartidas.

Dos reglas gobiernan este fichero:

1. **Una base de datos por test**, en un fichero real de `tmp_path`, creada con
   `db.migrate()` — las migraciones de producción, nunca un esquema copiado a
   mano que se desincronice.
2. **Ningún test toca la red.** Las fuentes se interceptan en la frontera de
   transporte (`responses` sobre requests) o falseando el módulo de fuente
   completo, nunca la función de negocio que se está probando.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from factory.core import config, db

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Ruta de una base vacía en disco, ya instalada como base por defecto.

    Fichero real (no `:memory:`) porque los tests de concurrencia abren una
    conexión por hilo y cada conexión a `:memory:` sería una base distinta.
    `get_conn()` sin argumentos —como lo llaman queue, quota y pipeline— tiene
    que aterrizar aquí también desde hilos nuevos, así que se parchea
    `DEFAULT_DB_PATH`, no la conexión.
    """
    ruta = tmp_path / "factory.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", ruta)
    db.close_conn()
    yield ruta
    db.close_conn()


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    """Conexión del hilo actual sobre una base ya migrada al último esquema."""
    conexion = db.get_conn()
    db.migrate(conexion)
    return conexion


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _config_sin_cache() -> Iterator[None]:
    """`settings()` está cacheada con lru_cache: se limpia entre tests.

    Sin esto, el primer test que cargue una configuración de prueba se la deja
    puesta a todos los siguientes.
    """
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


@pytest.fixture
def settings_falsas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Devuelve una función que instala un `settings.yaml` de prueba."""

    def instalar(data: dict[str, Any]) -> Path:
        ruta = tmp_path / "settings.yaml"
        ruta.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(config, "SETTINGS_PATH", ruta)
        config.settings.cache_clear()
        return ruta

    return instalar


@pytest.fixture(autouse=True)
def _sin_clave_de_youtube(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ningún test hereda una YOUTUBE_API_KEY real del `.env` del desarrollador."""
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red de seguridad: cualquier conexión saliente real revienta el test.

    Las respuestas se falsean con `responses`, que trabaja por encima del socket.
    Si un test se deja una URL sin registrar, sin esto saldría a internet de
    verdad, gastaría cuota y fallaría distinto según el día. Con esto, falla
    siempre y diciendo por qué.
    """
    import socket

    def prohibido(*args, **kwargs):
        raise RuntimeError(
            "un test ha intentado abrir una conexión de red real; "
            "falta registrar la respuesta con responses"
        )

    monkeypatch.setattr(socket.socket, "connect", prohibido)
    monkeypatch.setattr(socket.socket, "connect_ex", prohibido)
    monkeypatch.setattr(socket, "create_connection", prohibido)


# ---------------------------------------------------------------------------
# Muestras de respuestas reales
# ---------------------------------------------------------------------------


@pytest.fixture
def muestra():
    """Carga un fichero de `tests/fixtures/`. JSON decodificado, texto en crudo."""

    def cargar(nombre: str) -> Any:
        ruta = FIXTURES / nombre
        if ruta.suffix == ".json":
            return json.loads(ruta.read_text(encoding="utf-8"))
        return ruta.read_text(encoding="utf-8")

    return cargar


@pytest.fixture
def sin_esperas(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Anula los `time.sleep` de las capas de red y devuelve las esperas pedidas.

    Los tests de reintento comprueban CUÁNTO se habría esperado; esperarlo de
    verdad convertiría la suite en algo que nadie ejecuta.
    """
    from factory.research import http_util, reddit_source

    esperas: list[float] = []

    def registrar(segundos: float) -> None:
        esperas.append(segundos)

    monkeypatch.setattr(http_util.time, "sleep", registrar)
    monkeypatch.setattr(reddit_source.time, "sleep", registrar)
    return esperas
