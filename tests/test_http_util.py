"""HTTP compartido: qué se reintenta, qué no, y cuánto se espera.

En este sistema 429, 500 y timeout son estados normales de operación, no
excepciones raras: el camino de fallo se prueba tanto como el feliz.

La red se intercepta en la frontera de transporte con `responses` (parchea el
`HTTPAdapter` de requests). En ningún test se toca la función de negocio.
"""

from __future__ import annotations

import pytest
import requests
import responses

from factory.research import http_util
from factory.research.http_util import SourceUnavailable, get_json, get_with_retries

URL = "https://api.ejemplo.test/datos"


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


@responses.activate
def test_una_respuesta_200_se_devuelve_sin_reintentar():
    responses.get(URL, json={"ok": True}, status=200)

    respuesta = get_with_retries(URL)

    assert respuesta.status_code == 200
    assert len(responses.calls) == 1


@responses.activate
def test_get_json_decodifica_el_cuerpo():
    responses.get(URL, json={"items": [1, 2, 3]}, status=200)

    assert get_json(URL) == {"items": [1, 2, 3]}


@responses.activate
def test_los_parametros_y_cabeceras_llegan_a_la_peticion():
    responses.get(URL, json={}, status=200)

    get_with_retries(URL, params={"q": "hábitos"}, headers={"User-Agent": "factory/0.1"})

    peticion = responses.calls[0].request
    assert "q=h%C3%A1bitos" in peticion.url
    assert peticion.headers["User-Agent"] == "factory/0.1"


@responses.activate
def test_toda_peticion_lleva_timeout():
    # Sin timeout, un servidor que no cierra la conexión cuelga el worker entero.
    responses.get(URL, json={}, status=200)

    get_with_retries(URL, timeout=7.5)

    assert responses.calls[0].request.req_kwargs["timeout"] == 7.5


# ---------------------------------------------------------------------------
# Lo que se reintenta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(http_util.RETRY_STATUSES))
@responses.activate
def test_los_estados_transitorios_se_reintentan_hasta_agotar_intentos(
    sin_esperas, status
):
    for _ in range(3):
        responses.get(URL, json={"error": "x"}, status=status)

    with pytest.raises(SourceUnavailable) as excinfo:
        get_with_retries(URL, max_attempts=3)

    assert len(responses.calls) == 3
    assert excinfo.value.status == status
    assert "agotados 3 intentos" in str(excinfo.value)


@responses.activate
def test_un_500_que_luego_responde_bien_devuelve_la_respuesta_buena(sin_esperas):
    responses.get(URL, json={"error": "boom"}, status=500)
    responses.get(URL, json={"ok": True}, status=200)

    assert get_json(URL) == {"ok": True}
    assert len(responses.calls) == 2


@responses.activate
def test_un_timeout_se_reintenta_y_acaba_sin_codigo_de_estado(sin_esperas):
    responses.get(URL, body=requests.Timeout("se acabó el tiempo"))
    responses.get(URL, body=requests.Timeout("se acabó el tiempo"))
    responses.get(URL, body=requests.Timeout("se acabó el tiempo"))

    with pytest.raises(SourceUnavailable) as excinfo:
        get_with_retries(URL, max_attempts=3)

    assert len(responses.calls) == 3
    assert excinfo.value.status is None
    assert "Timeout" in str(excinfo.value)


@responses.activate
def test_un_error_de_conexion_se_reintenta(sin_esperas):
    responses.get(URL, body=requests.ConnectionError("DNS caído"))
    responses.get(URL, json={"ok": True}, status=200)

    assert get_json(URL) == {"ok": True}
    assert len(responses.calls) == 2


@responses.activate
def test_con_un_solo_intento_no_hay_reintento_ni_espera(sin_esperas):
    responses.get(URL, json={}, status=503)

    with pytest.raises(SourceUnavailable):
        get_with_retries(URL, max_attempts=1)

    assert len(responses.calls) == 1
    assert sin_esperas == []


# ---------------------------------------------------------------------------
# Lo que NO se reintenta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
@responses.activate
def test_un_4xx_no_transitorio_falla_al_primer_intento(sin_esperas, status):
    responses.get(URL, json={"error": "no"}, status=status)

    with pytest.raises(SourceUnavailable) as excinfo:
        get_with_retries(URL, max_attempts=3)

    assert len(responses.calls) == 1, "un 4xx permanente no se reintenta jamás"
    assert excinfo.value.status == status
    assert "no transitorio" in str(excinfo.value)
    assert sin_esperas == []


@responses.activate
def test_el_status_viaja_en_la_excepcion_para_distinguir_sin_datos_de_caida(
    sin_esperas,
):
    # Wikipedia usa esto: 404 significa "sin serie de pageviews", no "caída".
    responses.get(URL, json={}, status=404)

    with pytest.raises(SourceUnavailable) as excinfo:
        get_with_retries(URL)

    assert excinfo.value.status == 404


@responses.activate
def test_un_fallo_de_requests_no_transitorio_no_se_reintenta(sin_esperas):
    responses.get(URL, body=requests.TooManyRedirects("bucle de redirecciones"))

    with pytest.raises(SourceUnavailable, match="TooManyRedirects"):
        get_with_retries(URL, max_attempts=3)

    assert len(responses.calls) == 1
    assert sin_esperas == []


@responses.activate
def test_un_certificado_invalido_no_deberia_reintentarse(sin_esperas):
    responses.get(URL, body=requests.exceptions.SSLError("certificado inválido"))

    with pytest.raises(SourceUnavailable, match="SSLError"):
        get_with_retries(URL, max_attempts=3)

    assert len(responses.calls) == 1


@responses.activate
def test_una_respuesta_200_que_no_es_json_es_fuente_no_disponible(sin_esperas):
    # Un portal cautivo o una página de error de Cloudflare con status 200.
    responses.get(URL, body="<html>502 Bad Gateway</html>", status=200)

    with pytest.raises(SourceUnavailable, match="no es JSON"):
        get_json(URL)

    assert len(responses.calls) == 1


# ---------------------------------------------------------------------------
# Retry-After y backoff
# ---------------------------------------------------------------------------


@responses.activate
def test_un_429_con_retry_after_alto_manda_sobre_el_backoff(sin_esperas):
    responses.get(URL, json={}, status=429, headers={"Retry-After": "25"})
    responses.get(URL, json={"ok": True}, status=200)

    get_json(URL)

    assert sin_esperas == [25.0]


@responses.activate
def test_el_retry_after_se_acota_a_treinta_segundos(sin_esperas):
    # Un servidor no puede dejar el worker parado diez minutos.
    responses.get(URL, json={}, status=429, headers={"Retry-After": "600"})
    responses.get(URL, json={"ok": True}, status=200)

    get_json(URL)

    assert sin_esperas == [http_util.MAX_RETRY_AFTER_SECONDS]


@responses.activate
def test_un_retry_after_pequeno_no_acorta_el_backoff(sin_esperas):
    responses.get(URL, json={}, status=429, headers={"Retry-After": "1"})
    responses.get(URL, json={"ok": True}, status=200)

    get_json(URL)

    # backoff del primer intento: 2s + jitter de hasta 1s; nunca el 1s pedido.
    assert 2.0 <= sin_esperas[0] < 3.0


@responses.activate
def test_el_backoff_crece_exponencialmente_entre_intentos(sin_esperas):
    for _ in range(3):
        responses.get(URL, json={}, status=503)

    with pytest.raises(SourceUnavailable):
        get_with_retries(URL, max_attempts=3)

    assert len(sin_esperas) == 2, "se espera entre intentos, no después del último"
    assert 2.0 <= sin_esperas[0] < 3.0
    assert 4.0 <= sin_esperas[1] < 5.0


@pytest.mark.parametrize(
    ("cabecera", "esperado"),
    [
        ({}, 0.0),                                        # sin cabecera
        ({"Retry-After": "12"}, 12.0),
        ({"Retry-After": "12.5"}, 12.5),
        ({"Retry-After": "0"}, 0.0),
        ({"Retry-After": "-5"}, 0.0),                     # valor absurdo
        ({"Retry-After": "999"}, 30.0),                   # tope
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 0.0),  # formato fecha: se ignora
        ({"Retry-After": ""}, 0.0),
        ({"Retry-After": "pronto"}, 0.0),
    ],
)
@responses.activate
def test_retry_after_se_interpreta_o_se_ignora_sin_romper(cabecera, esperado):
    responses.get(URL, json={}, status=429, headers=cabecera)
    respuesta = requests.get(URL)

    assert http_util._retry_after_seconds(respuesta) == esperado


def test_los_estados_reintentables_son_los_documentados():
    assert http_util.RETRY_STATUSES == frozenset({429, 500, 502, 503, 504})
