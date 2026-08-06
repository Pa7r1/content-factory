"""Entrypoint del Content Factory: FastAPI + APScheduler + worker de jobs.

Un solo proceso, un solo worker de uvicorn: el scheduler vive dentro del
proceso y con N workers habría N schedulers disparando el mismo job N veces.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from factory.core import db, queue
from factory.core.scheduler import build_scheduler
from factory.research import pipeline as research_pipeline
from factory.web.server import STATIC_DIR, router as web_router

APP_VERSION = "0.1.0"

LOG_FILE = db.PROJECT_ROOT / "data" / "factory.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 2_000_000
LOG_BACKUPS = 5

# Espera al parar el worker. Un handler consulta `queue.should_stop()` entre
# pasos, así que lo normal es que pare en segundos; si no llega, el log lo dice
# y el job queda 'running' hasta que el siguiente arranque lo reencole.
WORKER_STOP_TIMEOUT = 30.0


def configure_logging() -> None:
    """Log a consola y a `data/factory.log` (rotativo, UTF-8).

    El fichero es lo único que queda cuando la máquina corre desatendida y nadie
    ve la consola. UTF-8 explícito porque el default de Windows es cp1252: un
    carácter fuera de esa página tira la línea entera y se pierde justo la traza
    del fallo que se está buscando.

    Se llama desde el lifespan, no al importar el módulo: `basicConfig(force=True)`
    secuestraría el logging de quien importe `app` (pytest, un script) y abriría
    el fichero de log por el mero hecho de hacer `import app`.
    """
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    formato = logging.Formatter(LOG_FORMAT)

    consola = logging.StreamHandler()
    consola.setFormatter(formato)

    fichero = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    fichero.setFormatter(formato)

    logging.basicConfig(level=logging.INFO, handlers=[consola, fichero], force=True)


logger = logging.getLogger(__name__)

# El worker existe antes del arranque para que los módulos de dominio registren
# aquí sus handlers, siempre antes de `worker.start()`.
worker = queue.JobWorker()
research_pipeline.register(worker)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Arranca logging, DB (migraciones), worker y scheduler; apaga en orden inverso."""
    configure_logging()
    version = db.migrate()
    logger.info("Base de datos lista (esquema v%d)", version)

    worker.start()
    scheduler = build_scheduler()
    scheduler.start()
    # El dashboard lo consulta para mostrar la próxima ejecución programada.
    app.state.scheduler = scheduler

    yield

    scheduler.shutdown(wait=False)
    app.state.scheduler = None
    worker.stop(timeout=WORKER_STOP_TIMEOUT)
    db.close_conn()


app = FastAPI(title="Content Factory", version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(web_router)


class HealthResponse(BaseModel):
    """Estado del proceso para monitorización simple."""

    status: str
    version: str
    db_ok: bool
    schema_version: int
    pending_jobs: int


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Estado: DB accesible, versión del esquema y jobs pendientes.

    Endpoint `def` (no async): sqlite3 es bloqueante y Starlette lo manda al
    threadpool, que es lo correcto.
    """
    conn = db.get_conn()
    db_ok = conn.execute("SELECT 1").fetchone()[0] == 1
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=APP_VERSION,
        db_ok=db_ok,
        schema_version=db.schema_version(conn),
        pending_jobs=queue.pending_count(conn),
    )


if __name__ == "__main__":
    import uvicorn

    # Un solo worker, sin reload: ver nota de despliegue arriba.
    uvicorn.run(app, host="127.0.0.1", port=8000)
