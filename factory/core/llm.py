"""Cliente de Gemini (REST v1beta, sin SDK: es un POST con `x-goog-api-key`).

Vive en `core/` porque lo comparten varios módulos de dominio (`research/` para
sintetizar temas, `script/` para los guiones del hito 2) y un módulo de dominio
no puede importar de otro.

Reglas que hereda del resto del sistema:

- Sin `GOOGLE_AI_API_KEY` en el entorno se lanza `SourceUnavailable`, igual que
  `youtube_source._api_key()`. El llamador lo trata como fuente caída, nunca
  como "no hay datos".
- El coste se reserva en `api_usage` ANTES de disparar la petición. La unidad de
  cuota es **una petición HTTP**, que es lo que limita el free tier, y se
  reserva el peor caso (`MAX_ATTEMPTS` peticiones) porque un 429 reintentado son
  dos peticiones reales contra el mismo tope. Contar de más antes de llamar es
  seguro; contar de menos es pasarse del límite sin enterarse. Al volver se
  liquida con las peticiones que de verdad se hicieron.
- Una respuesta que no se puede parsear es una fuente caída, no un dato vacío.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

from factory.core import config, quota
from factory.core.http_util import SourceUnavailable, post_json

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL = "gemini-2.5-flash"
QUOTA_API = "gemini"

# Timeout por defecto de una petición. Vale para las listas cortas de
# `research/`; quien pida una respuesta larga (un guion entero) pasa el suyo,
# porque un timeout aquí no aborta la generación: dispara un reintento que es
# otra generación completa.
TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 2
COST_PER_REQUEST = 1

# El free tier limita por minuto (10 peticiones), así que el backoff de 2-3 s
# del transporte reintenta dentro de la MISMA ventana y vuelve a fallar. Gemini
# dice cuánto esperar, pero no en la cabecera `Retry-After`: lo manda en el
# cuerpo del error, como "27s". Se acota a 65 s —una ventana entera más un
# margen—: más que eso no es un reintento, es un worker parado.
MAX_RETRY_WAIT_SECONDS = 65.0
_RETRY_DELAY_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)s$")

# El "thinking" de gemini-2.5-flash consume tokens de salida y puede devolver un
# JSON truncado con finishReason MAX_TOKENS. Para pedir listas cortas no aporta
# nada, así que se apaga explícitamente.
THINKING_BUDGET = 0
DEFAULT_MAX_OUTPUT_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.9


def generate_text(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout: float = TIMEOUT_SECONDS,
) -> str:
    """Texto generado por el modelo. Lanza `SourceUnavailable` si no lo hay."""
    return _generate(
        prompt,
        system=system,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout=timeout,
        json_mode=False,
    )


def generate_json(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    timeout: float = TIMEOUT_SECONDS,
) -> Any:
    """JSON generado por el modelo, ya decodificado.

    Se pide con `responseMimeType: application/json`, así que la respuesta no
    trae texto alrededor. Si aun así no se puede decodificar, se lanza
    `SourceUnavailable`: el modelo respondió algo inservible, que a efectos del
    llamador es exactamente lo mismo que no haber respondido.
    """
    crudo = _generate(
        prompt,
        system=system,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout=timeout,
        json_mode=True,
    )
    try:
        return json.loads(crudo)
    except ValueError as exc:
        raise SourceUnavailable(f"{MODEL}: la respuesta no es JSON ({exc})") from exc


def _generate(
    prompt: str,
    *,
    system: str | None,
    temperature: float,
    max_output_tokens: int,
    timeout: float,
    json_mode: bool,
) -> str:
    """Reserva cuota, llama a `:generateContent` y devuelve el texto.

    Se reserva el peor caso (`MAX_ATTEMPTS` peticiones) y se liquida con las que
    de verdad se hicieron: reservar de más antes de llamar es seguro, pero
    liquidar de más deja el presupuesto efectivo en la mitad del configurado.

    Si la llamada falla, la reserva se queda en 'reserved' con el peor caso: el
    gasto ya está hecho y corregirlo a la baja dejaría el contador por debajo
    del consumo real.
    """
    key = _api_key()  # antes de reservar: sin clave no hay que gastar cuota
    reserva = quota.reserve(
        QUOTA_API,
        COST_PER_REQUEST * MAX_ATTEMPTS,
        config.daily_budget(QUOTA_API),
        detail=f"generateContent {MODEL}",
    )
    data, peticiones = post_json(
        f"{API_BASE}/{MODEL}:generateContent",
        _request_body(prompt, system, temperature, max_output_tokens, json_mode),
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        timeout=timeout,
        max_attempts=MAX_ATTEMPTS,
        retry_delay_from_body=_retry_delay_seconds,
    )
    quota.reconcile(reserva, actual_units=COST_PER_REQUEST * peticiones)
    texto = _first_text(data)  # valida la forma de la respuesta: aquí ya es un dict
    logger.debug(
        "%s: %s tokens en total", MODEL, (data.get("usageMetadata") or {}).get("totalTokenCount")
    )
    return texto


def _retry_delay_seconds(response: requests.Response) -> float:
    """Segundos de espera que pide Gemini en el cuerpo de un error transitorio.

    La API los manda como `retryDelay: "27s"` dentro de los detalles del error
    (`google.rpc.RetryInfo`), no en la cabecera `Retry-After`, así que el
    transporte por sí solo leía 0 y reintentaba dentro del mismo minuto.
    Devuelve 0.0 si el cuerpo no dice nada: entonces manda el backoff normal.
    """
    try:
        data = response.json()
    except ValueError:
        return 0.0

    for detalle in _error_details(data):
        segundos = _parse_delay(detalle.get("retryDelay"))
        if segundos > 0:
            return min(segundos, MAX_RETRY_WAIT_SECONDS)
    return 0.0


def _error_details(data: Any) -> list[dict[str, Any]]:
    """Los bloques de detalle de un error de la API, si la respuesta los trae.

    Se miran las dos formas vistas en respuestas reales: la documentada
    (`error.details`, una lista de bloques con su `@type`) y `retryDetails`
    suelto en la raíz.
    """
    if not isinstance(data, dict):
        return []
    error = data.get("error")
    bloques = error.get("details") if isinstance(error, dict) else None
    candidatos = [
        *(bloques if isinstance(bloques, list) else []),
        data.get("retryDetails"),
    ]
    return [bloque for bloque in candidatos if isinstance(bloque, dict)]


def _parse_delay(valor: Any) -> float:
    """'27s' -> 27.0. Cualquier otra cosa, 0.0."""
    if not isinstance(valor, str):
        return 0.0
    match = _RETRY_DELAY_RE.match(valor.strip())
    return float(match.group(1)) if match else 0.0


def _api_key() -> str:
    """Clave del entorno. Sin ella, fuente caída: nunca un texto vacío."""
    key = os.environ.get("GOOGLE_AI_API_KEY", "").strip()
    if not key:
        raise SourceUnavailable("GOOGLE_AI_API_KEY no está en el entorno (.env)")
    return key


def _request_body(
    prompt: str,
    system: str | None,
    temperature: float,
    max_output_tokens: int,
    json_mode: bool,
) -> dict[str, Any]:
    """Cuerpo de `:generateContent` tal y como lo espera la API v1beta."""
    generation: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
        "thinkingConfig": {"thinkingBudget": THINKING_BUDGET},
    }
    if json_mode:
        generation["responseMimeType"] = "application/json"

    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    return body


def _first_text(data: Any) -> str:
    """Texto del primer candidato. `SourceUnavailable` si la respuesta no trae.

    Un prompt bloqueado por filtros de seguridad devuelve 200 sin candidatos:
    eso es una fuente que no puede dar dato, no un dato vacío.
    """
    if not isinstance(data, dict):
        raise SourceUnavailable(f"{MODEL}: respuesta con forma inesperada")

    candidatos = data.get("candidates") or []
    if not candidatos:
        motivo = (data.get("promptFeedback") or {}).get("blockReason", "sin motivo")
        raise SourceUnavailable(f"{MODEL}: respuesta sin candidatos ({motivo})")

    candidato = candidatos[0]
    partes = (candidato.get("content") or {}).get("parts") or []
    texto = "".join(parte.get("text", "") for parte in partes).strip()
    if not texto:
        raise SourceUnavailable(
            f"{MODEL}: candidato sin texto (finishReason="
            f"{candidato.get('finishReason', 'desconocido')})"
        )
    return texto
