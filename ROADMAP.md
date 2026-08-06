# Roadmap

Estado: `pendiente` · `en curso` · `hecho ✅ (fecha)`

## Hito 1 — Motor de investigación + Dashboard — **hecho ✅ (2026-08-06)**

El corazón del sistema: encontrar sistemáticamente mejores oportunidades que otros creadores.

- [x] `core/`: base SQLite (WAL, migraciones con `user_version`), cola de jobs en tabla, APScheduler — hecho ✅ (2026-08-06)
- [x] `research/`: YouTube Data API v3 (con presupuesto de cuota en `api_usage`), Wikipedia Pageviews, Google News RSS, Reddit JSON público — hecho ✅ (2026-08-06). Pendiente: trendspy (opcional degradable)
- [x] `scorer.py`: score 0–100 = demanda + hueco de competencia + evergreen + CPM del nicho; re-normaliza pesos si falta una señal — hecho ✅ (2026-08-06)
- [x] `competitor.py`: análisis de canales (frecuencia, duración, outlier ratio) — hecho ✅ (2026-08-06, verificado con transporte HTTP falso; falta pasada real con clave de YouTube)
- [x] `pipeline.py`: handler `research_daily` (keywords semilla → señales → score → ideas) — hecho ✅ (2026-08-06)
- [x] Dashboard: ranking de ideas con aprobar/descartar, estado de la cola, uso de cuota — hecho ✅
      (2026-08-06). Estructura y datos; la capa visual la pone `disenador`
- [x] Tests (611, sin xfail) + revisión en contexto limpio + verificación end-to-end — hecho ✅
      (2026-08-06). La revisión encontró 12 fallos (3 críticos de operación 24/7); todos arreglados
      y verificados en ejecución, no solo en tests

**Demo:** abrir el dashboard y ver ~20 ideas reales del nicho puntuadas, refrescadas cada mañana.

**Estado real al cerrar:** funciona y produce ideas puntuadas, pero **a medio gas hasta que haya
`YOUTUBE_API_KEY`**: sin ella falta el componente de competencia (30% del score) y la mediana de
vistas, así que las ideas salen casi solo de titulares de noticias. Ver [README.md](README.md).
Reddit responde 403 a esta IP: la señal degrada con su motivo registrado en la base.

## Hito 2 — Guiones + Base de conocimiento — pendiente

- [ ] 5 formatos de guion (misterio, educativo, storytelling, top-N, noticias)
- [ ] Knowledge base (hooks, CTA, lecciones) con FTS5; el LLM la consulta antes de escribir
- [ ] Checkpoint 1: editor de guion en el dashboard

**Demo:** aprobar una idea → leer y editar un guion de 6–10 min con capítulos.

## Hito 3 — Producción de video — pendiente

- [ ] Migrar TTS (Edge TTS), imágenes (con fallbacks) y montaje FFmpeg del MVP anterior
- [ ] Perfiles 1920×1080 (largo, música con ducking) y 1080×1920 (shorts)
- [ ] B-roll real de Pexels/Pixabay + 3 shorts derivados del largo
- [ ] Miniaturas: 3 variantes (fondo IA/stock + Pillow) puntuadas por LLM multimodal
- [ ] Checkpoint 2: player + selector de miniatura en el dashboard

**Demo:** guion aprobado → video largo + 3 shorts + 3 miniaturas listos para revisar.

## Hito 4 — Publicación + SEO — pendiente

- [ ] OAuth de YouTube (una sola vez), subida como privado programado (`publishAt`)
- [ ] SEO: títulos candidatos, descripción con capítulos, tags, comentario fijado
- [ ] Disclosure de contenido sintético; solicitar auditoría de la API de YouTube

**Demo:** aprobar video → aparece programado en YouTube Studio.

## Hito 5 — Métricas + Cerebro + servicio Windows — pendiente

- [ ] YouTube Analytics API: snapshots de CTR/retención/RPM a día 1/3/7/28
- [ ] `brain.py`: correlaciones simples → lecciones en la knowledge base → ajuste de pesos del scorer
- [ ] `install_windows.ps1` + NSSM: servicio 24/7 con auto-restart
- [ ] Prueba de instalación limpia en la PC Windows

**Demo:** el sistema corre solo; cada mañana hay ideas nuevas cuyo score incorpora lo aprendido.

## Después (ideas, sin compromiso)

- Predictor de ideas (regresión con >50 videos propios)
- Artículo web + posts para redes derivados de cada video
- Multicanal: N canales con la misma fábrica cambiando solo la temática
