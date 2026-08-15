"""Carga de configuración: `config/settings.yaml` + `.env` (si existe).

`settings()` devuelve el YAML como dict y se cachea: la configuración se lee
una vez por proceso. Los secretos nunca van al YAML; van en `.env` y se leen
del entorno con `os.environ` donde hagan falta.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from factory.core.db import PROJECT_ROOT

SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"

# Carga .env de la raíz del proyecto si existe; sin él no pasa nada (aún no hay secretos).
load_dotenv(PROJECT_ROOT / ".env")


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    """Configuración completa de `config/settings.yaml` (cacheada)."""
    with SETTINGS_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{SETTINGS_PATH} no contiene un mapeo YAML válido")
    return data


def score_weights() -> dict[str, float]:
    """Pesos del score. Valida que sumen 1 (con tolerancia de redondeo)."""
    weights: dict[str, float] = settings()["score_weights"]
    total = sum(weights.values())
    if abs(total - 1.0) > 0.001:
        raise ValueError(f"Los pesos del score suman {total}, deben sumar 1")
    return weights


def niches() -> dict[str, Any]:
    """Nichos activos con sus keywords semilla."""
    return settings()["niches"]


def subreddits(niche: str) -> list[str]:
    """Subreddits configurados para sondear un nicho (lista vacía si no hay)."""
    # `or []` y no un default del get, por lo mismo que en `script_format`: un
    # `subreddits:` sin valor —lo que queda al borrar el último subreddit de un
    # nicho— existe como clave y vale None, así que el default no se aplica y el
    # list() reventaría. Y esto se llama fuera de `_try_source`, sin red debajo:
    # el TypeError se llevaría por delante la investigación de todos los nichos.
    return list(niches()[niche].get("subreddits") or [])


def cpm_for_niche(niche: str) -> float | None:
    """CPM estimado en USD del nicho, o None si no está en la tabla."""
    tabla: dict[str, float] = settings().get("cpm_by_niche", {})
    valor = tabla.get(niche)
    return float(valor) if valor is not None else None


def daily_budget(api: str) -> int:
    """Presupuesto diario de unidades para una API (p. ej. 'youtube')."""
    return int(settings()["quotas"][api]["daily_budget"])


def quota_apis() -> list[str]:
    """APIs con presupuesto configurado, en orden alfabético."""
    return sorted(settings().get("quotas", {}))


def schedule_time(task: str) -> tuple[int, int]:
    """Hora configurada de una tarea periódica como (hora, minuto)."""
    raw: str = settings()["schedule"][task]
    hour_str, minute_str = raw.split(":")
    return int(hour_str), int(minute_str)
