"""Reddit: parseo del JSON público, tolerancia a subreddits caídos y lógica pura.

`engagement_in_posts` es el trozo que de verdad decide la señal y no toca red:
se prueba con tablas de casos, incluidos los que el tokenizador maltrata.
"""

from __future__ import annotations

import re

import pytest
import responses

from factory.research import reddit_source
from factory.research.http_util import SourceUnavailable

TOP_RE = re.compile(r"https://www\.reddit\.com/r/[^/]+/top\.json.*")

POSTS = [
    {"title": "¿Cómo mantuvisteis vuestros hábitos más de un año?", "ups": 842, "num_comments": 213},
    {"title": "La disciplina venció a la motivación: mi año de cambios", "ups": 1503, "num_comments": 401},
    {"title": "Recomendación de libro sobre finanzas", "ups": 97, "num_comments": 12},
]


# ---------------------------------------------------------------------------
# engagement_in_posts: lógica pura
# ---------------------------------------------------------------------------


def test_el_engagement_suma_upvotes_y_comentarios_de_los_posts_que_casan():
    resultado = reddit_source.engagement_in_posts(POSTS, "hábitos")

    assert resultado["engagement"] == 842 + 213
    assert resultado["matched_posts"] == [
        "¿Cómo mantuvisteis vuestros hábitos más de un año?"
    ]


def test_una_keyword_que_casa_con_varios_posts_los_suma_todos():
    resultado = reddit_source.engagement_in_posts(POSTS, "disciplina motivación")

    assert resultado["engagement"] == 1503 + 401
    assert len(resultado["matched_posts"]) == 1


def test_una_keyword_sin_coincidencias_da_cero_medido_no_ausente():
    resultado = reddit_source.engagement_in_posts(POSTS, "criptomonedas")

    assert resultado["engagement"] == 0
    assert resultado["matched_posts"] == []


def test_sin_posts_el_engagement_es_cero():
    assert reddit_source.engagement_in_posts([], "hábitos")["engagement"] == 0


def test_la_busqueda_ignora_mayusculas_y_minusculas():
    posts = [{"title": "HÁBITOS QUE FUNCIONAN", "ups": 10, "num_comments": 5}]

    assert reddit_source.engagement_in_posts(posts, "Hábitos")["engagement"] == 15


@pytest.mark.parametrize(
    ("keyword", "tokens"),
    [
        ("crecimiento personal", ["crecimiento", "personal"]),
        ("historias de la biblia", ["historias", "biblia"]),   # fuera stopwords
        ("traición y venganza", ["traición", "venganza"]),
        ("fe", ["fe"]),                        # una palabra corta: se usa entera
        ("de la", ["de la"]),                  # todo stopwords: se usa entera
        ("  Jesús  ", ["jesús"]),              # se normaliza a minúsculas
        ("top 5", ["top 5"]),                  # ambas demasiado cortas
    ],
)
def test_los_tokens_significativos_descartan_stopwords_y_palabras_cortas(keyword, tokens):
    assert reddit_source._significant_tokens(keyword) == tokens


def test_una_keyword_de_una_palabra_corta_sigue_buscando_algo():
    # Con "fe" el tokenizador se quedaría sin nada; usa la keyword entera.
    posts = [{"title": "Mi camino de fe este año", "ups": 30, "num_comments": 4}]

    assert reddit_source.engagement_in_posts(posts, "fe")["engagement"] == 34


def test_el_emparejado_es_por_subcadena_no_por_palabra_completa():
    # Comportamiento actual documentado: "hábito" casa dentro de "hábitos".
    posts = [{"title": "Mis hábitos diarios", "ups": 10, "num_comments": 0}]

    assert reddit_source.engagement_in_posts(posts, "hábito")["engagement"] == 10


# ---------------------------------------------------------------------------
# top_posts
# ---------------------------------------------------------------------------


@responses.activate
def test_top_posts_extrae_titulo_ups_y_comentarios(muestra):
    responses.get(TOP_RE, json=muestra("reddit_top.json"), status=200)

    posts = reddit_source.top_posts("DesarrolloPersonal")

    assert posts == POSTS


@responses.activate
def test_top_posts_pide_el_top_de_la_semana_con_su_user_agent(muestra):
    responses.get(TOP_RE, json=muestra("reddit_top.json"), status=200)

    reddit_source.top_posts("DesarrolloPersonal")

    peticion = responses.calls[0].request
    assert "/r/DesarrolloPersonal/top.json" in peticion.url
    assert "t=week" in peticion.url
    assert "content-factory-research" in peticion.headers["User-Agent"]


@responses.activate
def test_un_listado_vacio_devuelve_lista_vacia():
    responses.get(TOP_RE, json={"kind": "Listing", "data": {"children": []}}, status=200)

    assert reddit_source.top_posts("subredditmuerto") == []


@responses.activate
def test_un_403_de_reddit_no_se_reintenta_en_bucle(sin_esperas):
    # Reddit bloquea IPs de datacenter: insistir es ruido y más bloqueo.
    responses.get(TOP_RE, json={"message": "Forbidden"}, status=403)

    with pytest.raises(SourceUnavailable):
        reddit_source.top_posts("Cristianismo")

    assert len(responses.calls) == 1


@responses.activate
def test_un_429_de_reddit_tampoco_se_reintenta(sin_esperas):
    responses.get(TOP_RE, json={}, status=429)

    with pytest.raises(SourceUnavailable):
        reddit_source.top_posts("Cristianismo")

    assert len(responses.calls) == 1
    assert sin_esperas == []


# ---------------------------------------------------------------------------
# collect_top_posts
# ---------------------------------------------------------------------------


@responses.activate
def test_collect_top_posts_junta_los_posts_de_todos_los_subreddits(
    sin_esperas, muestra
):
    responses.get(TOP_RE, json=muestra("reddit_top.json"), status=200)

    posts = reddit_source.collect_top_posts(["a", "b", "c"])

    assert len(posts) == 9
    assert len(responses.calls) == 3


@responses.activate
def test_un_subreddit_caido_no_tumba_al_resto(sin_esperas, muestra):
    responses.get(TOP_RE, json={"message": "Forbidden"}, status=403)
    responses.get(TOP_RE, json=muestra("reddit_top.json"), status=200)

    posts = reddit_source.collect_top_posts(["bloqueado", "vivo"])

    assert len(posts) == 3


@responses.activate
def test_si_ningun_subreddit_responde_la_fuente_esta_caida(sin_esperas):
    responses.get(TOP_RE, json={"message": "Forbidden"}, status=403)

    with pytest.raises(SourceUnavailable, match="ningún subreddit respondió"):
        reddit_source.collect_top_posts(["a", "b"])


@responses.activate
def test_el_motivo_de_cada_subreddit_caido_queda_en_el_mensaje(sin_esperas):
    responses.get(TOP_RE, json={}, status=403)

    with pytest.raises(SourceUnavailable) as excinfo:
        reddit_source.collect_top_posts(["Cristianismo", "Biblia"])

    assert "r/Cristianismo" in str(excinfo.value)
    assert "r/Biblia" in str(excinfo.value)


@responses.activate
def test_se_espera_entre_subreddits_para_no_parecer_una_rafaga(sin_esperas, muestra):
    responses.get(TOP_RE, json=muestra("reddit_top.json"), status=200)

    reddit_source.collect_top_posts(["a", "b", "c"])

    # Dos pausas para tres subreddits: antes del segundo y del tercero.
    assert sin_esperas == [reddit_source.PAUSE_BETWEEN_CALLS] * 2


@responses.activate
def test_si_todos_responden_pero_ninguno_trae_posts_la_senal_esta_ausente(sin_esperas):
    # Regresión: devolver [] aquí hacía que el engagement de CADA keyword del
    # nicho valiese 0 y el scorer penalizase por falta de datos en vez de
    # re-normalizar. 200 con `children` vacío no es "cero interés", es "no hay
    # dato" — y es además el cuerpo que sirve Reddit bajo rate-limit.
    responses.get(TOP_RE, json={"kind": "Listing", "data": {"children": []}}, status=200)

    with pytest.raises(SourceUnavailable, match="ninguno trajo posts"):
        reddit_source.collect_top_posts(["Cristianismo", "Biblia"])

    assert len(responses.calls) == 2


@responses.activate
def test_el_mensaje_nombra_los_subreddits_que_respondieron_vacios(sin_esperas):
    responses.get(TOP_RE, json={"kind": "Listing", "data": {"children": []}}, status=200)

    with pytest.raises(SourceUnavailable) as excinfo:
        reddit_source.collect_top_posts(["Cristianismo", "Biblia"])

    assert "r/Cristianismo" in str(excinfo.value)
    assert "r/Biblia" in str(excinfo.value)


@responses.activate
def test_un_cuerpo_de_error_servido_con_200_tampoco_cuenta_como_senal(sin_esperas):
    # Bajo rate-limit Reddit contesta 200 con un JSON que no es un Listing.
    responses.get(TOP_RE, json={"message": "Too Many Requests", "error": 429}, status=200)

    with pytest.raises(SourceUnavailable, match="ninguno trajo posts"):
        reddit_source.collect_top_posts(["Cristianismo"])


@responses.activate
def test_un_subreddit_vacio_no_estropea_los_posts_del_que_si_respondio(
    sin_esperas, muestra
):
    responses.get(TOP_RE, json={"kind": "Listing", "data": {"children": []}}, status=200)
    responses.get(TOP_RE, json=muestra("reddit_top.json"), status=200)

    posts = reddit_source.collect_top_posts(["vacio", "vivo"])

    assert len(posts) == 3


@responses.activate
def test_una_lista_de_subreddits_vacia_lanza_un_error_que_dice_por_que(sin_esperas):
    # Se lanza en vez de devolver []: con lista vacía el engagement de cada
    # keyword valdría 0 y el scorer penalizaría por falta de datos en lugar de
    # re-normalizar. SourceUnavailable es lo que el pipeline ya sabe degradar.
    with pytest.raises(SourceUnavailable, match="sin subreddits configurados"):
        reddit_source.collect_top_posts([])

    assert len(responses.calls) == 0
