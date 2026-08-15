"""Job `write_script`: de una idea a un guion con capítulos en `videos`.

Cómo funciona una ejecución:

1. Se lee la idea del payload (`{"idea_id": 12}`).
2. Se elige el formato: el que sugirió la investigación si existe, y si no el
   configurado para el nicho (`config.script_format`).
3. Se monta el prompt con el formato, el tono del nicho y —si la base tiene
   alguno— los hooks y CTA que ya funcionaron en ese nicho.
4. El modelo escribe el guion, se criba la respuesta y se guarda la fila de
   `videos` en 'script_draft', lista para el checkpoint humano.

**Si el modelo no responde, o devuelve algo que no pasa la criba, el job falla**
y el motivo queda en `jobs.error`, que es donde lo ve el dashboard. Nunca se
rellena el guion con el texto de la idea ni con nada fabricado aquí: preferimos
cero guiones a guiones basura, igual que en `research/`.

Este módulo no importa de ningún otro módulo de dominio: entra y sale por la
base de datos (patrón blackboard).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from factory.core import config, llm
from factory.core.db import get_conn, transaction
from factory.core.models import Idea, Job, can_transition
from factory.core.queue import JobWorker, PermanentFailure, StopRequested, should_stop
from factory.script import formats

logger = logging.getLogger(__name__)

JOB_TYPE = "write_script"

# Estado en el que nace el guion: el humano lo aprueba en el checkpoint 1.
SCRIPT_STATUS = "script_draft"

# Ritmo de locución en español con el que se estiman las duraciones. Es una
# estimación de escritorio: la duración real solo se sabe cuando el hito 3
# sintetice el audio, y entonces se recalcula con la de verdad.
WORDS_PER_MINUTE = 145

# Lo que se le pide al modelo: 1000-1400 palabras son 7-10 minutos leídos. El
# suelo va por encima de los 6 minutos del objetivo porque el modelo se queda
# corto: pidiéndole 900 entregó 835 en la primera prueba contra la API real.
TARGET_MIN_WORDS = 1000
TARGET_MAX_WORDS = 1400
PROMPT_CHAPTERS = "6 a 8"

# Criba de la respuesta, más ancha que lo pedido: el modelo se pasa o se queda
# corto por poco a menudo, y el humano lo ajusta en el editor. Fuera de esta
# banda ya no es un guion de 6-10 minutos, es otra cosa, y el job falla.
MIN_WORDS = 700
MAX_WORDS = 2000
MIN_CHAPTERS = 3
MAX_CHAPTERS = 12
MIN_CHAPTER_WORDS = 40

# Un guion de 1400 palabras en español ronda los 2.500 tokens; con el JSON y los
# títulos alrededor, 8192 deja margen de sobra. Quedarse corto no da un error:
# da un JSON truncado, que el cliente rechaza como respuesta inservible.
MAX_OUTPUT_TOKENS = 8192

# Cuántos hooks y CTA aprendidos se le enseñan al modelo, como mucho.
KNOWLEDGE_PER_KIND = 4

# Estados de la idea desde los que se escribe un guion. 'used' se acepta de
# antemano: hoy nada lo pone —el dashboard todavía no encola guiones—, pero
# cuando lo cablee marcará la idea como usada al encolar el job, y sin 'used'
# aquí ningún guion llegaría a escribirse. Desde 'rejected' no se escribe: el
# humano ya dijo que no, y un guion nacido de ahí se cuela en el checkpoint como
# si nadie hubiera decidido nada.
WRITABLE_IDEA_STATUSES = frozenset({"approved", "used"})

# Un guion entero tarda más que las listas cortas de `research/`, así que se le
# da más margen que el minuto por defecto del cliente: un timeout dispara un
# reintento que es OTRA generación completa, hasta seis peticiones reales sin
# producir nada. **Estimación de escritorio, sin verificar contra la API**: no
# hay clave de Gemini en el entorno de desarrollo para medir cuánto tarda de
# verdad una respuesta de ~2.500 tokens.
LLM_TIMEOUT_SECONDS = 120.0

SYSTEM_PROMPT = (
    "Eres un guionista de video para YouTube en español. Escribes narración para"
    " leer en voz alta. Respondes únicamente con JSON válido, sin ningún texto"
    " alrededor."
)


# ---------------------------------------------------------------------------
# Handler de la cola
# ---------------------------------------------------------------------------


def register(worker: JobWorker) -> None:
    """Registra el handler de `write_script` en el worker de jobs."""
    worker.register(JOB_TYPE, handle_write_script)


def handle_write_script(job: Job) -> None:
    """Escribe el guion de la idea que venga en el payload."""
    idea_id = _idea_id(job.payload)
    conn = get_conn()
    idea = _load_idea(conn, idea_id)
    _assert_writable(idea)

    if _already_has_script(conn, idea_id):
        logger.warning(
            "La idea %d ya tiene un guion escrito: no se pide otro", idea_id
        )
        return

    format_id = _choose_format(idea)
    prompt = _script_prompt(idea, format_id, _knowledge(conn, idea.niche))
    if should_stop():
        # Antes de la llamada, que es lo caro: el job vuelve a la cola y lo
        # escribe el próximo arranque en vez de quedarse a medias. No consume
        # intento: no ha fallado nada, se le ha quitado el turno.
        raise StopRequested("parada pedida antes de llamar al modelo")

    respuesta = llm.generate_json(
        prompt,
        system=SYSTEM_PROMPT,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout=LLM_TIMEOUT_SECONDS,
    )
    guion = _validated_script(respuesta, format_id)

    video_id = _save_script(conn, idea, format_id, guion)
    if video_id is None:
        # La llamada ya está pagada y el guion se tira: si esto terminase en
        # 'done' nadie volvería a mirarlo. Sin reintento, porque lo que cerró la
        # puerta fue un humano moviendo el video, no un tropiezo.
        raise PermanentFailure(
            f"el video de la idea {idea_id} avanzó mientras se escribía el guion:"
            " se descarta el guion ya generado"
        )
    logger.info(
        "Idea %d -> video %d: guion %s de %d palabras en %d capitulos (~%.1f min)",
        idea_id, video_id, format_id, guion["word_count"],
        len(guion["chapters"]), guion["estimated_seconds"] / 60,
    )


def _idea_id(payload: dict[str, Any]) -> int:
    """Id de la idea del payload. `ValueError` si no viene o no es un entero.

    `True` se descarta a mano: en Python es un `int` que vale 1, así que un
    payload con `{"idea_id": true}` pasaría el filtro y escribiría el guion de
    la idea 1, que no tiene nada que ver.
    """
    valor = payload.get("idea_id")
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise ValueError(
            f"el payload de {JOB_TYPE} necesita un idea_id entero positivo: {payload!r}"
        )
    return valor


def _load_idea(conn: sqlite3.Connection, idea_id: int) -> Idea:
    """Idea de la base. `ValueError` si no existe: sin idea no hay guion."""
    fila = conn.execute(
        "SELECT id, title, niche, keyword, score_details, status, suggested_format"
        " FROM ideas WHERE id = ?",
        (idea_id,),
    ).fetchone()
    if fila is None:
        raise ValueError(f"la idea {idea_id} no existe")
    return Idea(
        id=int(fila["id"]),
        title=fila["title"],
        niche=fila["niche"],
        keyword=fila["keyword"],
        score_details=_score_details(fila["score_details"]),
        status=fila["status"],
        suggested_format=fila["suggested_format"],
    )


def _assert_writable(idea: Idea) -> None:
    """Corta antes de llamar al modelo si la idea ya no admite guion.

    Entre que se encola el job y que el worker lo coge, el humano puede haber
    rechazado la idea desde el dashboard —un job en backoff da minutos de
    ventana, y un "atrás" del navegador basta—. Sin esta guarda nacía un video
    en 'script_draft' de una idea rechazada y el job terminaba en 'done'.

    Sin reintento: el estado de una idea no vuelve solo, lo mueve una persona.
    """
    if idea.status not in WRITABLE_IDEA_STATUSES:
        raise PermanentFailure(
            f"la idea {idea.id} está en estado '{idea.status}' y no admite guion"
            f" (solo {sorted(WRITABLE_IDEA_STATUSES)})"
        )


def _score_details(crudo: str | None) -> dict[str, Any]:
    """Detalles del score de la idea, o {} si no se pueden leer.

    De aquí solo sale el ángulo del tema, que es opcional: un JSON ilegible en
    esa columna no es motivo para dejar la idea sin guion.
    """
    try:
        detalles = json.loads(crudo or "{}")
    except ValueError:
        logger.warning("score_details ilegible: el guion se escribe sin el angulo")
        return {}
    return detalles if isinstance(detalles, dict) else {}


def _choose_format(idea: Idea) -> str:
    """Formato del guion: el que sugirió la investigación, o el del nicho."""
    sugerido = idea.suggested_format
    if sugerido and formats.is_known(sugerido):
        return sugerido
    if sugerido:
        logger.warning(
            "La idea %s sugiere el formato desconocido %r: se usa el del nicho",
            idea.id, sugerido,
        )
    return config.script_format(idea.niche)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def _script_prompt(
    idea: Idea, format_id: str, knowledge: dict[str, list[str]]
) -> str:
    """Prompt completo del guion: nicho, formato, aprendizajes y forma del JSON."""
    niche: dict[str, Any] = config.niches().get(idea.niche, {})
    partes = [
        f"Nicho: {niche.get('name', idea.niche)}",
        f"Tono del canal: {niche.get('tone', 'neutro')}",
        f"Título del video: {idea.title}",
        *_angle_lines(idea),
        "",
        formats.instructions(format_id),
        "",
        *_knowledge_lines(knowledge),
        *_rules_lines(),
        "",
        "Devuelve exactamente este JSON:",
        '{"gancho": "...", "capitulos": [{"titulo": "...", "narracion": "..."}],'
        ' "cierre": "..."}',
    ]
    return "\n".join(partes)


def _angle_lines(idea: Idea) -> list[str]:
    """El ángulo que propuso la investigación, si la idea lo trae.

    Vive en `ideas.score_details.topic.angle` porque lo escribió el mismo modelo
    que propuso el título. Es la única pista de *de qué trata* el video más allá
    del título, así que cuando está se le pasa al guionista.
    """
    topic = idea.score_details.get("topic")
    angulo = topic.get("angle") if isinstance(topic, dict) else None
    if not isinstance(angulo, str) or not angulo.strip():
        return []
    return [f"De qué trata: {angulo.strip()}"]


def _knowledge_lines(knowledge: dict[str, list[str]]) -> list[str]:
    """Bloque de hooks y CTA aprendidos. Vacío si la base no tiene ninguno."""
    lineas: list[str] = []
    if knowledge["hook"]:
        lineas.append("Ganchos que funcionaron en este nicho (inspírate, no copies):")
        lineas += [f"- {texto}" for texto in knowledge["hook"]]
    if knowledge["cta"]:
        lineas.append("Llamadas a la acción que funcionaron en este nicho:")
        lineas += [f"- {texto}" for texto in knowledge["cta"]]
    return [*lineas, ""] if lineas else []


def _rules_lines() -> list[str]:
    """Las reglas que valen para cualquier formato."""
    return [
        "Reglas del guion:",
        f"- Escríbelo en {PROMPT_CHAPTERS} capítulos, más un gancho de apertura y"
        " un cierre.",
        f"- Entre {TARGET_MIN_WORDS} y {TARGET_MAX_WORDS} palabras en total,"
        " contando gancho y cierre: es un video de 7 a 10 minutos leído en voz"
        " alta, no de cinco.",
        "- Reparte ese total entre los capítulos: con 6 capítulos toca una media"
        " de 200 palabras cada uno; con 8, de 150. Es un presupuesto que hay que"
        " gastar, y ningún capítulo baja de 120 palabras: uno de 80 deja el"
        " video corto.",
        "- El gancho dura unos 15 segundos y da una razón concreta para quedarse.",
        "- La narración es prosa continua para locutar: frases cortas, sin listas,"
        " sin viñetas, sin markdown y sin emojis.",
        "- No escribas acotaciones, nombres de locutor, marcas de tiempo ni"
        " indicaciones de música o de imagen. Solo lo que se dice en voz alta.",
        "- El título de cada capítulo tiene de 2 a 6 palabras y sirve de marcador"
        " en la barra del video.",
        "- No inventes cifras, fechas, citas ni nombres propios que no puedas"
        " sostener. Si no tienes el dato, escribe la frase sin él.",
        "- Español neutro, sin modismos de un solo país.",
    ]


def _knowledge(conn: sqlite3.Connection, niche: str) -> dict[str, list[str]]:
    """Hooks y CTA del nicho que ya demostraron funcionar, los mejores primero.

    Hoy la tabla `knowledge` está vacía —la llena el cerebro del hito 5— y eso
    no es un problema: sin filas el prompt sale sin este bloque y el guion se
    escribe igual. Las filas sin `performance` van al final, no las primeras:
    "todavía no se ha medido" no es lo mismo que "funcionó bien".
    """
    filas = conn.execute(
        "SELECT kind, content FROM knowledge"
        " WHERE niche = ? AND kind IN ('hook', 'cta')"
        " ORDER BY performance IS NULL, performance DESC, id DESC",
        (niche,),
    ).fetchall()

    recogido: dict[str, list[str]] = {"hook": [], "cta": []}
    for fila in filas:
        textos = recogido[fila["kind"]]
        if len(textos) < KNOWLEDGE_PER_KIND:
            textos.append(str(fila["content"]))
    return recogido


# ---------------------------------------------------------------------------
# Criba de la respuesta del modelo
# ---------------------------------------------------------------------------


def _validated_script(respuesta: Any, format_id: str) -> dict[str, Any]:
    """Guion normalizado a partir de lo que devolvió el modelo.

    Lanza `ValueError` en cuanto algo no cuadra, y con eso el job falla y el
    motivo queda en `jobs.error`. No hay reparación ni relleno: un guion al que
    le falta la mitad de los capítulos no se arregla inventando los que faltan.
    """
    if not isinstance(respuesta, dict):
        raise ValueError(
            f"el modelo devolvió {type(respuesta).__name__}, se esperaba un objeto JSON"
        )

    gancho = _texto(respuesta.get("gancho"), "el gancho")
    cierre = _texto(respuesta.get("cierre"), "el cierre")
    capitulos = _chapters(respuesta.get("capitulos"))

    palabras = _count_words(gancho) + _count_words(cierre)
    palabras += sum(int(capitulo["words"]) for capitulo in capitulos)
    if not MIN_WORDS <= palabras <= MAX_WORDS:
        raise ValueError(
            f"el guion tiene {palabras} palabras, fuera de la banda aceptable"
            f" ({MIN_WORDS}-{MAX_WORDS}): no es un video de 6 a 10 minutos"
        )

    return {
        "format": format_id,
        "model": llm.MODEL,
        "generated_at": _rfc3339(datetime.now(timezone.utc)),
        "hook": gancho,
        "chapters": _with_timings(gancho, capitulos),
        "outro": cierre,
        "word_count": palabras,
        "words_per_minute": WORDS_PER_MINUTE,
        "estimated_seconds": _seconds(palabras),
    }


def _chapters(valor: Any) -> list[dict[str, Any]]:
    """Capítulos cribados, con su recuento de palabras. `ValueError` si no valen."""
    if not isinstance(valor, list):
        raise ValueError("el guion no trae una lista de capítulos")
    if not MIN_CHAPTERS <= len(valor) <= MAX_CHAPTERS:
        raise ValueError(
            f"el guion trae {len(valor)} capítulos; se aceptan entre"
            f" {MIN_CHAPTERS} y {MAX_CHAPTERS}"
        )

    capitulos: list[dict[str, Any]] = []
    for numero, item in enumerate(valor, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"el capítulo {numero} no es un objeto JSON")
        narracion = _texto(item.get("narracion"), f"la narración del capítulo {numero}")
        palabras = _count_words(narracion)
        if palabras < MIN_CHAPTER_WORDS:
            raise ValueError(
                f"el capítulo {numero} tiene {palabras} palabras (mínimo"
                f" {MIN_CHAPTER_WORDS}): es un capítulo de relleno"
            )
        capitulos.append(
            {
                "title": _texto(item.get("titulo"), f"el título del capítulo {numero}"),
                "narration": narracion,
                "words": palabras,
            }
        )
    return capitulos


def _with_timings(gancho: str, capitulos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Añade a cada capítulo el segundo en el que empieza, estimado.

    Las marcas salen del número de palabras y de `WORDS_PER_MINUTE`: la duración
    real solo se sabe cuando el hito 3 sintetice el audio, y ahí se recalculan.
    Sirven para ver de un vistazo si el guion se va de largo y para el listado
    de capítulos de la descripción.

    El primer capítulo empieza en 0 aunque el gancho se lea antes: YouTube exige
    que el primer marcador sea 00:00 y el gancho no es un capítulo aparte.
    """
    transcurrido = _seconds(_count_words(gancho))
    marcados: list[dict[str, Any]] = []
    for indice, capitulo in enumerate(capitulos):
        marcados.append({**capitulo, "start_sec": 0 if indice == 0 else transcurrido})
        transcurrido += _seconds(int(capitulo["words"]))
    return marcados


def _texto(valor: Any, campo: str) -> str:
    """Campo de texto obligatorio del modelo. `ValueError` si falta o va vacío."""
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError(f"falta {campo} en la respuesta del modelo, o no es texto")
    return valor.strip()


def _count_words(texto: str) -> int:
    """Palabras del texto, contadas como las cuenta un locutor: separadas por espacios."""
    return len(texto.split())


def _seconds(palabras: int) -> int:
    """Segundos que se tarda en leer ese número de palabras en voz alta."""
    return round(palabras * 60 / WORDS_PER_MINUTE)


def _rfc3339(momento: datetime) -> str:
    """datetime UTC a '2026-08-14T10:00:00Z'."""
    return momento.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


def _save_script(
    conn: sqlite3.Connection, idea: Idea, format_id: str, guion: dict[str, Any]
) -> int | None:
    """Escribe el guion en `videos`. Devuelve el id del video, o None si ya no cabe.

    Si la idea aún no tiene video, la fila nace directamente en 'script_draft':
    'idea_approved' solo existiría el tiempo de un INSERT y no lo vería nadie.
    Si ya lo tiene, se comprueba que la transición a 'script_draft' es legal
    antes de pisarlo: un video que ya se está produciendo o ya se publicó no
    admite un guion nuevo.

    La comprobación y la escritura van dentro de la misma transacción inmediata:
    son una operación, no dos.
    """
    with transaction(conn):
        fila = _existing_video(conn, idea.id)
        if fila is None:
            cursor = conn.execute(
                "INSERT INTO videos (idea_id, kind, format, title, chapters,"
                " script_json, status) VALUES (?, 'long', ?, ?, ?, ?, ?)",
                (
                    idea.id, format_id, idea.title, _chapter_list(guion["chapters"]),
                    json.dumps(guion, ensure_ascii=False), SCRIPT_STATUS,
                ),
            )
            return int(cursor.lastrowid)

        if not can_transition(fila["status"], SCRIPT_STATUS):
            logger.warning(
                "El video %d de la idea %d está en '%s': no admite un guion nuevo",
                fila["id"], idea.id, fila["status"],
            )
            return None

        conn.execute(
            "UPDATE videos SET format = ?, title = ?, chapters = ?, script_json = ?,"
            " status = ?, updated_at = datetime('now') WHERE id = ?",
            (
                format_id, idea.title, _chapter_list(guion["chapters"]),
                json.dumps(guion, ensure_ascii=False), SCRIPT_STATUS, fila["id"],
            ),
        )
        return int(fila["id"])


def _already_has_script(conn: sqlite3.Connection, idea_id: int) -> bool:
    """True si el video de esta idea ya no admite un guion nuevo.

    Se consulta antes de llamar al modelo para no gastar cuota en un guion que
    no se va a poder guardar: dos jobs encolados para la misma idea, un botón
    pulsado dos veces, un reintento a mano.
    """
    fila = _existing_video(conn, idea_id)
    return fila is not None and not can_transition(fila["status"], SCRIPT_STATUS)


def _existing_video(conn: sqlite3.Connection, idea_id: int | None) -> sqlite3.Row | None:
    """Fila del video largo de esta idea, si ya existe.

    Solo el largo: los shorts del hito 3 cuelgan de él por `parent_video_id` y
    no tienen guion propio.
    """
    return conn.execute(
        "SELECT id, status FROM videos WHERE idea_id = ? AND kind = 'long'"
        " ORDER BY id LIMIT 1",
        (idea_id,),
    ).fetchone()


def _chapter_list(capitulos: list[dict[str, Any]]) -> str:
    """Los capítulos con su marca de tiempo, en el formato que lee YouTube."""
    return "\n".join(
        f"{_mmss(int(capitulo['start_sec']))} {capitulo['title']}"
        for capitulo in capitulos
    )


def _mmss(segundos: int) -> str:
    """Segundos a 'MM:SS'. Los guiones son de minutos, nunca de horas."""
    return f"{segundos // 60:02d}:{segundos % 60:02d}"
