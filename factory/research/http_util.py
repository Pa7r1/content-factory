"""HTTP compartido por las fuentes: timeout obligatorio y reintentos acotados.

Solo se reintenta lo transitorio (429, 5xx, timeout, error de conexión) con
backoff exponencial más jitter. Un 4xx no transitorio no se reintenta jamás:
es gastar red para recibir el mismo error.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

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
    try:
        return response.json()
    except ValueError as exc:
        raise SourceUnavailable(f"{url}: la respuesta no es JSON ({exc})") from exc


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
    last_reason = "sin intentos"
    last_status: int | None = None
    for attempt in range(1, max_attempts + 1):
        espera_pedida = 0.0  # lo que pida el servidor con Retry-After, si lo pide
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
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
                return response
            if response.status_code not in RETRY_STATUSES:
                raise SourceUnavailable(
                    f"{url}: HTTP {response.status_code} (no transitorio)",
                    status=response.status_code,
                )
            last_reason = f"HTTP {response.status_code}"
            last_status = response.status_code
            espera_pedida = _retry_after_seconds(response)

        if attempt < max_attempts:
            delay = max(_backoff_delay(attempt), espera_pedida)
            logger.warning(
                "GET %s falló (%s), reintento %d de %d en %.1fs",
                url, last_reason, attempt, max_attempts - 1, delay,
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
