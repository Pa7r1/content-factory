"""HTTP compartido: timeout obligatorio y reintentos acotados.

Solo se reintenta lo transitorio (429, 5xx, timeout, error de conexión) con
backoff exponencial más jitter. Un 4xx no transitorio no se reintenta jamás:
es gastar red para recibir el mismo error.

Vive en `core/` porque lo usan tanto las fuentes de `research/` como el cliente
de LLM (`core/llm.py`), y un módulo de dominio no puede importar de otro. Tener
dos implementaciones del mismo retry es tener dos comportamientos distintos en
cuanto una de las dos se toque.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 30.0


class SourceUnavailable(RuntimeError):
    """La fuente no puede dar señal ahora (sin clave, bloqueada o caída).

    El orquestador la trata como señal ausente: registra el motivo y sigue.
    `status` lleva el código HTTP cuando lo hubo (None si ni siquiera conectó),
    para que el llamador distinga "no hay datos" (404) de "la fuente cayó".
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = 3,
) -> Any:
    """GET que devuelve el JSON decodificado o lanza `SourceUnavailable`."""
    response = get_with_retries(
        url, params=params, headers=headers, timeout=timeout, max_attempts=max_attempts
    )
    return _decode_json(response, url)


def post_json(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = 3,
    retry_delay_from_body: Callable[[requests.Response], float] | None = None,
) -> tuple[Any, int]:
    """POST de un cuerpo JSON. Devuelve (JSON decodificado, peticiones hechas).

    Mismos reintentos que el GET. Se devuelve el número de peticiones porque
    quien gasta cuota por petición reserva el PEOR caso (`max_attempts`) ANTES
    de llamar —un 429 reintentado son dos peticiones contra el mismo tope— y
    después necesita liquidar lo que de verdad gastó.

    `retry_delay_from_body` es para las APIs que no mandan `Retry-After` y
    ponen la espera en el cuerpo del error; ver `core/llm.py`.
    """
    response, intentos = _request_with_retries(
        "POST", url, json_body=body, headers=headers, timeout=timeout,
        max_attempts=max_attempts, retry_delay_from_body=retry_delay_from_body,
    )
    return _decode_json(response, url), intentos


def get_with_retries(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = 3,
) -> requests.Response:
    """GET con reintentos solo en errores transitorios.

    Devuelve la respuesta 2xx. Cualquier otro desenlace tras agotar intentos
    (o un 4xx no transitorio al primer intento) lanza `SourceUnavailable` con
    el motivo, para que el llamador lo registre como señal ausente.
    """
    response, _intentos = _request_with_retries(
        "GET", url, params=params, headers=headers, timeout=timeout,
        max_attempts=max_attempts,
    )
    return response


def _decode_json(response: requests.Response, url: str) -> Any:
    """Cuerpo JSON de una respuesta, o `SourceUnavailable` si no lo es."""
    try:
        return response.json()
    except ValueError as exc:
        raise SourceUnavailable(f"{url}: la respuesta no es JSON ({exc})") from exc


def _request_with_retries(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = 3,
    retry_delay_from_body: Callable[[requests.Response], float] | None = None,
) -> tuple[requests.Response, int]:
    """Petición con reintentos solo en transitorios. Devuelve (respuesta, intentos).

    `retry_delay_from_body` se llama con la respuesta de cada error transitorio
    y devuelve los segundos de espera que pida el cuerpo, para las APIs que no
    usan `Retry-After`. Es responsabilidad del lector acotar lo que devuelve:
    aquí se toma tal cual, y un número absurdo dejaría al worker parado.
    """
    last_reason = "sin intentos"
    last_status: int | None = None
    for attempt in range(1, max_attempts + 1):
        espera_pedida = 0.0  # lo que pida el servidor con Retry-After, si lo pide
        try:
            response = requests.request(
                method, url, params=params, json=json_body, headers=headers,
                timeout=timeout,
            )
        except requests.exceptions.SSLError as exc:
            # SSLError hereda de ConnectionError, así que va ANTES: un certificado
            # inválido o una cadena rota no se arreglan reintentando.
            raise SourceUnavailable(f"{url}: {type(exc).__name__}: {exc}") from exc
        except (requests.Timeout, requests.ConnectionError) as exc:
            # Transitorio: la red o el servidor no respondieron a tiempo.
            last_reason = type(exc).__name__
        except requests.RequestException as exc:
            # El resto de fallos de requests (redirecciones, respuesta rota)
            # no mejoran reintentando; se convierten en señal ausente igualmente.
            raise SourceUnavailable(f"{url}: {type(exc).__name__}: {exc}") from exc
        else:
            if response.ok:
                return response, attempt
            if response.status_code not in RETRY_STATUSES:
                raise SourceUnavailable(
                    f"{url}: HTTP {response.status_code} (no transitorio)",
                    status=response.status_code,
                )
            last_reason = f"HTTP {response.status_code}"
            last_status = response.status_code
            espera_pedida = _retry_after_seconds(response)
            if retry_delay_from_body is not None:
                espera_pedida = max(espera_pedida, retry_delay_from_body(response))

        if attempt < max_attempts:
            delay = max(_backoff_delay(attempt), espera_pedida)
            logger.warning(
                "%s %s falló (%s), reintento %d de %d en %.1fs",
                method, url, last_reason, attempt, max_attempts - 1, delay,
            )
            time.sleep(delay)

    raise SourceUnavailable(
        f"{url}: agotados {max_attempts} intentos ({last_reason})", status=last_status
    )


def _backoff_delay(attempt: int) -> float:
    """Espera antes del siguiente intento: exponencial más jitter."""
    return BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)


def _retry_after_seconds(response: requests.Response) -> float:
    """Segundos que pide la cabecera `Retry-After`, si viene y es un número.

    Se ignora el formato de fecha HTTP (raro en las APIs que usamos) y se acota
    a MAX_RETRY_AFTER: un servidor no puede dejar al worker parado diez minutos.
    """
    raw = response.headers.get("Retry-After", "")
    try:
        segundos = float(raw)
    except ValueError:
        return 0.0
    return min(max(segundos, 0.0), MAX_RETRY_AFTER_SECONDS)
