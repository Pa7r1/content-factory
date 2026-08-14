"""Orquestador del job `research_daily`: de keywords semilla a ideas puntuadas.

Cómo funciona una pasada de un nicho:

1. Se recogen una vez por nicho los datos que valen para todas sus keywords
   (posts de Reddit, CPM del nicho).
2. Por cada keyword semilla se consultan las cuatro fuentes. **Cada fuente va en
   su propio try/except**: si una cae, la señal queda ausente con su motivo
   escrito en `ideas.score_details` y el scorer re-normaliza pesos.
3. Con las señales se calcula el score. Los títulos ajenos (videos de YouTube,
   titulares) pasan por la criba de `candidates` y quedan como **material de
   contexto**: nunca se copian como idea. Un titular de otro canal describe el
   video de otro canal.
4. El LLM sintetiza **temas propios del nicho** a partir de ese material. Si el
   LLM no está disponible, esa keyword no produce ideas y el motivo queda en
   `research_log`: preferimos cero ideas a ideas basura.
5. Cada idea se hace upsert en `ideas` deduplicando por nicho + `dedupe_key`
   (el título normalizado, sin tildes ni puntuación): una segunda pasada
   actualiza la fila en estado 'new', no la duplica, y lo que el humano rechazó
   o usó no se vuelve a proponer aunque el modelo lo reformule.

El LLM decide **de qué trata** la idea, nunca **cuánto vale**: el score sale
entero de las señales medidas.

Este módulo no importa de ningún otro módulo de dominio: entra y sale por la
base de datos (patrón blackboard).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import fmean
from typing import Any, Callable

from factory.core import config, llm
from factory.core.db import get_conn, transaction
from factory.core.http_util import SourceUnavailable
from factory.core.models import Idea, Job
from factory.core.queue import JobWorker, should_stop
from factory.core.quota import QuotaExceeded
from factory.research import (
    candidates,
    news_source,
    reddit_source,
    scorer,
    wikipedia_source,
    youtube_source,
)

logger = logging.getLogger(__name__)

JOB_TYPE = "research_daily"

TOP_VIDEOS = 10
# Ventana de búsqueda en YouTube: 3 años. Suficiente para que la antigüedad
# mediana del top discrimine (el techo del scorer son 2 años) sin arrastrar
# éxitos de hace una década que ya no describen la competencia de hoy.
YOUTUBE_WINDOW_DAYS = 1095
# Cuánto material de contexto se le enseña al LLM por keyword. Más titulares no
# dan mejores temas: dan un prompt más caro y más ruido de portada.
VIDEO_TITLES_IN_PROMPT = 5
HEADLINES_IN_PROMPT = 5
IDEAS_PER_KEYWORD = 3
# Formatos que acepta `videos.format` en el esquema: el LLM no puede inventarse
# uno nuevo o el video que salga de la idea no se podrá grabar.
ALLOWED_FORMATS = ("misterio", "educativo", "storytelling", "top_n", "noticias")
# Pausa entre keywords: verificado en vivo que sondear ~19 keywords seguidas
# hace que la API de es.wikipedia empiece a responder 429. Un segundo de espera
# cuesta 19 segundos por pasada diaria y nos devuelve la señal evergreen.
PAUSE_BETWEEN_KEYWORDS = 1.0
# Estados desde los que una idea NO debe volver a proponerse cada mañana.
TERMINAL_STATUSES = ("rejected", "used")
# Keywords seguidas sin respuesta del modelo tras las que se corta la pasada.
# Cada keyword cuesta ~102 unidades de cuota de YouTube que se cobran ANTES de
# consultar al LLM: seguir sondeando 17 keywords más es gastar 1.700 unidades
# para no escribir ni una idea. Dos seguidas, y no una, porque un solo fallo
# puede ser un prompt bloqueado por filtros de seguridad, que es cosa de esa
# keyword y no del sistema.
MAX_LLM_FAILURES_IN_A_ROW = 2


@dataclass(frozen=True, slots=True)
class NicheContext:
    """Lo que se recoge una vez por nicho y sirve para todas sus keywords."""

    niche_id: str
    cpm_usd: float | None
    reddit_posts: list[dict[str, Any]] | None
    reddit_reason: str | None  # por qué no hay posts, si no los hay


@dataclass(slots=True)
class KeywordResearch:
    """Resultado de sondear una keyword: señales, ausencias y material de contexto."""

    signals: scorer.Signals
    raw: dict[str, Any] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    # Títulos ajenos ya cribados. Alimentan el prompt; nunca son la idea.
    material: dict[str, Any] = field(default_factory=dict)
    # Qué pasó con el LLM. `llm_asked` es False si ni siquiera había material
    # que enseñarle; `llm_answered` solo significa algo si se le preguntó.
    llm_asked: bool = False
    llm_answered: bool = False


@dataclass(frozen=True, slots=True)
class KeywordOutcome:
    """Lo que dejó una keyword: ideas escritas y qué hizo el modelo."""

    ideas_written: int
    llm_asked: bool
    llm_answered: bool
    llm_reason: str | None


@dataclass(slots=True)
class LlmHealth:
    """Cómo se está portando el modelo a lo largo de la pasada de un nicho.

    Sirve para dos decisiones que no se pueden tomar keyword a keyword: cortar
    la pasada cuando lleva varias seguidas sin responder, y hacer fallar el job
    si no respondió ni una sola vez —si no, un job que quemó toda la cuota de
    YouTube sin escribir una idea terminaría 'done' y nadie se enteraría.
    """

    asked: int = 0
    failed: int = 0
    in_a_row: int = 0
    last_reason: str | None = None

    def record(self, outcome: KeywordOutcome) -> None:
        """Apunta lo que pasó con el modelo en una keyword."""
        if not outcome.llm_asked:
            return
        self.asked += 1
        if outcome.llm_answered:
            self.in_a_row = 0
            return
        self.failed += 1
        self.in_a_row += 1
        self.last_reason = outcome.llm_reason

    @property
    def hopeless(self) -> bool:
        """Ya está claro que no responde: seguir sondeando gasta cuota para nada."""
        return self.in_a_row >= MAX_LLM_FAILURES_IN_A_ROW

    @property
    def never_answered(self) -> bool:
        """Se le preguntó y no respondió ni una sola vez."""
        return self.asked > 0 and self.failed == self.asked


# ---------------------------------------------------------------------------
# Handler de la cola
# ---------------------------------------------------------------------------


def register(worker: JobWorker) -> None:
    """Registra el handler de `research_daily` en el worker de jobs."""
    worker.register(JOB_TYPE, handle_research_daily)


def handle_research_daily(job: Job) -> None:
    """Investiga el nicho del payload, o todos los nichos si no viene ninguno."""
    disponibles = config.niches()
    pedido = job.payload.get("niche")
    if pedido is not None and pedido not in disponibles:
        raise ValueError(f"nicho desconocido en el payload: {pedido!r}")

    objetivos = [pedido] if pedido else list(disponibles)
    for niche_id in objetivos:
        research_niche(niche_id)


def research_niche(niche_id: str) -> int:
    """Sondea todas las keywords semilla de un nicho. Devuelve ideas escritas.

    Una keyword que revienta no tumba el nicho; que revienten TODAS sí hace
    fallar el job, porque entonces el problema no es una fuente sino el sistema.

    Escribir cero ideas NO es un fallo por sí solo: a las pocas semanas es lo
    normal, porque el humano ya descartó esas ideas y `_upsert_idea` no las
    repropone. Sí lo es no poder sondear ninguna keyword, y también sondearlas
    bien pero no poder sintetizar nunca: un modelo que no responde en ninguna
    keyword deja el nicho sin una sola idea después de haber gastado toda la
    cuota de YouTube de la pasada.
    """
    keywords: list[str] = config.niches()[niche_id].get("keywords", [])
    context = _niche_context(niche_id)
    # Títulos ya escritos en esta pasada: dos keywords del mismo nicho llegan a
    # proponer el mismo tema y entraría dos veces con scores distintos.
    vistos: set[str] = set()

    escritas = 0
    sondeadas = 0
    fallos: list[str] = []
    salud = LlmHealth()
    for indice, keyword in enumerate(keywords):
        if should_stop():
            logger.warning(
                "Nicho %s: parada pedida, se dejan %d keywords sin sondear",
                niche_id, len(keywords) - indice,
            )
            break
        if indice > 0:
            time.sleep(PAUSE_BETWEEN_KEYWORDS)
        try:
            resultado = _research_keyword(context, keyword, vistos)
        except Exception as exc:  # una keyword rota no cancela el resto del nicho
            logger.exception("Keyword %r del nicho %s falló", keyword, niche_id)
            fallos.append(f"{keyword}: {type(exc).__name__}: {exc}")
            continue

        sondeadas += 1
        escritas += resultado.ideas_written
        salud.record(resultado)
        if salud.hopeless:
            logger.error(
                "Nicho %s: el modelo lleva %d keywords sin responder, se corta la"
                " pasada para no gastar mas cuota de YouTube en balde",
                niche_id, salud.in_a_row,
            )
            break

    _check_pass_outcome(niche_id, sondeadas, fallos, salud)
    logger.info(
        "Nicho %s: %d ideas escritas de %d keywords sondeadas (%d con fallo)",
        niche_id, escritas, sondeadas, len(fallos),
    )
    return escritas


def _check_pass_outcome(
    niche_id: str, sondeadas: int, fallos: list[str], salud: LlmHealth
) -> None:
    """Hace fallar el job si la pasada no pudo dar ningún resultado útil.

    Los dos casos son distintos y los dos terminaban antes en 'done': ninguna
    keyword sondeada (todas reventaron) y ninguna keyword sintetizada (el modelo
    no respondió nunca). Un job en 'done' sin ideas es invisible; uno en
    'failed' se ve en el dashboard el mismo día.
    """
    if fallos and sondeadas == 0:
        raise RuntimeError(
            f"nicho {niche_id}: no se pudo sondear ninguna keyword — {' | '.join(fallos)}"
        )
    if salud.never_answered:
        raise RuntimeError(
            f"nicho {niche_id}: el modelo no respondió en ninguna de las"
            f" {salud.asked} keywords consultadas — {salud.last_reason}"
        )


# ---------------------------------------------------------------------------
# Recogida de señales
# ---------------------------------------------------------------------------


def _niche_context(niche_id: str) -> NicheContext:
    """Prepara los datos comunes del nicho (Reddit una sola vez, CPM)."""
    posts, motivo = _niche_reddit_posts(niche_id)
    return NicheContext(
        niche_id=niche_id,
        cpm_usd=config.cpm_for_niche(niche_id),
        reddit_posts=posts,
        reddit_reason=motivo,
    )


def _niche_reddit_posts(niche_id: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Posts de Reddit del nicho y, si no los hay, el motivo.

    Va por `_try_source` como las otras cuatro fuentes: aquí se capturaba solo
    `SourceUnavailable`, así que un campo inesperado en la respuesta (un `ups`
    nulo) tumbaba el nicho entero con sus tres reintentos.
    """
    subreddits = config.subreddits(niche_id)
    if not subreddits:
        return None, "el nicho no tiene subreddits configurados"

    reasons: dict[str, str] = {}
    posts = _try_source(
        "reddit", lambda: reddit_source.collect_top_posts(subreddits), reasons
    )
    return posts, reasons.get("reddit")


def collect_keyword_signals(context: NicheContext, keyword: str) -> KeywordResearch:
    """Consulta las cuatro fuentes para una keyword: señales + material cribado."""
    reasons: dict[str, str] = {}

    youtube = _try_source("youtube", lambda: _youtube_research(keyword), reasons)
    news = _try_source("news", lambda: news_source.momentum_signal(keyword), reasons)
    wiki = _try_source("wikipedia", lambda: wikipedia_source.demand_signal(keyword), reasons)
    reddit = _reddit_research(context, keyword, reasons)

    if news is not None and news["last_7d"] is None:
        reasons["news"] = (
            "hay titulares del tema pero ninguno trae fecha: sin fechas no se"
            " puede medir el momentum y un 0 sería un dato inventado"
        )
    if context.cpm_usd is None:
        reasons["cpm"] = f"no hay CPM configurado para el nicho {context.niche_id}"

    return KeywordResearch(
        signals=_build_signals(youtube, news, wiki, reddit, context.cpm_usd),
        raw=_raw_snapshot(youtube, news, wiki, reddit, context.cpm_usd),
        reasons=reasons,
        material=_prompt_material(youtube, news, wiki),
    )


def _prompt_material(
    youtube: dict[str, Any] | None,
    news: dict[str, Any] | None,
    wiki: dict[str, Any] | None,
) -> dict[str, Any]:
    """Lo que se le enseña al LLM como contexto, ya pasado por la criba.

    Son pruebas de que el tema tiene demanda, no plantillas de título: por eso
    la basura (música, Shorts, otro idioma) se cae aquí, antes de gastar la
    llamada, y no después con una idea inservible ya escrita en la base.
    """
    videos = youtube["videos"] if youtube else []
    headlines = news["headlines"] if news else []
    return {
        "video_titles": candidates.usable_video_titles(videos, VIDEO_TITLES_IN_PROMPT),
        "headlines": candidates.usable_headlines(headlines, HEADLINES_IN_PROMPT),
        "wikipedia_article": wiki["article"] if wiki else None,
    }


def _build_signals(
    youtube: dict[str, Any] | None,
    news: dict[str, Any] | None,
    wiki: dict[str, Any] | None,
    reddit: dict[str, Any] | None,
    cpm_usd: float | None,
) -> scorer.Signals:
    """Empaqueta lo recogido en la entrada que espera el scorer."""
    videos = youtube["videos"] if youtube else []
    subs = [v["subscribers"] for v in videos if v["subscribers"] is not None]
    edades = [v["age_days"] for v in videos if v["age_days"] is not None]
    return scorer.Signals(
        top_video_views=[v["views"] for v in videos] or None,
        top_channel_subs=subs or None,
        top_video_ages_days=edades or None,
        news_last_7d=news["last_7d"] if news else None,
        news_last_30d=news["last_30d"] if news else None,
        reddit_engagement=reddit["engagement"] if reddit else None,
        wikipedia_monthly_views=wiki["monthly_views"] if wiki else None,
        cpm_usd=cpm_usd,
    )


def _raw_snapshot(
    youtube: dict[str, Any] | None,
    news: dict[str, Any] | None,
    wiki: dict[str, Any] | None,
    reddit: dict[str, Any] | None,
    cpm_usd: float | None,
) -> dict[str, Any]:
    """Foto de las señales crudas que se guarda en `ideas.score_details`.

    Es lo que permite responder desde el dashboard "¿por qué esta idea tiene
    este número?" sin volver a llamar a ninguna API.
    """
    raw: dict[str, Any] = {"cpm_usd": cpm_usd}
    if youtube:
        videos = youtube["videos"]
        raw["youtube"] = {
            "videos_encontrados": len(videos),
            "vistas": [v["views"] for v in videos],
            "subs_canales": [v["subscribers"] for v in videos],
            "antiguedad_dias": [_redondear(v["age_days"]) for v in videos],
        }
    if news:
        raw["news"] = {
            "last_7d": news["last_7d"],
            "last_30d": news["last_30d"],
            "titulares": news["headlines"][:5],
        }
    if wiki:
        raw["wikipedia"] = {
            "articulo": wiki["article"],
            "vistas_mensuales": wiki["monthly_views"],
        }
    if reddit:
        raw["reddit"] = {
            "engagement": reddit["engagement"],
            "posts": reddit["matched_posts"][:5],
        }
    return raw


def _try_source(
    name: str, fetch: Callable[[], Any], reasons: dict[str, str]
) -> Any | None:
    """Consulta una fuente; si falla o no trae datos, apunta el motivo y sigue.

    Ninguna fuente puede tumbar la investigación: una señal ausente con su
    motivo registrado vale infinitamente más que un job que revienta a las 7:30
    y nadie ve hasta tres días después.
    """
    try:
        resultado = fetch()
    except (SourceUnavailable, QuotaExceeded) as exc:
        reasons[name] = f"{type(exc).__name__}: {exc}"
        logger.info("Señal %s ausente: %s", name, exc)
        return None
    except Exception as exc:  # bug o fallo no previsto de la fuente
        reasons[name] = f"error inesperado: {type(exc).__name__}: {exc}"
        logger.exception("La fuente %s falló de forma inesperada", name)
        return None

    if resultado is None:
        reasons[name] = "la fuente respondió pero no tiene datos de esta keyword"
    return resultado


def _reddit_research(
    context: NicheContext, keyword: str, reasons: dict[str, str]
) -> dict[str, Any] | None:
    """Engagement de la keyword en los posts ya descargados para el nicho."""
    if context.reddit_posts is None:
        reasons["reddit"] = context.reddit_reason or "sin posts de Reddit"
        return None
    return reddit_source.engagement_in_posts(context.reddit_posts, keyword)


def _youtube_research(keyword: str) -> dict[str, Any] | None:
    """Top de videos de la keyword con vistas, subs del canal y antigüedad.

    Coste: 102 unidades de cuota (search.list 100 + videos.list + channels.list).
    """
    published_after = _rfc3339(
        datetime.now(timezone.utc) - timedelta(days=YOUTUBE_WINDOW_DAYS)
    )
    resultados = youtube_source.search_videos(keyword, published_after, TOP_VIDEOS)
    if not resultados:
        return None

    detalles = youtube_source.videos_details([r["video_id"] for r in resultados])
    canales = youtube_source.channels_details(
        [r["channel_id"] for r in resultados if r["channel_id"]]
    )
    videos = _merge_youtube(resultados, detalles, canales)
    if not videos:
        return None

    media_vistas = fmean([v["views"] for v in videos])
    for video in videos:
        video["outlier_ratio"] = video["views"] / media_vistas if media_vistas > 0 else 0.0
    return {"videos": videos}


def _merge_youtube(
    resultados: list[dict[str, Any]],
    detalles: dict[str, dict[str, Any]],
    canales: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cruza búsqueda + estadísticas + canal en una fila por video."""
    ahora = datetime.now(timezone.utc)
    videos: list[dict[str, Any]] = []
    for resultado in resultados:
        detalle = detalles.get(resultado["video_id"])
        if detalle is None:
            continue
        canal = canales.get(resultado["channel_id"] or "", {})
        videos.append(
            {
                "video_id": resultado["video_id"],
                "title": detalle["title"] or resultado["title"],
                "views": detalle["views"],
                "duration_sec": detalle["duration_sec"],
                "channel_id": resultado["channel_id"],
                "channel_title": resultado["channel_title"],
                "subscribers": canal.get("subscribers"),
                "age_days": _age_days(detalle["published_at"], ahora),
            }
        )
    return videos


# ---------------------------------------------------------------------------
# Síntesis de temas con el LLM
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Eres un estratega de contenido para canales de YouTube en español. "
    "Respondes únicamente con JSON válido, sin ningún texto alrededor."
)


def _synthesize_topics(
    context: NicheContext, keyword: str, research: KeywordResearch
) -> list[dict[str, Any]]:
    """Temas de video propios del nicho, sintetizados por el LLM.

    Devuelve [] si no hay material que enseñarle, si el LLM se cae o si lo que
    devuelve no es utilizable, y en los tres casos deja el motivo en
    `research.reasons`. **Nunca cae de vuelta a copiar títulos ajenos**: cero
    ideas es un resultado aceptable; una idea basura ya escrita en la base, no.
    """
    if not _hay_material(research.material):
        research.reasons["llm"] = "ninguna fuente dio material para sintetizar temas"
        return []

    prompt = _topics_prompt(context, keyword, research.material)
    respuesta = _ask_llm(prompt, research)
    if not research.llm_answered:
        return []

    temas = _valid_topics(respuesta, _material_keys(research.material))
    if not temas:
        research.reasons["llm"] = "el modelo respondió pero ningún tema pasó la criba"
    return temas


def _ask_llm(prompt: str, research: KeywordResearch) -> Any:
    """Pide los temas al modelo y deja constancia de si llegó a responder.

    No va por `_try_source` como las cuatro fuentes porque su fallo tiene otra
    consecuencia: una señal ausente re-normaliza pesos y la keyword sigue
    puntuando, pero un modelo que no responde deja la keyword sin ninguna idea,
    y si no responde en ninguna la pasada entera no sirve de nada
    (`research_niche` la corta y hace fallar el job).
    """
    research.llm_asked = True
    try:
        respuesta = llm.generate_json(prompt, system=SYSTEM_PROMPT)
    except (SourceUnavailable, QuotaExceeded) as exc:
        research.reasons["llm"] = f"{type(exc).__name__}: {exc}"
        logger.info("El modelo no respondió: %s", exc)
        return None
    except Exception as exc:  # bug o fallo no previsto del cliente
        research.reasons["llm"] = f"error inesperado: {type(exc).__name__}: {exc}"
        logger.exception("La llamada al modelo falló de forma inesperada")
        return None

    research.llm_answered = True
    return respuesta


def _material_keys(material: dict[str, Any]) -> set[str]:
    """Claves de deduplicación de los títulos ajenos que se le enseñaron.

    El prompt prohíbe copiarlos, pero un prompt no es una garantía: un título
    copiado tal cual entraría en `ideas` marcado como propio y ya habría pasado
    esta misma criba, porque de ahí salió.
    """
    ajenos = [*material["video_titles"], *material["headlines"]]
    return {candidates.dedupe_key(titulo) for titulo in ajenos}


def _hay_material(material: dict[str, Any]) -> bool:
    """¿Queda alguna señal real que enseñarle al modelo?

    Sin material, el modelo solo tendría la keyword y se inventaría el tema
    entero. Eso no es investigación: es gastar cuota para escribir ficción.
    """
    return bool(
        material["video_titles"]
        or material["headlines"]
        or material["wikipedia_article"]
    )


def _topics_prompt(context: NicheContext, keyword: str, material: dict[str, Any]) -> str:
    """Prompt de síntesis: el material va como prueba de demanda, no como molde."""
    niche = config.niches()[context.niche_id]
    partes = [
        f"Nicho: {niche.get('name', context.niche_id)}",
        f"Tono del canal: {niche.get('tone', 'neutro')}",
        f"Tema sonda: {keyword}",
        "",
        "Material medido hoy para este tema. Es la PRUEBA de que hay demanda,",
        "no una plantilla: son títulos y titulares de otros canales y medios,",
        "y copiarlos o parafrasearlos está prohibido.",
        *_material_block(material),
        "",
        f"Propón {IDEAS_PER_KEYWORD} temas de video ORIGINALES para este canal.",
        "Reglas:",
        "- En español neutro, pensados para un video de 8 a 12 minutos.",
        "- No copies, traduzcas ni parafrasees los títulos de arriba.",
        "- Nada de música, videoclips, ni contenido que dependa de una persona"
        " famosa concreta.",
        "- Sin emojis, sin hashtags y sin mayúsculas sostenidas.",
        "- Título de entre 40 y 90 caracteres.",
        "",
        'Devuelve exactamente este JSON: {"temas": [{"titulo": "...",'
        ' "angulo": "una frase de qué trata", "formato": "..."}]}',
        f"El campo formato debe ser uno de: {', '.join(ALLOWED_FORMATS)}.",
    ]
    return "\n".join(partes)


def _material_block(material: dict[str, Any]) -> list[str]:
    """Las líneas del prompt que enumeran el material ya cribado."""
    lineas: list[str] = []
    if material["video_titles"]:
        lineas.append("Videos que destacan en YouTube:")
        lineas += [f"- {titulo}" for titulo in material["video_titles"]]
    if material["headlines"]:
        lineas.append("Titulares de noticias recientes:")
        lineas += [f"- {titular}" for titular in material["headlines"]]
    if material["wikipedia_article"]:
        lineas.append(f"Artículo de Wikipedia del tema: {material['wikipedia_article']}")
    return lineas


def _valid_topics(respuesta: Any, prohibidos: set[str]) -> list[dict[str, Any]]:
    """Temas del LLM que pasan la criba, deduplicados y acotados en número.

    La misma criba determinista que se aplica al material de entrada se aplica
    aquí a la salida: el modelo también devuelve emojis y titulos de tres
    palabras. `prohibidos` son las claves del material que vio: un título ajeno
    devuelto tal cual no es una idea propia, por mucho que pase la criba.
    """
    temas: list[dict[str, Any]] = []
    claves: set[str] = set()
    for item in _topic_items(respuesta):
        titulo = item.get("titulo") if isinstance(item, dict) else None
        if not isinstance(titulo, str) or not candidates.is_usable_title(titulo):
            continue
        clave = candidates.dedupe_key(titulo)
        if clave in prohibidos:
            logger.info("El modelo copió un título del material: %r", titulo)
            continue
        if clave in claves:
            continue
        claves.add(clave)
        temas.append(
            {
                "origin": "llm",
                "model": llm.MODEL,
                "title": candidates.clean_title(titulo),
                "angle": _texto_corto(item.get("angulo")),
                "suggested_format": _valid_format(item.get("formato")),
            }
        )
        if len(temas) >= IDEAS_PER_KEYWORD:
            break
    return temas


def _topic_items(respuesta: Any) -> list[Any]:
    """La lista de temas de la respuesta, venga envuelta en `temas` o suelta."""
    if isinstance(respuesta, dict):
        temas = respuesta.get("temas")
        return temas if isinstance(temas, list) else []
    return respuesta if isinstance(respuesta, list) else []


def _valid_format(formato: Any) -> str | None:
    """Formato sugerido si es uno de los que acepta el esquema; si no, None."""
    if isinstance(formato, str) and formato.strip().lower() in ALLOWED_FORMATS:
        return formato.strip().lower()
    return None


def _texto_corto(valor: Any) -> str | None:
    """Campo de texto libre del modelo, limpio y acotado."""
    if not isinstance(valor, str) or not valor.strip():
        return None
    return candidates.clean_title(valor)


# ---------------------------------------------------------------------------
# De señales a ideas
# ---------------------------------------------------------------------------


def _research_keyword(
    context: NicheContext, keyword: str, vistos: set[str]
) -> KeywordOutcome:
    """Sondea una keyword, escribe sus ideas y cuenta qué pasó con el modelo."""
    research = collect_keyword_signals(context, keyword)
    resultado = scorer.score_idea(research.signals, config.score_weights())
    topics = _synthesize_topics(context, keyword, research)
    ideas = _build_ideas(context.niche_id, keyword, research, resultado, topics)

    nuevas = _sin_repetidas(ideas, vistos)
    conn = get_conn()
    escritas = 0
    for idea in nuevas:
        if _upsert_idea(conn, idea) is not None:
            escritas += 1

    motivos = dict(research.reasons)
    if ideas and not nuevas:
        motivos["dedupe"] = "todos los temas ya se habían propuesto en esta pasada"
    _log_research(conn, context.niche_id, keyword, resultado, motivos, escritas)
    logger.info(
        "%s / %r -> score %.1f (faltan: %s) -> %d ideas",
        context.niche_id, keyword, resultado.score,
        ", ".join(resultado.missing) or "nada", escritas,
    )
    return KeywordOutcome(
        ideas_written=escritas,
        llm_asked=research.llm_asked,
        llm_answered=research.llm_answered,
        llm_reason=research.reasons.get("llm"),
    )


def _sin_repetidas(ideas: list[Idea], vistos: set[str]) -> list[Idea]:
    """Ideas cuyo título no haya salido ya en esta pasada del nicho.

    `vistos` se actualiza aquí: dos keywords distintas del mismo nicho llegan al
    mismo tema y la segunda entraría con otro score, que es justo lo que pasó
    con "¿Cómo Afrontar Los Problemas?" en la primera pasada real.
    """
    nuevas: list[Idea] = []
    for idea in ideas:
        clave = candidates.dedupe_key(idea.title)
        if clave in vistos:
            logger.debug("Idea %r repetida en esta pasada: no se escribe", idea.title)
            continue
        vistos.add(clave)
        nuevas.append(idea)
    return nuevas


def _build_ideas(
    niche_id: str,
    keyword: str,
    research: KeywordResearch,
    resultado: scorer.ScoreResult,
    topics: list[dict[str, Any]],
) -> list[Idea]:
    """Convierte los temas de una keyword en filas de `ideas` listas para grabar.

    Todos los temas de una keyword comparten su score: las señales miden el
    tema-sonda, y el LLM decide de qué trata la idea, no cuánto vale. Lo que los
    diferencia (ángulo, formato) va en `score_details.topic`.

    Sin temas no hay ideas: la keyword se queda sin proponer nada y el motivo
    está en `research_log`.
    """
    base = {
        "signals": research.raw,
        "sub_metrics": resultado.sub_metrics,
        "components": resultado.components,
        "weights_used": resultado.weights_used,
        "missing_signals": resultado.missing,
        "missing_reasons": research.reasons,
        "material": research.material,
        "collected_at": _rfc3339(datetime.now(timezone.utc)),
    }

    ideas: list[Idea] = []
    for topic in topics:
        ideas.append(
            Idea(
                title=topic["title"],
                niche=niche_id,
                keyword=keyword,
                source=topic["origin"],
                demand=resultado.components["demand"],
                competition=resultado.components["competition"],
                evergreen=resultado.components["evergreen"],
                cpm_factor=resultado.components["cpm"],
                score=resultado.score,
                score_details={**base, "topic": topic},
                suggested_format=topic.get("suggested_format"),
            )
        )
    return ideas


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


def _upsert_idea(conn: sqlite3.Connection, idea: Idea) -> int | None:
    """Inserta la idea, o actualiza la equivalente que siga en estado 'new'.

    Equivalente = mismo nicho y misma `dedupe_key`, **sin mirar la keyword**.
    Dos keywords del mismo nicho llegan al mismo tema, y ahí no hay dos
    oportunidades: hay una, medida dos veces. Es además la única regla coherente
    con el dedupe de dentro de la pasada, que ya es por nicho.

    Emparejar por título exacto era inoperante desde que los títulos los escribe
    un modelo: "El efecto dominó de una pequeña elección." entraba como fila
    nueva teniendo al lado "El efecto dominó de una pequeña elección" rechazada,
    y el checkpoint humano dejaba de proteger nada.

    El título de la fila NO se reescribe: la clave dice que es la misma idea, y
    cambiarle la redacción cada mañana le mueve el sitio al humano que la está
    mirando. Lo que se refresca es la medición.

    Devuelve el id de la idea, o None si se omitió por estar ya decidida.
    """
    clave = candidates.dedupe_key(idea.title)
    detalles = json.dumps(idea.score_details, ensure_ascii=False, default=str)
    with transaction(conn):
        fila = conn.execute(
            """
            SELECT id, status FROM ideas
             WHERE niche = ? AND dedupe_key = ?
             ORDER BY CASE status WHEN 'new' THEN 1 ELSE 0 END, id DESC
             LIMIT 1
            """,
            (idea.niche, clave),
        ).fetchone()

        if fila is not None and fila["status"] in TERMINAL_STATUSES:
            logger.debug("Idea %r ya está en estado %s: no se repropone", idea.title, fila["status"])
            return None

        if fila is not None and fila["status"] == "new":
            conn.execute(
                """
                UPDATE ideas
                   SET keyword = ?, source = ?, demand = ?, competition = ?,
                       evergreen = ?, cpm_factor = ?, score = ?, score_details = ?,
                       suggested_format = ?, updated_at = datetime('now')
                 WHERE id = ?
                """,
                (
                    idea.keyword, idea.source, idea.demand, idea.competition,
                    idea.evergreen, idea.cpm_factor, idea.score, detalles,
                    idea.suggested_format, fila["id"],
                ),
            )
            return int(fila["id"])

        if fila is not None:
            # Está shortlisted o approved: el humano ya la movió, no se toca.
            logger.debug("Idea %r en estado %s: no se modifica", idea.title, fila["status"])
            return None

        cursor = conn.execute(
            """
            INSERT INTO ideas (title, niche, keyword, dedupe_key, source, demand,
                               competition, evergreen, cpm_factor, score,
                               score_details, status, suggested_format)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                idea.title, idea.niche, idea.keyword, clave, idea.source, idea.demand,
                idea.competition, idea.evergreen, idea.cpm_factor, idea.score,
                detalles, idea.suggested_format,
            ),
        )
    return int(cursor.lastrowid)


def _log_research(
    conn: sqlite3.Connection,
    niche_id: str,
    keyword: str,
    resultado: scorer.ScoreResult,
    reasons: dict[str, str],
    escritas: int,
) -> None:
    """Deja constancia en la base de qué pasó con esta keyword.

    Una keyword que no produce ideas no deja ninguna fila en `ideas`, así que
    sin esto el motivo (LLM caído, sin material, nada que pasara la criba) solo
    viviría en el log. En un sistema desatendido, "por qué esta keyword no
    propuso nada hoy" tiene que poder responderse desde la base.
    """
    with transaction(conn):
        conn.execute(
            "INSERT INTO research_log (niche, keyword, score, ideas_written, reasons)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                niche_id,
                keyword,
                resultado.score,
                escritas,
                json.dumps(reasons, ensure_ascii=False),
            ),
        )


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _redondear(valor: float | None) -> float | None:
    """Redondea a un decimal para el snapshot, conservando los None."""
    return None if valor is None else round(valor, 1)


def _age_days(published_at: str | None, ahora: datetime) -> float | None:
    """Días desde la publicación, o None si la fecha no viene o no se puede leer.

    None, no 0.0: un 0.0 diría "recién subido", hundiría `age_norm` y contaría
    como saturación del top. Una fecha ilegible es una señal ausente.
    """
    if not published_at:
        return None
    try:
        fecha = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Fecha ilegible de YouTube: %r", published_at)
        return None
    return max((ahora - fecha).total_seconds() / 86400, 0.0)


def _rfc3339(momento: datetime) -> str:
    """datetime UTC → '2026-08-06T10:00:00Z', el formato que pide YouTube."""
    return momento.strftime("%Y-%m-%dT%H:%M:%SZ")
