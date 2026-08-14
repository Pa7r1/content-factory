"""Cliente de Gemini: cuándo hay texto, cuándo es fuente caída y qué cuota gasta.

Las respuestas son muestras guardadas en `tests/fixtures/` con la forma que
devuelve `:generateContent` de la v1beta, incluidos los dos desenlaces que en
operación son normales: el prompt bloqueado por filtros (200 sin candidatos) y
el candidato truncado sin texto.

Ninguna prueba sale a la red: `responses` intercepta el transporte y el fixture
autouse `_sin_clave_de_gemini` garantiza que no se hereda una clave real.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from typing import Any

import pytest
import responses

from factory.core import llm, quota
from factory.core.http_util import SourceUnavailable

URL = f"{llm.API_BASE}/{llm.MODEL}:generateContent"

# Presupuesto real de config/settings.yaml: 200 peticiones, corte efectivo 160.
PRESUPUESTO_EFECTIVO = 160
# Se reserva el peor caso (un 429 reintentado son dos peticiones reales), pero
# se liquida lo que de verdad se gastó.
RESERVA_DEL_PEOR_CASO = llm.COST_PER_REQUEST * llm.MAX_ATTEMPTS
COSTE_DE_UNA_PETICION = llm.COST_PER_REQUEST


@pytest.fixture
def con_clave(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "clave-de-prueba-no-real")


@pytest.fixture
def respuesta_con(muestra):
    """Devuelve la muestra real de `:generateContent` con otro texto dentro."""

    def construir(texto: str) -> dict[str, Any]:
        data = copy.deepcopy(muestra("gemini_generate_content.json"))
        data["candidates"][0]["content"]["parts"][0]["text"] = texto
        return data

    return construir


def _cuerpo_enviado() -> dict[str, Any]:
    return json.loads(responses.calls[0].request.body)


# ---------------------------------------------------------------------------
# Sin clave: fuente caída, nunca texto vacío
# ---------------------------------------------------------------------------


def test_sin_clave_en_el_entorno_el_modelo_no_esta_disponible(conn: sqlite3.Connection):
    with pytest.raises(SourceUnavailable, match="GOOGLE_AI_API_KEY"):
        llm.generate_json("dame tres temas")


def test_sin_clave_no_se_gasta_ni_una_unidad_de_cuota(conn: sqlite3.Connection):
    with pytest.raises(SourceUnavailable):
        llm.generate_json("dame tres temas")

    assert quota.usage_today("gemini") == 0


def test_una_clave_en_blanco_cuenta_como_ausente(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "   ")

    with pytest.raises(SourceUnavailable, match="GOOGLE_AI_API_KEY"):
        llm.generate_text("dame tres temas")


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


@responses.activate
def test_generate_json_devuelve_el_json_del_modelo_ya_decodificado(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.post(URL, json=muestra("gemini_generate_content.json"), status=200)

    resultado = llm.generate_json("dame un tema")

    assert resultado["temas"][0]["formato"] == "educativo"


@responses.activate
def test_generate_text_devuelve_el_texto_del_primer_candidato(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    responses.post(URL, json=respuesta_con("Un hábito no es fuerza de voluntad."), status=200)

    assert llm.generate_text("escribe una frase") == "Un hábito no es fuerza de voluntad."


@responses.activate
def test_las_partes_del_candidato_se_concatenan(
    conn: sqlite3.Connection, con_clave, muestra
):
    data = copy.deepcopy(muestra("gemini_generate_content.json"))
    data["candidates"][0]["content"]["parts"] = [{"text": "primera"}, {"text": " y segunda"}]
    responses.post(URL, json=data, status=200)

    assert llm.generate_text("escribe") == "primera y segunda"


@responses.activate
def test_la_clave_viaja_en_la_cabecera_y_no_en_la_url(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    # En la URL acabaría en cualquier log de red o de proxy.
    responses.post(URL, json=respuesta_con("hola"), status=200)

    llm.generate_text("hola")

    peticion = responses.calls[0].request
    assert peticion.headers["x-goog-api-key"] == "clave-de-prueba-no-real"
    assert "clave-de-prueba-no-real" not in peticion.url


@responses.activate
def test_el_prompt_y_la_instruccion_de_sistema_van_en_el_cuerpo(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    responses.post(URL, json=respuesta_con("hola"), status=200)

    llm.generate_text("dame tres temas", system="responde solo con JSON")

    cuerpo = _cuerpo_enviado()
    assert cuerpo["contents"][0]["parts"][0]["text"] == "dame tres temas"
    assert cuerpo["systemInstruction"]["parts"][0]["text"] == "responde solo con JSON"


@responses.activate
def test_sin_instruccion_de_sistema_el_campo_no_se_envia(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    responses.post(URL, json=respuesta_con("hola"), status=200)

    llm.generate_text("dame tres temas")

    assert "systemInstruction" not in _cuerpo_enviado()


@responses.activate
def test_en_modo_json_se_pide_la_respuesta_como_json(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.post(URL, json=muestra("gemini_generate_content.json"), status=200)

    llm.generate_json("dame tres temas")

    assert _cuerpo_enviado()["generationConfig"]["responseMimeType"] == "application/json"


@responses.activate
def test_en_modo_texto_no_se_pide_mime_type(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    responses.post(URL, json=respuesta_con("hola"), status=200)

    llm.generate_text("dame tres temas")

    assert "responseMimeType" not in _cuerpo_enviado()["generationConfig"]


@responses.activate
def test_el_thinking_se_apaga_para_que_la_respuesta_no_llegue_truncada(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    # Con thinking encendido, el modelo gasta los tokens de salida pensando y
    # devuelve un JSON a medias con finishReason MAX_TOKENS.
    responses.post(URL, json=respuesta_con("hola"), status=200)

    llm.generate_text("dame tres temas")

    config = _cuerpo_enviado()["generationConfig"]
    assert config["thinkingConfig"]["thinkingBudget"] == 0


@responses.activate
def test_la_peticion_lleva_timeout_explicito(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    responses.post(URL, json=respuesta_con("hola"), status=200)

    llm.generate_text("dame tres temas")

    assert responses.calls[0].request.req_kwargs["timeout"] == llm.TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Cuota: reservar antes, reconciliar después
# ---------------------------------------------------------------------------


@responses.activate
def test_una_generacion_que_acierta_a_la_primera_se_liquida_por_una_peticion(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    # Se reserva el peor caso, pero al liquidar se corrige a la baja: si no
    # hubo reintento, cobrar dos peticiones se comería medio presupuesto.
    responses.post(URL, json=respuesta_con("hola"), status=200)

    llm.generate_text("dame tres temas")

    assert quota.usage_today("gemini") == COSTE_DE_UNA_PETICION
    fila = conn.execute("SELECT status, detail FROM api_usage").fetchone()
    assert fila["status"] == "settled"
    assert llm.MODEL in fila["detail"]


@responses.activate
def test_la_cuota_se_reserva_antes_de_disparar_la_peticion(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    # Si el proceso muere a mitad de llamada, el contador tiene que estar por
    # encima del gasto real, nunca por debajo.
    gastado_durante_la_llamada: list[int] = []

    def responder(peticion):
        gastado_durante_la_llamada.append(quota.usage_today("gemini"))
        return 200, {}, json.dumps(respuesta_con("hola"))

    responses.add_callback(responses.POST, URL, callback=responder)

    llm.generate_text("dame tres temas")

    assert gastado_durante_la_llamada == [RESERVA_DEL_PEOR_CASO]


@responses.activate
def test_una_llamada_que_falla_no_devuelve_la_cuota_ya_gastada(
    conn: sqlite3.Connection, con_clave, sin_esperas
):
    responses.post(URL, json={"error": "rate limit"}, status=429)
    responses.post(URL, json={"error": "rate limit"}, status=429)

    with pytest.raises(SourceUnavailable):
        llm.generate_text("dame tres temas")

    assert quota.usage_today("gemini") == RESERVA_DEL_PEOR_CASO
    assert conn.execute("SELECT status FROM api_usage").fetchone()["status"] == "reserved"


@responses.activate
def test_con_la_cuota_agotada_no_se_llega_a_llamar_al_modelo(
    conn: sqlite3.Connection, con_clave
):
    quota.reserve("gemini", PRESUPUESTO_EFECTIVO, 200)

    with pytest.raises(quota.QuotaExceeded):
        llm.generate_text("dame tres temas")

    assert len(responses.calls) == 0


# ---------------------------------------------------------------------------
# Fallos del modelo: todos son fuente caída, ninguno es un dato vacío
# ---------------------------------------------------------------------------


@responses.activate
def test_un_prompt_bloqueado_por_seguridad_es_una_fuente_caida(
    conn: sqlite3.Connection, con_clave, muestra
):
    # 200 sin candidatos: el modelo no puede dar dato, no es que no haya dato.
    responses.post(URL, json=muestra("gemini_bloqueado.json"), status=200)

    with pytest.raises(SourceUnavailable, match="SAFETY"):
        llm.generate_json("dame tres temas")


@responses.activate
def test_un_candidato_truncado_sin_texto_es_una_fuente_caida(
    conn: sqlite3.Connection, con_clave, muestra
):
    responses.post(URL, json=muestra("gemini_truncado.json"), status=200)

    with pytest.raises(SourceUnavailable, match="MAX_TOKENS"):
        llm.generate_json("dame tres temas")


@responses.activate
def test_una_respuesta_sin_candidatos_ni_motivo_tambien_es_fuente_caida(
    conn: sqlite3.Connection, con_clave
):
    responses.post(URL, json={"candidates": []}, status=200)

    with pytest.raises(SourceUnavailable, match="sin motivo"):
        llm.generate_json("dame tres temas")


@responses.activate
def test_una_respuesta_con_forma_inesperada_es_una_fuente_caida(
    conn: sqlite3.Connection, con_clave
):
    responses.post(URL, json=["esto no es un objeto"], status=200)

    with pytest.raises(SourceUnavailable, match="forma inesperada"):
        llm.generate_json("dame tres temas")


@responses.activate
def test_un_texto_que_no_es_json_es_una_fuente_caida_no_un_dato_vacio(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    responses.post(URL, json=respuesta_con("Claro, aquí tienes tres temas:"), status=200)

    with pytest.raises(SourceUnavailable, match="no es JSON"):
        llm.generate_json("dame tres temas")


@responses.activate
def test_un_texto_que_no_es_json_si_vale_como_texto(
    conn: sqlite3.Connection, con_clave, respuesta_con
):
    responses.post(URL, json=respuesta_con("Claro, aquí tienes tres temas:"), status=200)

    assert llm.generate_text("dame tres temas") == "Claro, aquí tienes tres temas:"


# ---------------------------------------------------------------------------
# Reintentos
# ---------------------------------------------------------------------------


@responses.activate
def test_un_429_se_reintenta_hasta_agotar_los_intentos(
    conn: sqlite3.Connection, con_clave, sin_esperas
):
    responses.post(URL, json={"error": "rate limit"}, status=429)
    responses.post(URL, json={"error": "rate limit"}, status=429)

    with pytest.raises(SourceUnavailable, match="agotados"):
        llm.generate_text("dame tres temas")

    assert len(responses.calls) == llm.MAX_ATTEMPTS


@responses.activate
def test_un_500_transitorio_se_reintenta_y_la_segunda_vez_sale_bien(
    conn: sqlite3.Connection, con_clave, sin_esperas, respuesta_con
):
    responses.post(URL, json={"error": "interno"}, status=500)
    responses.post(URL, json=respuesta_con("hola"), status=200)

    assert llm.generate_text("dame tres temas") == "hola"
    assert len(responses.calls) == 2


@responses.activate
def test_un_400_no_se_reintenta_porque_daria_el_mismo_error(
    conn: sqlite3.Connection, con_clave, sin_esperas
):
    responses.post(URL, json={"error": "API key not valid"}, status=400)

    with pytest.raises(SourceUnavailable, match="400"):
        llm.generate_text("dame tres temas")

    assert len(responses.calls) == 1
