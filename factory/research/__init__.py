"""Motor de investigación: fuentes de señales, scorer y pipeline de ideas.

Cada fuente degrada por separado: si falla, la señal se marca ausente y el
scorer re-normaliza pesos. Este módulo solo habla con el resto del sistema a
través de la base de datos (patrón blackboard).
"""
