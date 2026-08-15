"""Job `write_script`: de una idea aprobada a un guion en `videos`.

El cliente de Gemini se dobla entero (`llm.generate_json`), nunca la criba ni el
guardado: lo que se prueba aquí es el código del writer, no el doble. Un test
—el de la clave ausente— usa el módulo real a propósito, porque el camino "no
hay credencial" tiene que seguir siendo fuente caída y no guion vacío.

Las dos invariantes que gobiernan este fichero:

1. **Jamás relleno.** Si el modelo no responde, o responde algo que no es un
   guion de 6-10 minutos, el job FALLA y en `videos` no queda ni una fila. Un
   guion inventado aquí viajaría hasta YouTube sin que nadie lo mire.
2. **Un video que ya avanzó no se pisa.** La comprobación de estado va dentro de
   la misma transacción que la escritura, y hay otra antes de llamar al modelo
   para no gastar cuota en un guion que no se va a poder guardar.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from freezegun import freeze_time

from factory.core import config, db, llm, queue
from factory.core.http_util import SourceUnavailable
from factory.core.models import Job
from factory.core.quota import QuotaExceeded
from factory.script import formats, writer

AHORA = "2026-08-14 10:00:00"

SETTINGS = {
    "niches": {
        "pruebas": {
            "name": "Pruebas",
            "language": "es",
            "tone": "calmado, reflexivo",
            "keywords": ["hábitos"],
        }
    },
    "script_formats": {"default": "educativo", "by_niche": {"pruebas": "misterio"}},
}

TITULO = "Por qué tus hábitos se rompen en la tercera semana"


# ---------------------------------------------------------------------------
# Material
# ---------------------------------------------------------------------------


def _palabras(cuantas: int) -> str:
    """Un texto con exactamente ese número de palabras."""
    return " ".join(["palabra"] * cuantas)


def _guion(
    *, gancho: int = 30, capitulos: list[int] | None = None, cierre: int = 20
) -> dict[str, Any]:
    """Respuesta del modelo con los tamaños que se le pidan, en palabras.

    Por defecto: 30 + 6x150 + 20 = 950 palabras, dentro de la banda aceptable.
    """
    largos = [150] * 6 if capitulos is None else capitulos
    return {
        "gancho": _palabras(gancho),
        "capitulos": [
            {"titulo": f"Capítulo {numero}", "narracion": _palabras(largo)}
            for numero, largo in enumerate(largos, start=1)
        ],
        "cierre": _palabras(cierre),
    }


# 950 palabras: 30 del gancho + 900 de los capítulos + 20 del cierre.
GUION_VALIDO = _guion()
SIN_CAPITULOS = {"gancho": _palabras(30), "cierre": _palabras(20)}


class DobleDelLlm:
    """Doble de `llm.generate_json` que registra los prompts que recibe."""

    def __init__(self) -> None:
        self._respuesta: Any = json.loads(json.dumps(GUION_VALIDO))
        self.prompts: list[str] = []
        self.sistemas: list[str | None] = []
        self.tokens: list[int] = []
        self.timeouts: list[float] = []

    def responde(self, respuesta: Any) -> None:
        """Fija lo que devolverá el modelo: un valor, o una función del prompt."""
        self._respuesta = respuesta

    def falla(self, excepcion: Exception) -> None:
        self._respuesta = excepcion

    def generate_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_output_tokens: int = 1024,
        timeout: float = llm.TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> Any:
        self.prompts.append(prompt)
        self.sistemas.append(system)
        self.tokens.append(max_output_tokens)
        self.timeouts.append(timeout)
        if isinstance(self._respuesta, Exception):
            raise self._respuesta
        if callable(self._respuesta):
            return self._respuesta(prompt)
        return json.loads(json.dumps(self._respuesta))


@pytest.fixture
def modelo(monkeypatch: pytest.MonkeyPatch) -> DobleDelLlm:
    """Instala el doble del LLM. Los tests que NO lo piden usan el módulo real."""
    doble = DobleDelLlm()
    monkeypatch.setattr(llm, "generate_json", doble.generate_json)
    return doble


@pytest.fixture
def entorno(conn: sqlite3.Connection, settings_falsas) -> None:
    """Base migrada + configuración de prueba (nicho 'pruebas', formato misterio)."""
    settings_falsas(SETTINGS)


def _nueva_idea(
    conn: sqlite3.Connection,
    *,
    titulo: str = TITULO,
    niche: str = "pruebas",
    suggested_format: str | None = None,
    score_details: str = "{}",
    status: str = "approved",
) -> int:
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO ideas (title, niche, keyword, score_details, status,"
            " suggested_format) VALUES (?, ?, 'hábitos', ?, ?, ?)",
            (titulo, niche, score_details, status, suggested_format),
        )
    return int(cursor.lastrowid)


def _nuevo_video(
    conn: sqlite3.Connection, idea_id: int, estado: str, *, kind: str = "long"
) -> int:
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO videos (idea_id, kind, title, status) VALUES (?, ?, ?, ?)",
            (idea_id, kind, "Un título viejo", estado),
        )
    return int(cursor.lastrowid)


def _aprendizaje(
    conn: sqlite3.Connection,
    kind: str,
    content: str,
    *,
    niche: str | None = "pruebas",
    performance: float | None = None,
) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO knowledge (kind, niche, content, performance) VALUES (?, ?, ?, ?)",
            (kind, niche, content, performance),
        )


def _job(idea_id: Any) -> Job:
    return Job(id=1, type=writer.JOB_TYPE, payload={"idea_id": idea_id})


def _encola_y_procesa(
    conn: sqlite3.Connection,
    idea_id: int,
    *,
    max_attempts: int = 3,
    parada_pedida: bool = False,
) -> int:
    """Encola el job y lo pasa por el worker real. Devuelve el id del job.

    Por aquí van los tests que miran el estado en el que queda `jobs`: qué le
    pasa a la fila es justo lo que decide si un fallo se ve en el dashboard o
    desaparece.

    Con `parada_pedida=True` el worker arranca ya con el apagado en curso, que
    es lo que mira la cola para decidir si devolver el turno. Un test que solo
    doble `should_stop()` simularía algo que en producción no existe: esa
    función lee el evento de parada de este mismo worker.
    """
    worker = queue.JobWorker()
    writer.register(worker)
    if parada_pedida:
        # `stop()` sin hilo arrancado solo pide la parada y vuelve.
        worker.stop()
    job_id = queue.enqueue(writer.JOB_TYPE, {"idea_id": idea_id}, max_attempts=max_attempts)
    worker._process_one()
    return job_id


def _segundos_de_espera(conn: sqlite3.Connection, run_after: str | None) -> float:
    """Segundos que le faltan a `run_after` para vencer, según la propia base."""
    fila = conn.execute(
        "SELECT (julianday(?) - julianday('now')) * 86400.0", (run_after,)
    ).fetchone()
    return float(fila[0])


def _job_row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT status, attempts, error, run_after FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()


def _videos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM videos ORDER BY id").fetchall())


def _video(conn: sqlite3.Connection) -> sqlite3.Row:
    filas = _videos(conn)
    assert len(filas) == 1, f"se esperaba un solo video, hay {len(filas)}"
    return filas[0]


def _script(fila: sqlite3.Row) -> dict[str, Any]:
    return json.loads(fila["script_json"])


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_un_guion_valido_nace_como_borrador_listo_para_el_humano(conn, entorno, modelo):
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    fila = _video(conn)
    assert fila["status"] == "script_draft"
    assert fila["idea_id"] == idea
    assert fila["kind"] == "long"
    assert fila["format"] == "misterio"
    assert fila["title"] == TITULO


@freeze_time(AHORA)
def test_el_script_json_guarda_el_guion_entero_con_su_procedencia(conn, entorno, modelo):
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    guion = _script(_video(conn))
    assert guion["format"] == "misterio"
    assert guion["model"] == llm.MODEL
    assert guion["generated_at"] == "2026-08-14T10:00:00Z"
    assert guion["hook"] == _palabras(30)
    assert guion["outro"] == _palabras(20)
    assert guion["word_count"] == 950
    assert guion["words_per_minute"] == 145
    assert guion["estimated_seconds"] == 393  # round(950 * 60 / 145)


@freeze_time(AHORA)
def test_cada_capitulo_lleva_sus_palabras_y_el_segundo_en_el_que_empieza(
    conn, entorno, modelo
):
    # El gancho son 12 s y cada capítulo 62 s: el primero se ancla en 0 (YouTube
    # exige que el primer marcador sea 00:00) y los demás acumulan desde ahí.
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    capitulos = _script(_video(conn))["chapters"]
    assert [c["title"] for c in capitulos] == [f"Capítulo {i}" for i in range(1, 7)]
    assert [c["words"] for c in capitulos] == [150] * 6
    assert [c["start_sec"] for c in capitulos] == [0, 74, 136, 198, 260, 322]


@freeze_time(AHORA)
def test_el_listado_de_capitulos_del_video_empieza_en_00_00(conn, entorno, modelo):
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    assert _video(conn)["chapters"].splitlines() == [
        "00:00 Capítulo 1",
        "01:14 Capítulo 2",
        "02:16 Capítulo 3",
        "03:18 Capítulo 4",
        "04:20 Capítulo 5",
        "05:22 Capítulo 6",
    ]


@pytest.mark.parametrize(
    ("capitulos", "palabras"),
    [([216, 217, 217], 700), ([195] * 10, 2000), ([40, 400, 400], 890)],
    ids=["minimo_de_la_banda", "maximo_de_la_banda", "capitulo_de_40_palabras"],
)
@freeze_time(AHORA)
def test_un_guion_justo_en_el_limite_de_la_criba_si_se_acepta(
    conn, entorno, modelo, capitulos, palabras
):
    idea = _nueva_idea(conn)
    modelo.responde(_guion(capitulos=capitulos))

    writer.handle_write_script(_job(idea))

    assert _script(_video(conn))["word_count"] == palabras


# ---------------------------------------------------------------------------
# Jamás relleno: si el modelo no da un guion, no hay fila
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fallo",
    [
        SourceUnavailable("gemini-2.5-flash: respuesta sin candidatos (SAFETY)"),
        SourceUnavailable("gemini-2.5-flash: la respuesta no es JSON"),
        QuotaExceeded("gemini: 160 + 2 unidades superaría el tope"),
        RuntimeError("un fallo que nadie previó"),
    ],
    ids=["bloqueado", "json_ilegible", "sin_cuota", "bug"],
)
@freeze_time(AHORA)
def test_si_el_modelo_no_responde_el_job_falla_y_no_deja_ningun_video(
    conn, entorno, modelo, fallo
):
    idea = _nueva_idea(conn)
    modelo.falla(fallo)

    with pytest.raises(type(fallo)):
        writer.handle_write_script(_job(idea))

    assert _videos(conn) == []


@freeze_time(AHORA)
def test_sin_clave_de_gemini_la_idea_se_queda_sin_guion_en_vez_de_recibir_uno_vacio(
    conn, entorno
):
    # Sin doble: el módulo real lanza SourceUnavailable porque no hay clave.
    idea = _nueva_idea(conn)

    with pytest.raises(SourceUnavailable, match="GOOGLE_AI_API_KEY"):
        writer.handle_write_script(_job(idea))

    assert _videos(conn) == []


@pytest.mark.parametrize(
    ("respuesta", "motivo"),
    [
        ([], "se esperaba un objeto JSON"),
        ("un guion en texto plano", "se esperaba un objeto JSON"),
        (None, "se esperaba un objeto JSON"),
        ({}, "falta el gancho"),
        ({**GUION_VALIDO, "gancho": "   "}, "falta el gancho"),
        ({**GUION_VALIDO, "cierre": 7}, "falta el cierre"),
        (SIN_CAPITULOS, "no trae una lista de capítulos"),
        ({**SIN_CAPITULOS, "capitulos": {"uno": "otro"}}, "no trae una lista de capítulos"),
        (_guion(capitulos=[]), "se aceptan entre"),
        (_guion(capitulos=[300, 300]), "se aceptan entre"),
        (_guion(capitulos=[60] * 13), "se aceptan entre"),
        (
            {
                **GUION_VALIDO,
                "capitulos": [
                    GUION_VALIDO["capitulos"][0],
                    "un capítulo en texto plano",
                    GUION_VALIDO["capitulos"][2],
                ],
            },
            "el capítulo 2 no es un objeto JSON",
        ),
        (
            {
                "gancho": _palabras(30),
                "cierre": _palabras(20),
                "capitulos": [
                    {"titulo": "Uno", "narracion": _palabras(300)},
                    {"narracion": _palabras(300)},
                    {"titulo": "Tres", "narracion": _palabras(300)},
                ],
            },
            "falta el título del capítulo 2",
        ),
        (
            {
                "gancho": _palabras(30),
                "cierre": _palabras(20),
                "capitulos": [{"titulo": "Uno"}] + _guion()["capitulos"][1:],
            },
            "falta la narración del capítulo 1",
        ),
        (_guion(capitulos=[39, 400, 400]), "es un capítulo de relleno"),
        (_guion(capitulos=[216, 217, 216]), "fuera de la banda aceptable"),
        (_guion(capitulos=[196] + [195] * 9), "fuera de la banda aceptable"),
    ],
    ids=[
        "lista", "texto", "nulo", "vacio", "gancho_en_blanco", "cierre_no_texto",
        "sin_capitulos", "capitulos_no_lista", "cero_capitulos", "dos_capitulos",
        "trece_capitulos", "capitulo_no_objeto", "capitulo_sin_titulo",
        "capitulo_sin_narracion", "capitulo_de_relleno", "guion_corto", "guion_largo",
    ],
)
@freeze_time(AHORA)
def test_una_respuesta_que_no_es_un_guion_no_escribe_ninguna_fila(
    conn, entorno, modelo, respuesta, motivo
):
    idea = _nueva_idea(conn)
    modelo.responde(respuesta)

    with pytest.raises(ValueError, match=motivo):
        writer.handle_write_script(_job(idea))

    assert _videos(conn) == []


@freeze_time(AHORA)
def test_un_guion_rechazado_no_pisa_el_video_que_la_idea_ya_tenia(conn, entorno, modelo):
    idea = _nueva_idea(conn)
    _nuevo_video(conn, idea, "idea_approved")
    modelo.responde(_guion(capitulos=[300, 300]))

    with pytest.raises(ValueError, match="se aceptan entre"):
        writer.handle_write_script(_job(idea))

    fila = _video(conn)
    assert fila["status"] == "idea_approved"
    assert fila["script_json"] is None


@freeze_time(AHORA)
def test_el_motivo_del_fallo_queda_en_jobs_error_para_que_lo_vea_el_dashboard(
    conn, entorno, modelo
):
    idea = _nueva_idea(conn)
    modelo.responde(_guion(capitulos=[300, 300]))
    worker = queue.JobWorker()
    writer.register(worker)
    queue.enqueue(writer.JOB_TYPE, {"idea_id": idea}, max_attempts=1)

    worker._process_one()

    fila = conn.execute("SELECT status, error FROM jobs").fetchone()
    assert fila["status"] == "failed"
    assert "capítulos" in fila["error"]
    assert _videos(conn) == []


# ---------------------------------------------------------------------------
# Payload y idea
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"idea_id": None},
        {"idea_id": "7"},
        {"idea_id": 0},
        {"idea_id": -3},
        {"idea_id": 1.0},
        # True es un int que vale 1: sin guarda, escribiría el guion de la idea 1.
        {"idea_id": True},
        {"idea_id": False},
    ],
    ids=["vacio", "nulo", "texto", "cero", "negativo", "decimal", "cierto", "falso"],
)
@freeze_time(AHORA)
def test_un_payload_sin_idea_id_entero_es_un_error_del_llamador(
    conn, entorno, modelo, payload
):
    with pytest.raises(ValueError, match="idea_id entero positivo"):
        writer.handle_write_script(Job(id=1, type=writer.JOB_TYPE, payload=payload))

    assert modelo.prompts == []


@freeze_time(AHORA)
def test_una_idea_que_no_existe_no_gasta_una_llamada_al_modelo(conn, entorno, modelo):
    with pytest.raises(ValueError, match="la idea 99 no existe"):
        writer.handle_write_script(_job(99))

    assert modelo.prompts == []
    assert _videos(conn) == []


@pytest.mark.parametrize("estado", ["rejected", "new", "shortlisted"])
@freeze_time(AHORA)
def test_una_idea_que_no_esta_aprobada_no_recibe_guion_ni_gasta_cuota(
    conn, entorno, modelo, estado
):
    # El humano rechaza la idea mientras su job espera en la cola o en el
    # backoff de un reintento: sin esta guarda nacía un video en 'script_draft'
    # de una idea rechazada, y el job terminaba en 'done'.
    idea = _nueva_idea(conn, status=estado)

    job_id = _encola_y_procesa(conn, idea)

    assert modelo.prompts == []
    assert _videos(conn) == []
    trabajo = _job_row(conn, job_id)
    assert trabajo["status"] == "failed"
    assert f"estado '{estado}'" in trabajo["error"]
    assert trabajo["attempts"] == 1  # el estado de una idea no vuelve solo


@freeze_time(AHORA)
def test_una_idea_ya_usada_si_admite_que_se_le_reescriba_el_guion(conn, entorno, modelo):
    # El dashboard marca la idea como 'used' al encolar el job: si 'used' no
    # pasara la guarda, ningún guion llegaría a escribirse nunca.
    idea = _nueva_idea(conn, status="used")

    writer.handle_write_script(_job(idea))

    assert _video(conn)["status"] == "script_draft"


# ---------------------------------------------------------------------------
# Elección de formato
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sugerido", "esperado"),
    [
        ("educativo", "educativo"),
        ("top_n", "top_n"),
        ("noticias", "noticias"),
        (None, "misterio"),
        ("", "misterio"),
        ("videoclip", "misterio"),
    ],
    ids=["sugerido", "sugerido_top_n", "sugerido_noticias", "sin_sugerir", "vacio", "inventado"],
)
@freeze_time(AHORA)
def test_el_formato_sale_de_la_idea_y_si_no_del_nicho(
    conn, entorno, modelo, sugerido, esperado
):
    idea = _nueva_idea(conn, suggested_format=sugerido)

    writer.handle_write_script(_job(idea))

    fila = _video(conn)
    assert fila["format"] == esperado
    assert _script(fila)["format"] == esperado


@freeze_time(AHORA)
def test_el_prompt_lleva_las_instrucciones_del_formato_elegido_y_solo_esas(
    conn, entorno, modelo
):
    idea = _nueva_idea(conn, suggested_format="top_n")

    writer.handle_write_script(_job(idea))

    prompt = modelo.prompts[0]
    assert formats.instructions("top_n") in prompt
    assert formats.instructions("misterio") not in prompt


@freeze_time(AHORA)
def test_un_formato_desconocido_en_la_config_revienta_antes_de_gastar_la_llamada(
    conn, settings_falsas, modelo
):
    settings_falsas({**SETTINGS, "script_formats": {"default": "videoclip"}})
    idea = _nueva_idea(conn)

    with pytest.raises(ValueError, match="formato de guion desconocido"):
        writer.handle_write_script(_job(idea))

    assert modelo.prompts == []
    assert _videos(conn) == []


@pytest.mark.parametrize(
    ("bloque", "niche", "esperado"),
    [
        ({"default": "educativo", "by_niche": {"pruebas": "misterio"}}, "pruebas", "misterio"),
        ({"default": "educativo", "by_niche": {"otro": "misterio"}}, "pruebas", "educativo"),
        ({"default": "educativo"}, "pruebas", "educativo"),
        ({"default": "educativo", "by_niche": {"pruebas": ""}}, "pruebas", "educativo"),
    ],
    ids=["nicho_propio", "nicho_sin_entrada", "sin_by_niche", "entrada_vacia"],
)
def test_script_format_cae_al_formato_por_defecto_cuando_el_nicho_no_tiene_el_suyo(
    settings_falsas, bloque, niche, esperado
):
    settings_falsas({**SETTINGS, "script_formats": bloque})

    assert config.script_format(niche) == esperado


def test_los_nichos_reales_usan_formatos_que_el_guionista_sabe_escribir():
    # Contra el settings.yaml de verdad: una errata ahí deja al nicho sin guiones.
    for niche in config.niches():
        assert formats.is_known(config.script_format(niche)), niche
    assert formats.is_known(config.settings()["script_formats"]["default"])


def test_los_formatos_que_sugiere_la_investigacion_son_los_que_sabe_escribir_el_guionista():
    # `ideas.suggested_format` lo escribe research/ y lo lee script/: si las dos
    # listas se separan, la sugerencia se descarta en silencio.
    from factory.research.pipeline import ALLOWED_FORMATS

    assert set(ALLOWED_FORMATS) == set(formats.FORMATS)


# ---------------------------------------------------------------------------
# Qué se le enseña al modelo
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_el_prompt_lleva_el_titulo_de_la_idea_y_el_tono_del_nicho(conn, entorno, modelo):
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    prompt = modelo.prompts[0]
    assert TITULO in prompt
    assert "calmado, reflexivo" in prompt
    assert modelo.sistemas[0] == writer.SYSTEM_PROMPT


@freeze_time(AHORA)
def test_el_angulo_que_propuso_la_investigacion_llega_al_guionista(conn, entorno, modelo):
    idea = _nueva_idea(
        conn,
        score_details=json.dumps({"topic": {"angle": "Qué pasa cuando se acaba la motivación"}}),
    )

    writer.handle_write_script(_job(idea))

    assert "Qué pasa cuando se acaba la motivación" in modelo.prompts[0]


@pytest.mark.parametrize(
    "detalles",
    ["{ esto no es json", "[]", "null", json.dumps({"topic": {"angle": "   "}})],
    ids=["ilegible", "no_es_objeto", "nulo", "angulo_en_blanco"],
)
@freeze_time(AHORA)
def test_un_score_details_inservible_no_deja_la_idea_sin_guion(
    conn, entorno, modelo, detalles
):
    idea = _nueva_idea(conn, score_details=detalles)

    writer.handle_write_script(_job(idea))

    assert _video(conn)["status"] == "script_draft"
    assert "De qué trata:" not in modelo.prompts[0]


@freeze_time(AHORA)
def test_con_la_tabla_knowledge_vacia_el_prompt_sale_sin_bloque_de_aprendizajes(
    conn, entorno, modelo
):
    # Es su estado real hoy: la llena el cerebro del hito 5.
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    assert "que funcionaron en este nicho" not in modelo.prompts[0]
    assert _video(conn)["status"] == "script_draft"


@freeze_time(AHORA)
def test_los_ganchos_medidos_llegan_al_prompt_los_mejores_primero(conn, entorno, modelo):
    _aprendizaje(conn, "hook", "Gancho regular", performance=0.5)
    _aprendizaje(conn, "hook", "Gancho sin medir", performance=None)
    _aprendizaje(conn, "hook", "Gancho bueno", performance=0.9)
    _aprendizaje(conn, "cta", "Una llamada a la acción", performance=0.7)
    _aprendizaje(conn, "title_pattern", "Un patrón de título", performance=0.9)
    _aprendizaje(conn, "hook", "Gancho de otro nicho", niche="otro", performance=1.0)
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    prompt = modelo.prompts[0]
    assert [linea[2:] for linea in prompt.splitlines() if linea.startswith("- G")] == [
        "Gancho bueno",
        "Gancho regular",
        "Gancho sin medir",
    ]
    assert "Una llamada a la acción" in prompt
    assert "Un patrón de título" not in prompt
    assert "Gancho de otro nicho" not in prompt


@freeze_time(AHORA)
def test_no_se_le_enseñan_mas_de_cuatro_ganchos(conn, entorno, modelo):
    for numero in range(6):
        _aprendizaje(conn, "hook", f"Gancho {numero}", performance=1.0 - numero / 10)
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    ganchos = [l for l in modelo.prompts[0].splitlines() if l.startswith("- Gancho ")]
    assert ganchos == ["- Gancho 0", "- Gancho 1", "- Gancho 2", "- Gancho 3"]


# ---------------------------------------------------------------------------
# Un video que ya avanzó no se pisa
# ---------------------------------------------------------------------------


@freeze_time(AHORA)
def test_un_video_en_idea_approved_recibe_el_guion_en_su_misma_fila(conn, entorno, modelo):
    idea = _nueva_idea(conn)
    video = _nuevo_video(conn, idea, "idea_approved")

    writer.handle_write_script(_job(idea))

    fila = _video(conn)
    assert fila["id"] == video
    assert fila["status"] == "script_draft"
    assert fila["title"] == TITULO


@pytest.mark.parametrize(
    "estado",
    ["script_draft", "script_approved", "producing", "video_ready", "published", "rejected"],
)
@freeze_time(AHORA)
def test_un_video_que_ya_avanzo_no_admite_otro_guion_ni_gasta_cuota(
    conn, entorno, modelo, estado
):
    idea = _nueva_idea(conn)
    _nuevo_video(conn, idea, estado)

    writer.handle_write_script(_job(idea))

    assert modelo.prompts == []
    fila = _video(conn)
    assert fila["status"] == estado
    assert fila["script_json"] is None


@freeze_time(AHORA)
def test_si_el_video_avanza_mientras_el_modelo_escribe_el_job_falla_con_el_motivo(
    conn, entorno, modelo
):
    # La comprobación previa pasó, pero para cuando vuelve el modelo el humano ya
    # aprobó el guion anterior y el video está produciéndose. La comprobación que
    # cuenta es la de dentro de la transacción.
    #
    # El guion se descarta, pero la llamada YA se pagó: terminar en 'done' sin
    # rastro deja la cuota gastada y a nadie mirando. El job falla, y sin
    # reintento: lo que cerró la puerta fue un humano moviendo el video.
    idea = _nueva_idea(conn)
    video = _nuevo_video(conn, idea, "idea_approved")

    def mientras_escribe(prompt: str) -> dict[str, Any]:
        with db.transaction(conn):
            conn.execute("UPDATE videos SET status = 'producing' WHERE id = ?", (video,))
        return json.loads(json.dumps(GUION_VALIDO))

    modelo.responde(mientras_escribe)
    job_id = _encola_y_procesa(conn, idea)

    fila = _video(conn)
    assert fila["status"] == "producing"
    assert fila["script_json"] is None
    trabajo = _job_row(conn, job_id)
    assert trabajo["status"] == "failed"
    assert "avanzó mientras se escribía el guion" in trabajo["error"]
    assert trabajo["attempts"] == 1  # no se reintenta contra una decisión humana


@freeze_time(AHORA)
def test_un_short_de_la_misma_idea_no_cuenta_como_guion_ya_escrito(conn, entorno, modelo):
    # Los shorts del hito 3 cuelgan del largo y no tienen guion propio.
    idea = _nueva_idea(conn)
    short = _nuevo_video(conn, idea, "producing", kind="short")

    writer.handle_write_script(_job(idea))

    largos = [f for f in _videos(conn) if f["kind"] == "long"]
    assert len(largos) == 1
    assert largos[0]["status"] == "script_draft"
    assert conn.execute(
        "SELECT status FROM videos WHERE id = ?", (short,)
    ).fetchone()["status"] == "producing"


# ---------------------------------------------------------------------------
# Enganche con el worker
# ---------------------------------------------------------------------------


def test_register_engancha_el_handler_al_worker():
    worker = queue.JobWorker()

    writer.register(worker)

    assert worker._handlers[writer.JOB_TYPE] is writer.handle_write_script


@freeze_time(AHORA)
def test_una_parada_pedida_antes_de_llamar_al_modelo_no_gasta_la_llamada(
    conn, entorno, modelo, monkeypatch
):
    # Apagado de la PC: el job vuelve a la cola y lo escribe el próximo arranque.
    idea = _nueva_idea(conn)
    monkeypatch.setattr(writer, "should_stop", lambda: True)

    with pytest.raises(queue.StopRequested, match="parada pedida"):
        writer.handle_write_script(_job(idea))

    assert modelo.prompts == []
    assert _videos(conn) == []


@freeze_time(AHORA)
def test_una_parada_pedida_devuelve_el_job_a_la_cola_sin_gastarle_un_intento(
    conn, entorno, modelo, monkeypatch
):
    # Mismo criterio que `requeue_stale_running`: el apagado de la PC no es
    # culpa del job. Tres apagados seguidos no pueden dejarlo 'failed' sin que
    # se haya llegado a intentar ni una vez.
    #
    # El apagado se simula por las dos piezas que lo componen en producción: el
    # evento de parada del worker (lo que mira la cola) y `should_stop()`, que
    # en el proceso real lo lee de la threading.local que puebla `_run()` — y
    # este test llama a `_process_one()` a mano, sin pasar por `_run()`.
    idea = _nueva_idea(conn)
    monkeypatch.setattr(writer, "should_stop", lambda: True)

    job_id = _encola_y_procesa(conn, idea, parada_pedida=True)

    trabajo = _job_row(conn, job_id)
    assert trabajo["status"] == "pending"
    assert trabajo["attempts"] == 0
    assert trabajo["run_after"] is None  # sin backoff: no hay nada que esperar
    assert "parada pedida" in trabajo["error"]
    assert modelo.prompts == []


@freeze_time(AHORA)
def test_pedir_el_turno_de_vuelta_sin_parada_en_curso_es_un_fallo_como_cualquier_otro(
    conn, entorno, modelo, monkeypatch
):
    # El contrario del test de arriba, y la razón de que la cola compruebe su
    # propio evento de parada: devolver el turno no gasta intento y deja
    # `run_after` a NULL, así que un handler que pidiera volver a la cola fuera
    # del apagado se reclamaría otra vez en la vuelta siguiente —el bucle no va
    # a salir— sin backoff y sin ninguna cota de intentos que acabe cortándolo.
    #
    # Aquí el worker NO está parando: solo el handler cree que sí. Es lo que se
    # vería desde la cola si un handler lanzase StopRequested por su cuenta.
    idea = _nueva_idea(conn)
    monkeypatch.setattr(writer, "should_stop", lambda: True)

    job_id = _encola_y_procesa(conn, idea, parada_pedida=False)

    trabajo = _job_row(conn, job_id)
    assert trabajo["status"] == "pending"  # le quedan 2 de 3 intentos
    assert trabajo["attempts"] == 1  # el intento SÍ se gasta
    assert "StopRequested sin parada en curso" in trabajo["error"]
    assert 25 < _segundos_de_espera(conn, trabajo["run_after"]) <= 30  # backoff
    assert modelo.prompts == []


@freeze_time(AHORA)
def test_sin_cuota_el_job_falla_a_la_primera_en_vez_de_quemar_los_tres_intentos(
    conn, entorno, modelo
):
    # La cuota de Gemini no se levanta hasta medianoche: reintentar a los 30 y a
    # los 60 segundos son dos llamadas más contra el mismo tope. El job queda
    # 'failed' con el motivo, que es de donde lo rescata el humano.
    idea = _nueva_idea(conn)
    modelo.falla(QuotaExceeded("gemini: 160 + 2 unidades superaría el tope"))

    job_id = _encola_y_procesa(conn, idea)

    trabajo = _job_row(conn, job_id)
    assert trabajo["status"] == "failed"
    assert trabajo["attempts"] == 1
    assert "superaría el tope" in trabajo["error"]
    assert _videos(conn) == []


@freeze_time(AHORA)
def test_se_le_pide_al_modelo_margen_de_salida_para_un_guion_entero(conn, entorno, modelo):
    # Con el tope por defecto (1024 tokens) el JSON vuelve truncado y el guion
    # se pierde entero después de haber gastado la llamada.
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    assert modelo.tokens[0] == writer.MAX_OUTPUT_TOKENS
    assert writer.MAX_OUTPUT_TOKENS > llm.DEFAULT_MAX_OUTPUT_TOKENS


@freeze_time(AHORA)
def test_se_le_da_al_modelo_mas_tiempo_del_que_basta_para_una_lista_corta(
    conn, entorno, modelo
):
    # Un timeout no aborta la generación: dispara un reintento que es otra
    # generación entera, hasta seis peticiones reales sin producir un guion.
    idea = _nueva_idea(conn)

    writer.handle_write_script(_job(idea))

    assert modelo.timeouts[0] == writer.LLM_TIMEOUT_SECONDS
    assert writer.LLM_TIMEOUT_SECONDS > llm.TIMEOUT_SECONDS
