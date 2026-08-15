"""Los cinco formatos de guion: qué estructura le pide cada uno al modelo.

Un formato es un bloque de texto que se inserta en el prompt del guion, nada
más. No hay clases ni jerarquía porque lo único que cambia entre un misterio y
un top-N es la estructura que se le describe al modelo; el resto del prompt
(duración, tono del nicho, forma del JSON) es común y vive en `writer.py`.

Los identificadores son los mismos que acepta `videos.format` en el esquema y
los que `research/` puede sugerir en `ideas.suggested_format`. Si aquí se añade
uno nuevo, hay que añadirlo también al comentario de la columna.
"""

from __future__ import annotations

# Instrucciones por formato. Se pegan tal cual en el prompt, así que están
# escritas para el modelo: imperativas, concretas y sin ambigüedad.
FORMATS: dict[str, str] = {
    "misterio": """Formato: misterio.
- Abre planteando un enigma concreto y sin resolver. En las dos primeras frases
  tiene que quedar claro qué es lo que no se sabe.
- Cada capítulo añade una pieza del rompecabezas y deja una duda nueva abierta.
- El penúltimo capítulo trae el dato o el giro que reordena todo lo anterior.
- El último capítulo separa lo que se sabe de lo que sigue sin saberse. No
  inventes una solución que no existe: el misterio sin resolver es el final.
- Tono: tensión contenida, frases cortas, sin exclamaciones ni sensacionalismo.""",
    "educativo": """Formato: educativo.
- Abre con el problema práctico que el espectador reconoce en su propia vida.
- Cada capítulo explica UNA sola idea y la aterriza con un ejemplo concreto.
- Las ideas van de la más simple a la más difícil y cada una se apoya en la
  anterior.
- El último capítulo resume en tres frases y dice qué puede hacer el espectador
  mañana mismo.
- Tono: claro y directo, sin jerga. Se explica como a un amigo, no como en una
  clase magistral.""",
    "storytelling": """Formato: storytelling.
- Abre en mitad de la escena más tensa de la historia, no por el principio.
- Presenta pronto a un protagonista con algo concreto en juego.
- Cada capítulo es una escena: dónde ocurre, cuándo y qué cambia al terminarla.
- Tiene que haber un momento en el que todo se tuerce y otro en el que el
  protagonista decide.
- El último capítulo cierra la historia y deja la lección implícita. No la
  expliques: una moraleja dicha en voz alta apaga el final.
- Tono: narrativo, en pasado, con detalles sensoriales concretos.""",
    "top_n": """Formato: top-N (lista).
- Abre diciendo cuántos elementos tiene la lista y por qué el último sorprende.
- Un capítulo por elemento, ordenados de menor a mayor interés.
- De cada elemento: qué es, por qué está en la lista y un detalle que el
  espectador no espera.
- El último capítulo remata con el elemento más fuerte y lanza una pregunta
  para los comentarios.
- No empieces dos capítulos con la misma estructura de frase.""",
    "noticias": """Formato: noticias.
- Abre con lo que ha pasado, en una frase y sin rodeos.
- Un capítulo para los hechos, otro para el contexto que falta en los
  titulares, otro para a quién afecta y cómo.
- Distingue siempre lo comprobado de lo que solo se especula, y dilo con esas
  palabras.
- Cierra con qué conviene mirar en las próximas semanas.
- No inventes cifras, fechas ni declaraciones. Si un dato no lo tienes, di que
  no se ha confirmado en vez de rellenarlo.""",
}


def is_known(format_id: str) -> bool:
    """True si el formato existe (y por tanto se le puede pedir un guion)."""
    return format_id in FORMATS


def instructions(format_id: str) -> str:
    """Bloque de prompt del formato. `ValueError` si no existe.

    Un formato desconocido es un error de configuración o un valor viejo en
    `ideas.suggested_format`: pedirle el guion a un prompt sin estructura daría
    un guion informe, así que se para antes de gastar la llamada.
    """
    bloque = FORMATS.get(format_id)
    if bloque is None:
        raise ValueError(
            f"formato de guion desconocido: {format_id!r};"
            f" los que hay son {sorted(FORMATS)}"
        )
    return bloque
