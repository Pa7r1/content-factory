# Content Factory

Sistema de producción de contenido para YouTube que automatiza la toma de decisiones:
descubre oportunidades, las puntúa, genera guion, video, miniatura y SEO, publica, mide
y aprende. Vos solo controlás la calidad en dos checkpoints: aprobar el guion y aprobar
el video final.

Corre 24/7 en una PC (Windows o Linux). Todo gratis o casi ($0/mes objetivo).

## Cómo funciona

```
Ideas → Score → [✋ aprobás la idea] → Guion → [✋ aprobás el guion]
      → Video + Miniaturas → [✋ aprobás el video] → Publicación programada → Métricas → Aprendizaje
```

Cada video pasa por una máquina de estados; los módulos se comunican solo a través de la
base de datos SQLite (`data/factory.db`). El panel de control vive en `http://localhost:8000`.

## Instalación (Linux / desarrollo)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py               # dashboard en http://localhost:8000
```

Arranca sin ninguna clave: las fuentes que necesiten una que falte quedan como señal
ausente y el score se re-normaliza con las que sí hay. Para tenerlo completo, ver abajo.

## Instalación (Windows / producción 24/7)

```powershell
.\scripts_ops\install_windows.ps1   # venv + dependencias + FFmpeg + servicio NSSM
```

(Se completa en el hito 5 — ver [ROADMAP.md](ROADMAP.md).)

## Claves

Van en un fichero `.env` en la raíz del proyecto (está en `.gitignore`, nunca se sube).
Créalo a mano con las que tengas:

```
YOUTUBE_API_KEY=
GOOGLE_AI_API_KEY=
PEXELS_API_KEY=
PIXABAY_API_KEY=
```

### YOUTUBE_API_KEY — la más importante, y es gratis

Sin ella el motor de investigación funciona a medio gas: **no hay señal de competencia**
(el hueco de mercado, que pesa un 30% del score) ni mediana de vistas, así que las ideas
salen casi solo de titulares de noticias. Con ella el score empieza a significar algo.

1. Entrá en [console.cloud.google.com](https://console.cloud.google.com) y creá un proyecto.
2. *APIs y servicios → Biblioteca* → buscá **YouTube Data API v3** → Habilitar.
3. *APIs y servicios → Credenciales* → **Crear credenciales → Clave de API**.
4. Copiá la clave en `.env` como `YOUTUBE_API_KEY=...` y reiniciá la app.

Cuota gratuita: 10.000 unidades/día. El sistema se reserva 4.000 y corta al 80% (3.200),
llevando la cuenta en la tabla `api_usage` — la ves en la pestaña *Sistema* del dashboard.

### GOOGLE_AI_API_KEY

Gemini, para los guiones (hito 2). Gratis en
[aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey).

## Requisitos

- Python 3.12+
- FFmpeg en el PATH (para los hitos de producción de video)

## Estructura

```
app.py              # entrypoint: dashboard + scheduler + worker
factory/core/       # DB, cola de jobs, scheduler
factory/research/   # fuentes de datos + score de oportunidades
factory/script/     # generación de guiones (5 formatos)
factory/media/      # TTS, imágenes, stock
factory/video/      # montaje FFmpeg (16:9 y 9:16)
factory/thumbnail/  # miniaturas
factory/seo/        # títulos, descripción, tags
factory/publish/    # subida a YouTube
factory/analytics/  # métricas + cerebro
factory/web/        # dashboard
```

El estado del proyecto está en [ROADMAP.md](ROADMAP.md) y las decisiones/avances en
[PROJECT_MEMORY.md](PROJECT_MEMORY.md).
