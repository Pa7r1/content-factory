"""Puntuación de oportunidades: lógica pura, sin red y sin base de datos.

Recibe señales crudas (`Signals`) y los pesos de configuración; devuelve un
score 0–100 con su desglose. Que sea pura es lo que la hace verificable con
datos sintéticos y reutilizable cuando el cerebro (hito 5) reajuste pesos.

Regla central: **una señal ausente nunca vale 0**. Los pesos de las señales
que sí llegaron se re-normalizan, y `ScoreResult.missing` deja constancia de
qué faltó para que el dashboard pueda explicar el número.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import log10
from statistics import fmean, median, pstdev

# --- Techos de normalización (documentados: son criterio, no verdad) --------
VIEWS_LOG_CEILING = 6.0          # mediana de 10^6 vistas = demanda máxima
NEWS_VOLUME_CEILING = 15         # 15 titulares en 7 días = volumen máximo
REDDIT_ENGAGEMENT_CEILING = 5000  # upvotes + comentarios que ya son señal máxima
SMALL_CHANNEL_SUBS = 100_000     # por debajo de esto, un canal es "pequeño"
AGE_CEILING_DAYS = 730.0         # 2 años de antigüedad mediana = hueco máximo
FRESH_DAYS = 90.0                # un video de menos de 90 días cuenta como saturación
CPM_REFERENCE_USD = 8.0          # CPM alto para YouTube en español = factor 1

# Sub-pesos internos de cada componente (los de nivel superior vienen de config).
DEMAND_WEIGHTS: dict[str, float] = {"views": 0.5, "momentum": 0.3, "reddit": 0.2}
COMPETITION_WEIGHTS: dict[str, float] = {
    "small_channels": 0.5,
    "age": 0.3,
    "room_left": 0.2,  # 1 - saturación
}


@dataclass(frozen=True, slots=True)
class Signals:
    """Señales crudas de una keyword. `None` significa fuente sin dato, no cero."""

    top_video_views: list[int] | None = None       # vistas de los top N de YouTube
    top_channel_subs: list[int] | None = None      # subs de los canales de esos videos
    top_video_ages_days: list[float] | None = None  # antigüedad de esos videos, en días
    news_last_7d: int | None = None                # titulares de los últimos 7 días
    news_last_30d: int | None = None               # titulares de los últimos 30 días
    reddit_engagement: int | None = None           # upvotes + comentarios que casan
    wikipedia_monthly_views: list[int] | None = None  # meses completos, en orden
    cpm_usd: float | None = None                   # CPM estimado del nicho


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Score final y todo lo necesario para explicarlo."""

    score: float                                   # 0..100
    components: dict[str, float | None]            # demand / competition / evergreen / cpm
    sub_metrics: dict[str, float | None]           # cada señal ya normalizada 0..1
    weights_used: dict[str, float]                 # pesos efectivos tras re-normalizar
    missing: list[str] = field(default_factory=list)  # señales que no llegaron


def score_idea(signals: Signals, weights: dict[str, float]) -> ScoreResult:
    """Puntúa una oportunidad de 0 a 100 a partir de sus señales crudas.

    `weights` son los pesos de `config.score_weights()`: demand, competition,
    evergreen, cpm. Si un componente entero se queda sin señales, su peso se
    reparte entre los demás. Sin ninguna señal, el score es 0 y `missing` lo dice.
    """
    sub = _normalize_signals(signals)

    components: dict[str, float | None] = {
        "demand": _combine({k: sub[k] for k in DEMAND_WEIGHTS}, DEMAND_WEIGHTS),
        "competition": _combine(
            {k: sub[k] for k in COMPETITION_WEIGHTS}, COMPETITION_WEIGHTS
        ),
        "evergreen": sub["evergreen"],
        "cpm": sub["cpm"],
    }
    effective = _effective_weights(components, weights)
    total = _combine(components, weights)

    return ScoreResult(
        score=round(100.0 * total, 2) if total is not None else 0.0,
        components=components,
        sub_metrics=sub,
        weights_used=effective,
        missing=sorted(name for name, value in sub.items() if value is None),
    )


# ---------------------------------------------------------------------------
# Normalización de cada señal a 0..1 (None si la señal no llegó)
# ---------------------------------------------------------------------------


def _normalize_signals(signals: Signals) -> dict[str, float | None]:
    """Cada señal cruda a su versión 0..1, conservando los None."""
    return {
        "views": views_norm(signals.top_video_views),
        "momentum": momentum_norm(signals.news_last_7d, signals.news_last_30d),
        "reddit": reddit_norm(signals.reddit_engagement),
        "small_channels": small_channel_share(signals.top_channel_subs),
        "age": age_norm(signals.top_video_ages_days),
        "room_left": room_left(signals.top_video_ages_days),
        "evergreen": evergreen_norm(signals.wikipedia_monthly_views),
        "cpm": cpm_norm(signals.cpm_usd),
    }


def views_norm(views: list[int] | None) -> float | None:
    """Demanda por volumen: log10(mediana de vistas) / 6, tope 1."""
    if not views:
        return None
    mediana = median(views)
    if mediana <= 1:
        return 0.0
    return _clamp(log10(mediana) / VIEWS_LOG_CEILING)


def momentum_norm(last_7d: int | None, last_30d: int | None) -> float | None:
    """Momentum del tema: cuánta noticia hay y cuánta es de esta semana."""
    if last_7d is None:
        return None
    volumen = _clamp(last_7d / NEWS_VOLUME_CEILING)
    if not last_30d:
        return volumen
    recencia = _clamp(last_7d / last_30d)
    return 0.6 * volumen + 0.4 * recencia


def reddit_norm(engagement: int | None) -> float | None:
    """Interés en comunidades: log del engagement contra un techo de 5000."""
    if engagement is None:
        return None
    return _clamp(log10(1 + max(engagement, 0)) / log10(1 + REDDIT_ENGAGEMENT_CEILING))


def small_channel_share(subs: list[int] | None) -> float | None:
    """Proporción de canales pequeños (<100k subs) en el top: hueco real."""
    if not subs:
        return None
    pequenos = sum(1 for s in subs if s < SMALL_CHANNEL_SUBS)
    return pequenos / len(subs)


def age_norm(ages_days: list[float] | None) -> float | None:
    """Antigüedad mediana del top / 2 años, tope 1. Más viejo = más hueco."""
    if not ages_days:
        return None
    return _clamp(median(ages_days) / AGE_CEILING_DAYS)


def room_left(ages_days: list[float] | None) -> float | None:
    """1 - saturación, donde saturación = fracción del top publicada hace <90 días.

    Un top copado por videos recién subidos indica que el tema ya está siendo
    trabajado ahora mismo; uno con videos antiguos deja sitio.
    """
    if not ages_days:
        return None
    recientes = sum(1 for age in ages_days if age < FRESH_DAYS)
    return 1.0 - recientes / len(ages_days)


def evergreen_norm(monthly_views: list[int] | None) -> float | None:
    """Estabilidad del interés: 1 - coeficiente de variación de los pageviews.

    Hacen falta al menos 3 meses completos; con menos no hay serie que juzgar.
    """
    if not monthly_views or len(monthly_views) < 3:
        return None
    media = fmean(monthly_views)
    if media <= 0:
        return 0.0
    return _clamp(1.0 - pstdev(monthly_views) / media)


def cpm_norm(cpm_usd: float | None) -> float | None:
    """CPM del nicho contra la referencia de 8 USD, tope 1."""
    if cpm_usd is None:
        return None
    return _clamp(cpm_usd / CPM_REFERENCE_USD)


# ---------------------------------------------------------------------------
# Combinación con re-normalización de pesos
# ---------------------------------------------------------------------------


def _effective_weights(
    values: dict[str, float | None], weights: dict[str, float]
) -> dict[str, float]:
    """Pesos de las señales presentes, re-escalados para volver a sumar 1."""
    presentes = {
        name: weight
        for name, weight in weights.items()
        if values.get(name) is not None
    }
    total = sum(presentes.values())
    if total <= 0:
        return {}
    return {name: weight / total for name, weight in presentes.items()}


def _combine(
    values: dict[str, float | None], weights: dict[str, float]
) -> float | None:
    """Media ponderada que ignora los ausentes. None si no queda ninguna señal."""
    effective = _effective_weights(values, weights)
    if not effective:
        return None
    return sum(values[name] * weight for name, weight in effective.items())  # type: ignore[operator]


def _clamp(value: float) -> float:
    """Recorta a 0..1: los techos de normalización son topes, no asíntotas."""
    return max(0.0, min(1.0, value))
