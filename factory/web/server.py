"""Rutas del dashboard: ranking de ideas, checkpoint del guion y estado del sistema.

Cuatro vistas y seis acciones, todas con formularios POST clásicos y redirect
(patrón POST/Redirect/GET): el dashboard funciona sin una línea de JavaScript,
que es lo que quieres cuando lo abres a las 7 de la mañana desde el móvil.

Todos los endpoints son `def`, no `async def`: sqlite3 es bloqueante y con `def`
Starlette los manda al threadpool de anyio en vez de parar el bucle de eventos.

El SQL vive en `queries.py`; aquí solo se arma el contexto de la plantilla.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from factory.core import db, queue, scheduler
from factory.web import queries

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

# Jobs que se muestran en la vista de sistema. Suficientes para ver la pasada
# de esta mañana entera (un job por nicho) y las de días anteriores.
RECENT_JOBS_LIMIT = 25

# Redirect tras un POST de formulario: 303 obliga al navegador a hacer GET, así
# que recargar la página no repite la acción.
SEE_OTHER = 303


# ---------------------------------------------------------------------------
# Vista: Ideas
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def ideas_view(request: Request, mensaje: str | None = None) -> HTMLResponse:
    """Ranking de ideas pendientes de decisión, de mayor a menor score."""
    conn = db.get_conn()
    return templates.TemplateResponse(
        request,
        "ideas.html",
        {
            "activa": "ideas",
            "ideas": queries.ranked_ideas(conn),
            "totales": queries.count_ideas_by_status(conn),
            "mensaje": mensaje,
        },
    )


@router.post("/ideas/{idea_id}/approve")
def approve_idea(idea_id: int) -> RedirectResponse:
    """Aprueba una idea y encola su guion, en una sola transacción.

    El guion lo escribe el worker, que tarda minutos: aquí se encola y se vuelve
    al ranking. Aprobar dos veces la misma idea no encola dos guiones (el dedupe
    está en `queries.approve_idea_for_script`); el segundo intento da 409.
    """
    try:
        aprobada = queries.approve_idea_for_script(db.get_conn(), idea_id)
    except (queries.IdeaNotFound, queries.IdeaLocked) as exc:
        raise _error_de_idea(exc) from exc
    return _volver_a_ideas(
        f"Idea aprobada: {aprobada.title} - guion en cola (job {aprobada.job_id})"
    )


@router.post("/ideas/{idea_id}/reject")
def reject_idea(idea_id: int) -> RedirectResponse:
    """Descarta una idea: no vuelve a proponerse en las pasadas siguientes."""
    try:
        titulo = queries.update_idea_status(
            db.get_conn(), idea_id, queries.REJECTED_STATUS
        )
    except (queries.IdeaNotFound, queries.IdeaLocked) as exc:
        raise _error_de_idea(exc) from exc
    return _volver_a_ideas(f"Idea descartada: {titulo}")


def _error_de_idea(exc: queries.IdeaNotFound | queries.IdeaLocked) -> HTTPException:
    """404 si la idea no existe; 409 si su estado ya no admite la decisión."""
    codigo = 404 if isinstance(exc, queries.IdeaNotFound) else 409
    return HTTPException(status_code=codigo, detail=str(exc))


# ---------------------------------------------------------------------------
# Vista: Guiones (el checkpoint humano)
# ---------------------------------------------------------------------------


@router.get("/scripts", response_class=HTMLResponse)
def scripts_view(request: Request, mensaje: str | None = None) -> HTMLResponse:
    """Los guiones que esperan que alguien los lea y decida."""
    return templates.TemplateResponse(
        request,
        "scripts.html",
        {
            "activa": "guiones",
            "guiones": queries.scripts_awaiting_review(db.get_conn()),
            "mensaje": mensaje,
        },
    )


@router.get("/scripts/{video_id}", response_class=HTMLResponse)
def script_view(
    request: Request, video_id: int, mensaje: str | None = None
) -> HTMLResponse:
    """Un guion entero, en el editor por capítulos."""
    try:
        guion = queries.script_for_review(db.get_conn(), video_id)
    except (queries.ScriptNotFound, queries.ScriptLocked) as exc:
        raise _error_de_guion(exc) from exc
    return templates.TemplateResponse(
        request,
        "script.html",
        {"activa": "guiones", "guion": guion, "mensaje": mensaje},
    )


@router.post("/scripts/{video_id}/save")
def save_script(
    video_id: int,
    gancho: str = Form(""),
    cierre: str = Form(""),
    titulo: list[str] = Form([]),
    narracion: list[str] = Form([]),
) -> RedirectResponse:
    """Guarda lo escrito sin decidir: el guion se queda en el checkpoint."""
    try:
        edicion = _edicion_del_formulario(gancho, cierre, titulo, narracion)
        titulo_video = queries.save_script_draft(db.get_conn(), video_id, edicion)
    except (queries.ScriptNotFound, queries.ScriptLocked, queries.ScriptInvalid) as exc:
        raise _error_de_guion(exc) from exc
    return _volver_al_guion(video_id, f"Cambios guardados: {titulo_video}")


@router.post("/scripts/{video_id}/approve")
def approve_script(
    video_id: int,
    gancho: str = Form(""),
    cierre: str = Form(""),
    titulo: list[str] = Form([]),
    narracion: list[str] = Form([]),
) -> RedirectResponse:
    """Guarda lo escrito y aprueba el guion, en una sola transacción.

    Aprobar dos veces el mismo guion da 409: el segundo intento se encuentra el
    video fuera de 'script_draft' (el dedupe está en `queries.approve_script`).
    """
    try:
        edicion = _edicion_del_formulario(gancho, cierre, titulo, narracion)
        titulo_video = queries.approve_script(db.get_conn(), video_id, edicion)
    except (queries.ScriptNotFound, queries.ScriptLocked, queries.ScriptInvalid) as exc:
        raise _error_de_guion(exc) from exc
    return _volver_a_guiones(f"Guion aprobado: {titulo_video}")


@router.post("/scripts/{video_id}/reject")
def reject_script(video_id: int) -> RedirectResponse:
    """Descarta el guion: ese video no se produce."""
    try:
        titulo_video = queries.reject_script(db.get_conn(), video_id)
    except (queries.ScriptNotFound, queries.ScriptLocked) as exc:
        raise _error_de_guion(exc) from exc
    return _volver_a_guiones(f"Guion descartado: {titulo_video}")


def _edicion_del_formulario(
    gancho: str, cierre: str, titulos: list[str], narraciones: list[str]
) -> queries.ScriptEdit:
    """Los campos del editor emparejados capítulo a capítulo, ya limpios.

    Los títulos y las narraciones llegan como dos listas paralelas porque el
    formulario repite el mismo `name` una vez por capítulo, y el navegador las
    manda en el orden en el que están en el HTML. Si no vienen a pares, el
    formulario llegó roto y no se guarda nada.
    """
    if len(titulos) != len(narraciones):
        raise queries.ScriptInvalid(
            "el formulario llegó con capítulos incompletos: no se ha guardado nada"
        )
    return queries.ScriptEdit(
        hook=_texto_del_formulario(gancho),
        outro=_texto_del_formulario(cierre),
        chapters=tuple(
            queries.ChapterEdit(
                title=_texto_del_formulario(titulo),
                narration=_texto_del_formulario(narracion),
            )
            for titulo, narracion in zip(titulos, narraciones)
        ),
    )


def _texto_del_formulario(texto: str) -> str:
    """Texto de un campo del editor, con los saltos de línea normalizados.

    Un `textarea` manda los saltos como retorno de carro más salto (lo exige el
    HTML) y en la base están guardados solo con el salto. Sin normalizar, abrir
    un guion y guardarlo sin tocar nada contaría como una edición y metería
    retornos de carro en el texto que el hito 3 va a locutar.
    """
    return texto.replace("\r\n", "\n").strip()


def _error_de_guion(
    exc: queries.ScriptNotFound | queries.ScriptLocked | queries.ScriptInvalid,
) -> HTTPException:
    """404 si el video no existe, 409 si ya lo decidieron, 400 si la edición no vale."""
    if isinstance(exc, queries.ScriptNotFound):
        codigo = 404
    elif isinstance(exc, queries.ScriptLocked):
        codigo = 409
    else:
        codigo = 400
    return HTTPException(status_code=codigo, detail=str(exc))


# ---------------------------------------------------------------------------
# Vista: Sistema
# ---------------------------------------------------------------------------


@router.get("/system", response_class=HTMLResponse)
def system_view(request: Request, mensaje: str | None = None) -> HTMLResponse:
    """Estado operativo: jobs recientes, cuota de hoy y próxima investigación."""
    conn = db.get_conn()
    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "activa": "sistema",
            "jobs": queue.list_jobs(limit=RECENT_JOBS_LIMIT, conn=conn),
            "pendientes": queue.pending_count(conn),
            "cuotas": queries.quota_today(conn),
            "proxima_investigacion": _next_research_run(request),
            "mensaje": mensaje,
        },
    )


@router.post("/research/run")
def run_research_now() -> RedirectResponse:
    """Encola una investigación inmediata, la misma que dispara el cron.

    Encola y vuelve: el trabajo real lo hace el worker, que tarda minutos. Si el
    dashboard esperase al resultado, la petición moriría por timeout y el job
    seguiría corriendo igual.

    Un nicho que ya tiene su investigación en cola no se reencola (el dedupe
    está en `scheduler.enqueue_research_daily`); aquí solo se cuenta.
    """
    resultado = scheduler.enqueue_research_daily()
    logger.info(
        "Investigación manual: encolados %s, ya en cola %s",
        resultado.job_ids, resultado.skipped_niches,
    )
    return _volver_a_sistema(_mensaje_de_investigacion(resultado))


def _mensaje_de_investigacion(resultado: scheduler.ResearchEnqueue) -> str:
    """Texto del aviso tras pulsar "Investigar ahora"."""
    partes: list[str] = []
    if resultado.job_ids:
        ids = ", ".join(str(job_id) for job_id in resultado.job_ids)
        partes.append(f"{len(resultado.job_ids)} job(s) de investigación encolados: {ids}")
    if resultado.skipped_niches:
        nichos = ", ".join(resultado.skipped_niches)
        partes.append(f"ya había investigación en cola para: {nichos}")
    return " · ".join(partes) or "no hay nichos configurados que investigar"


def _next_research_run(request: Request) -> datetime | None:
    """Próxima ejecución programada de `research_daily`, si el scheduler corre.

    Es None en dos casos legítimos: la app arrancó sin lifespan (tests) o el
    scheduler está parado. La plantilla lo distingue por sí sola.
    """
    planificador = getattr(request.app.state, "scheduler", None)
    if planificador is None:
        return None
    job = planificador.get_job(scheduler.RESEARCH_JOB_ID)
    return job.next_run_time if job is not None else None


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _volver_a_ideas(mensaje: str) -> RedirectResponse:
    """Redirect al ranking con un mensaje de resultado."""
    return RedirectResponse(f"/?mensaje={quote(mensaje)}", status_code=SEE_OTHER)


def _volver_a_guiones(mensaje: str) -> RedirectResponse:
    """Redirect a la lista de guiones con un mensaje de resultado."""
    return RedirectResponse(f"/scripts?mensaje={quote(mensaje)}", status_code=SEE_OTHER)


def _volver_al_guion(video_id: int, mensaje: str) -> RedirectResponse:
    """Redirect al editor del mismo guion, para seguir donde se estaba."""
    return RedirectResponse(
        f"/scripts/{video_id}?mensaje={quote(mensaje)}", status_code=SEE_OTHER
    )


def _volver_a_sistema(mensaje: str) -> RedirectResponse:
    """Redirect a la vista de sistema con un mensaje de resultado."""
    return RedirectResponse(f"/system?mensaje={quote(mensaje)}", status_code=SEE_OTHER)
