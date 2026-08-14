"""Acceso a SQLite: una conexión por hilo, pragmas por conexión y migraciones versionadas.

Todo el sistema comparte una sola base (`data/factory.db`). Los módulos de dominio
no se importan entre sí: se comunican escribiendo y leyendo aquí (patrón blackboard).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from factory.core import text

logger = logging.getLogger(__name__)

# Raíz del proyecto: .../content-factory/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "factory.db"

_local = threading.local()


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """Devuelve la conexión SQLite del hilo actual, creándola si no existe.

    `check_same_thread` queda en su valor por defecto (True) a propósito:
    cada hilo tiene la suya y compartirlas sería un error, no una optimización.

    Pedir una ruta distinta a la que el hilo ya tiene abierta lanza `ValueError`:
    devolver la conexión equivocada en silencio escribiría en la base que no es.
    """
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        _assert_same_path(db_path)
        return conn

    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # autocommit=True (modo explícito de Python 3.12+): ninguna transacción
    # implícita. Las escrituras agrupadas usan transaction() con BEGIN IMMEDIATE.
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    _local.conn = conn
    _local.path = path
    return conn


def close_conn() -> None:
    """Cierra la conexión del hilo actual, si existe. Útil al terminar un worker."""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


def _assert_same_path(db_path: Path | None) -> None:
    """Comprueba que la ruta pedida es la que el hilo ya tiene abierta."""
    if db_path is None:
        return
    abierta: Path | None = getattr(_local, "path", None)
    if abierta is None or Path(db_path).resolve() == abierta.resolve():
        return
    raise ValueError(
        f"este hilo ya tiene una conexión abierta a {abierta}; "
        f"llama a close_conn() antes de pedir {db_path}"
    )


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Pragmas que deben fijarse en CADA conexión nueva, no solo al crear la base."""
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transacción de escritura con `BEGIN IMMEDIATE`.

    IMMEDIATE toma el candado de escritura desde el principio: una transacción
    DEFERRED que lee y luego escribe falla al instante con "database is locked"
    ignorando el busy_timeout. Toda escritura de más de una sentencia pasa por aquí.

    El COMMIT va DENTRO del try: si falla, la transacción sigue abierta y sin el
    ROLLBACK todo `BEGIN IMMEDIATE` posterior de este hilo daría "cannot start a
    transaction within a transaction" para siempre.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        _rollback_quietly(conn)
        raise


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """ROLLBACK que registra su propio fallo sin tapar la excepción original."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        logger.exception("El ROLLBACK falló tras un error de transacción")


# ---------------------------------------------------------------------------
# Migraciones. Cada entrada sube `PRAGMA user_version` en 1. Nunca se edita una
# migración ya aplicada: los cambios de esquema posteriores son migraciones nuevas.
#
# Una migración es un script SQL o —cuando el dato a escribir no se puede
# calcular en SQL— una función que recibe la conexión. Las dos corren dentro de
# la misma transacción inmediata que sube `user_version`.
# ---------------------------------------------------------------------------

Migration = str | Callable[[sqlite3.Connection], None]


def _migracion_3_dedupe_key(conn: sqlite3.Connection) -> None:
    """Añade `ideas.dedupe_key` y la rellena para las filas ya escritas.

    Va en Python y no en SQL porque la clave quita tildes y puntuación, y el
    `lower()` de SQLite solo baja el ASCII: calculada en SQL, "El efecto dominó"
    y "El efecto domino" tendrían claves distintas, que es exactamente el
    duplicado que la columna viene a impedir.
    """
    conn.executescript(
        """
        ALTER TABLE ideas ADD COLUMN dedupe_key TEXT;
        CREATE INDEX idx_ideas_dedupe ON ideas (niche, dedupe_key);
        """
    )
    filas = conn.execute("SELECT id, title FROM ideas").fetchall()
    conn.executemany(
        "UPDATE ideas SET dedupe_key = ? WHERE id = ?",
        [(text.dedupe_key(titulo), idea_id) for idea_id, titulo in filas],
    )
    logger.info("dedupe_key calculada para %d ideas ya existentes", len(filas))


MIGRATIONS: list[Migration] = [
    # --- Migración 1: esquema inicial completo -----------------------------
    """
    CREATE TABLE ideas (
        id               INTEGER PRIMARY KEY,
        title            TEXT NOT NULL,
        niche            TEXT NOT NULL,
        keyword          TEXT,
        source           TEXT,                 -- youtube | wikipedia | reddit | news | trends
        demand           REAL,                 -- señales normalizadas 0..1 (NULL = señal ausente)
        competition      REAL,
        evergreen        REAL,
        cpm_factor       REAL,
        score            REAL,                 -- 0..100
        score_details    TEXT,                 -- JSON con métricas crudas para re-ponderar
        status           TEXT NOT NULL DEFAULT 'new'
                         CHECK (status IN ('new', 'shortlisted', 'approved', 'rejected', 'used')),
        suggested_format TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_ideas_status ON ideas (status);
    CREATE INDEX idx_ideas_score  ON ideas (score);
    CREATE INDEX idx_ideas_niche  ON ideas (niche);

    CREATE TABLE competitors (
        id               INTEGER PRIMARY KEY,
        channel_id       TEXT NOT NULL UNIQUE, -- id de canal de YouTube
        name             TEXT,
        niche            TEXT,
        subscribers      INTEGER,
        video_count      INTEGER,
        avg_views        REAL,                 -- media de views de sus últimos videos
        upload_frequency REAL,                 -- videos por semana
        last_checked_at  TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_competitors_niche ON competitors (niche);

    CREATE TABLE competitor_videos (
        id             INTEGER PRIMARY KEY,
        competitor_id  INTEGER NOT NULL REFERENCES competitors (id) ON DELETE CASCADE,
        yt_video_id    TEXT NOT NULL UNIQUE,
        title          TEXT,
        published_at   TEXT,
        views          INTEGER,
        duration_sec   INTEGER,
        views_per_day  REAL,
        outlier_ratio  REAL,                   -- views / media del canal: la señal estrella
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_competitor_videos_competitor ON competitor_videos (competitor_id);
    CREATE INDEX idx_competitor_videos_outlier    ON competitor_videos (outlier_ratio);

    CREATE TABLE videos (
        id              INTEGER PRIMARY KEY,
        idea_id         INTEGER REFERENCES ideas (id),
        channel         TEXT,
        kind            TEXT NOT NULL DEFAULT 'long' CHECK (kind IN ('long', 'short')),
        parent_video_id INTEGER REFERENCES videos (id),  -- el largo del que deriva un short
        format          TEXT,                 -- misterio | educativo | storytelling | top_n | noticias
        title           TEXT,
        description     TEXT,
        tags            TEXT,                 -- JSON array
        chapters        TEXT,
        pinned_comment  TEXT,
        script_json     TEXT,
        status          TEXT NOT NULL DEFAULT 'idea_approved'
                        CHECK (status IN ('idea_approved', 'script_draft', 'script_approved',
                                          'producing', 'video_ready', 'video_approved',
                                          'scheduled', 'published', 'measured',
                                          'rejected', 'failed')),
        video_path      TEXT,
        thumbnail_path  TEXT,
        yt_video_id     TEXT,
        scheduled_at    TEXT,
        published_at    TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_videos_status  ON videos (status);
    CREATE INDEX idx_videos_idea    ON videos (idea_id);
    CREATE INDEX idx_videos_parent  ON videos (parent_video_id);

    CREATE TABLE jobs (
        id           INTEGER PRIMARY KEY,
        type         TEXT NOT NULL,
        payload      TEXT NOT NULL DEFAULT '{}',   -- JSON
        status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'running', 'done', 'failed')),
        priority     INTEGER NOT NULL DEFAULT 0,   -- mayor = antes
        attempts     INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        run_after    TEXT,                         -- no reclamar antes de este instante (backoff)
        error        TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        started_at   TEXT,
        finished_at  TEXT
    );
    CREATE INDEX idx_jobs_claim  ON jobs (status, priority DESC, id);
    CREATE INDEX idx_jobs_type   ON jobs (type);

    CREATE TABLE metrics (
        id            INTEGER PRIMARY KEY,
        video_id      INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
        day           INTEGER NOT NULL,             -- snapshot a día 1 / 3 / 7 / 28
        views         INTEGER,
        impressions   INTEGER,
        ctr           REAL,
        avg_view_pct  REAL,
        subs_gained   INTEGER,
        rpm           REAL,
        collected_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (video_id, day)
    );
    CREATE INDEX idx_metrics_video ON metrics (video_id);
    CREATE INDEX idx_metrics_day   ON metrics (day);

    CREATE TABLE knowledge (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL
                    CHECK (kind IN ('hook', 'cta', 'title_pattern', 'lesson', 'win', 'fail')),
        niche       TEXT,
        content     TEXT NOT NULL,
        performance REAL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_knowledge_kind  ON knowledge (kind);
    CREATE INDEX idx_knowledge_niche ON knowledge (niche);

    CREATE TABLE api_usage (
        id         INTEGER PRIMARY KEY,
        api        TEXT NOT NULL,                  -- youtube | gemini | ...
        day        TEXT NOT NULL,                  -- YYYY-MM-DD (UTC)
        units      INTEGER NOT NULL,               -- coste en unidades de esa API
        status     TEXT NOT NULL DEFAULT 'reserved'
                   CHECK (status IN ('reserved', 'settled')),
        detail     TEXT,                           -- qué llamada fue (p.ej. 'search.list')
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_api_usage_api_day ON api_usage (api, day);
    """,
    # --- Migración 2: bitácora de investigación ----------------------------
    # Una fila por keyword sondeada. Existe porque una keyword que no produce
    # ninguna idea (LLM caído, nada que pasara la criba, todo ya descartado por
    # el humano) no deja rastro en `ideas`, y entonces "por qué hoy no propuso
    # nada" solo se podría responder mirando el log de la PC de producción.
    """
    CREATE TABLE research_log (
        id            INTEGER PRIMARY KEY,
        niche         TEXT NOT NULL,
        keyword       TEXT NOT NULL,
        score         REAL,
        ideas_written INTEGER NOT NULL DEFAULT 0,
        reasons       TEXT,                       -- JSON {fuente: motivo}
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_research_log_created ON research_log (created_at);
    CREATE INDEX idx_research_log_niche   ON research_log (niche, keyword);
    """,
    # --- Migración 3: clave de deduplicación de ideas ----------------------
    # Emparejar por título exacto valía cuando los títulos se copiaban de las
    # fuentes; los escribe un LLM y nunca repite la cadena literal, así que lo
    # que el humano rechazaba volvía al día siguiente reformulado.
    _migracion_3_dedupe_key,
]


def migrate(conn: sqlite3.Connection | None = None) -> int:
    """Aplica las migraciones pendientes según `PRAGMA user_version`.

    Devuelve la versión final del esquema. Cada migración corre dentro de su
    propia transacción inmediata y sube user_version de forma atómica con ella.
    """
    conn = conn or get_conn()
    version = schema_version(conn)
    for target, migracion in enumerate(MIGRATIONS, start=1):
        if version >= target:
            continue
        logger.info("Aplicando migración %d", target)
        # Con autocommit=True, executescript no emite ningún COMMIT implícito,
        # así que la migración y el user_version suben juntos, atómicamente.
        with transaction(conn):
            _aplicar_migracion(conn, migracion)
            conn.execute(f"PRAGMA user_version = {target:d}")
        version = target
    return version


def _aplicar_migracion(conn: sqlite3.Connection, migracion: Migration) -> None:
    """Ejecuta una migración, sea un script SQL o un paso escrito en Python."""
    if isinstance(migracion, str):
        conn.executescript(migracion)
        return
    migracion(conn)


def schema_version(conn: sqlite3.Connection) -> int:
    """Versión actual del esquema (`PRAGMA user_version`)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])
