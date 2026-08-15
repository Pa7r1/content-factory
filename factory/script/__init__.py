"""Motor de guiones: de una idea aprobada a un guion con capítulos en `videos`.

Cinco formatos (misterio, educativo, storytelling, top_n, noticias) que solo
cambian el bloque de instrucciones del prompt. Si el modelo no responde o
devuelve algo que no pasa la criba, el job falla y el motivo queda en la base:
nunca se fabrica un guion de relleno.

Como el resto de módulos de dominio, solo habla con el sistema a través de la
base de datos (patrón blackboard).
"""
