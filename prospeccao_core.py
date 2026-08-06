"""
Lógica central da prospecção via Google Places API (Text Search - New).

Este módulo é usado tanto pelo script de terminal (prospeccao_places.py)
quanto pela interface web (app.py). Mantenha as duas pontas funcionando
importando as funções daqui em vez de duplicar código.

Este arquivo NUNCA deve conter a chave de API em texto puro - ela é
sempre passada como argumento (vinda de variável de ambiente ou de
st.secrets, dependendo de onde o código está rodando).
"""

import math
import re
import time
import unicodedata

import requests

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Campos de dado do lugar pedidos + nextPageToken (paginação, não é dado de
# lugar) + googleMapsUri (link do perfil no Google Maps/Google Meu Negócio).
# Aviso: não tenho 100% de certeza de que "googleMapsUri" é o nome exato e
# definitivo do campo na doc mais atual da API - confira em
# https://developers.google.com/maps/documentation/places/web-service/text-search
# se der erro de "campo desconhecido" ou custo inesperado.
FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.nationalPhoneNumber,"
    "places.websiteUri,"
    "places.rating,"
    "places.userRatingCount,"
    "places.googleMapsUri,"
    "nextPageToken"
)
# "places.id" foi adicionado para permitir deduplicar resultados quando você
# roda várias buscas (ver buscar_multiplas_queries) - é o Place ID interno
# do Google, não é exibido na planilha final. Não tenho confirmação 100%
# atualizada de que esse campo é gratuito/Essentials no faturamento atual;
# se notar custo mais alto que o esperado, vale conferir a tabela de SKUs
# na documentação.

PAGE_SIZE = 20  # máximo permitido por página pela Text Search (New)

# Isto NÃO é um limite de negócio (você pediu para não ter máximo). É só uma
# trava técnica de segurança para o loop não rodar para sempre caso a API
# devolva nextPageToken indefinidamente por algum bug/comportamento
# inesperado. Na prática, pela documentação e pelo que observamos em teste,
# a Text Search (New) tende a parar de paginar por volta de ~60 resultados
# (3 páginas de 20) - não tenho uma fonte oficial 100% explícita confirmando
# esse número como regra permanente, então trate como comportamento
# observado, não garantia contratual da API.
LIMITE_SEGURANCA_RESULTADOS = 500


def _buscar_com_body(body_base, api_key, max_resultados, contador, field_mask=None):
    """Faz a busca paginada dado um corpo de requisição já pronto
    (com textQuery e, opcionalmente, locationBias). Função interna
    compartilhada por buscar_lugares() e pela busca em grade."""
    teto = max_resultados if max_resultados is not None else LIMITE_SEGURANCA_RESULTADOS
    resultados = []
    page_token = None

    while len(resultados) < teto:
        body = dict(body_base)
        body["pageSize"] = PAGE_SIZE
        if page_token:
            body["pageToken"] = page_token

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": field_mask or FIELD_MASK,
        }

        resp = requests.post(SEARCH_URL, json=body, headers=headers, timeout=30)
        if contador is not None:
            contador["chamadas"] = contador.get("chamadas", 0) + 1

        if resp.status_code != 200:
            raise RuntimeError(f"Erro na Places API: {resp.status_code} - {resp.text}")

        data = resp.json()
        places = data.get("places", [])
        resultados.extend(places)

        page_token = data.get("nextPageToken")
        if not page_token or not places:
            break

        # Pequena espera para o nextPageToken ficar válido. Não achei
        # confirmação 100% oficial do tempo exato para a API New; 2s
        # costuma funcionar na prática (valor documentado na API legada).
        time.sleep(2)

    return resultados[:teto]


def buscar_lugares(query, api_key, max_resultados=None, contador=None):
    """Busca paginada na Places API (New), UMA busca de texto só.

    Importante: a Text Search tem um teto prático de resultados por busca
    (na prática, perto de ~60 - comportamento observado, não uma trava
    deste código). Se a cidade/categoria tiver mais estabelecimentos do
    que isso, use buscar_cidade_completa() em vez desta função - ela
    automatiza a divisão em sub-áreas geográficas para cobrir mais
    terreno, sem você precisar digitar buscas manuais.

    - `max_resultados`: se None (padrão), busca até a API parar de
      devolver nextPageToken (ou até o limite de segurança interno).
    - `contador`: dict opcional {"chamadas": 0}, incrementado a cada
      chamada HTTP feita à API.
    """
    body_base = {"textQuery": query, "languageCode": "pt-BR"}
    return _buscar_com_body(body_base, api_key, max_resultados, contador)


# ----------------------- BUSCA COMPLETA POR GRADE GEOGRÁFICA -----------------------
# Field mask mínimo só para descobrir a coordenada (lat/lng) de uma cidade -
# usa "places.location", separado do FIELD_MASK principal para não pedir
# campos de dado desnecessários nessa chamada de geocodificação.
_FIELD_MASK_LOCATION = "places.location"

KM_POR_GRAU_LAT = 111.32  # aproximação padrão, não varia com a latitude


def _geocodificar_cidade(cidade, api_key, contador=None):
    """Descobre a coordenada aproximada (lat, lng) do centro de uma cidade
    usando a própria Places API (1 chamada). Lança erro se não encontrar."""
    body = {"textQuery": cidade, "languageCode": "pt-BR"}
    resultados = _buscar_com_body(
        body, api_key, max_resultados=1, contador=contador, field_mask=_FIELD_MASK_LOCATION
    )
    if not resultados:
        raise RuntimeError(f"Não encontrei a localização de '{cidade}' na Places API.")
    loc = resultados[0].get("location", {})
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if lat is None or lng is None:
        raise RuntimeError(f"A Places API não devolveu coordenadas para '{cidade}'.")
    return lat, lng


def gerar_grade(lat_centro, lng_centro, largura_km, altura_km, espacamento_km):
    """Gera uma lista de pontos (lat, lng) cobrindo um retângulo de
    `largura_km` x `altura_km` centrado em (lat_centro, lng_centro),
    espaçados por `espacamento_km`. Aproximação simples (equirretangular),
    suficiente para uma cidade (não para áreas continentais)."""
    km_por_grau_lng = KM_POR_GRAU_LAT * math.cos(math.radians(lat_centro))

    n_passos_x = max(1, math.ceil(largura_km / espacamento_km))
    n_passos_y = max(1, math.ceil(altura_km / espacamento_km))

    pontos = []
    for i in range(n_passos_x + 1):
        offset_x_km = -largura_km / 2 + i * espacamento_km
        for j in range(n_passos_y + 1):
            offset_y_km = -altura_km / 2 + j * espacamento_km
            lat = lat_centro + offset_y_km / KM_POR_GRAU_LAT
            lng = lng_centro + offset_x_km / km_por_grau_lng
            pontos.append((lat, lng))
    return pontos


import time
def _normalizar_texto(txt):
    """Remove acentos e baixa a caixa, para comparar nomes de cidade de
    forma tolerante a variação de grafia (ex.: 'São José' == 'sao jose')."""
    if not txt:
        return ""
    nfkd = unicodedata.normalize("NFKD", txt)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.lower()


def _extrair_nome_cidade(cidade_busca):
    """Extrai só o nome da cidade de um texto tipo 'Floreal SP', removendo
    a sigla do estado no final quando presente (token isolado de 2 letras)."""
    texto = cidade_busca.strip()
    partes = texto.split()
    if len(partes) >= 2 and len(partes[-1]) == 2 and partes[-1].isalpha():
        return " ".join(partes[:-1])
    return texto


def _endereco_pertence_a_cidade(endereco, cidade_busca):
    """Confere se o endereço devolvido pela API é realmente da cidade
    buscada (nome da cidade aparece como substring do endereço formatado).
    O locationBias da Text Search (New) é só uma dica de onde procurar, não
    um filtro rígido - quando a categoria é rara numa cidade pequena, o
    Google preenche a resposta com estabelecimentos de cidades vizinhas."""
    if not endereco:
        return False
    nome_cidade = _normalizar_texto(_extrair_nome_cidade(cidade_busca))
    if not nome_cidade:
        return True
    return nome_cidade in _normalizar_texto(endereco)


def buscar_cidade_completa(import unicodedata

import requests
    categoria,
    cidade,
    api_key,
    largura_km=20,
    altura_km=20,
    espacamento_km=4,
    contador=None,
    progresso_callback=None,
    estatisticas=None,
):
    """Busca TODOS os estabelecimentos de `categoria` (ex.: "clínica de
    estética") numa cidade, contornando o teto de ~60 resultados por
    busca da Text Search.

    Como funciona: descobre o centro da cidade (1 chamada), gera uma
    grade de pontos cobrindo `largura_km` x `altura_km` ao redor desse
    centro, espaçados a cada `espacamento_km`, e faz uma busca de
    `categoria` (sem o nome da cidade, para não ancorar demais no centro)
    em cada ponto, usando locationBias para direcionar geograficamente.
    Os resultados de todos os pontos são somados e deduplicados pelo
    Place ID.

    Isso NÃO é mágica - é dividir o problema em pedaços pequenos o
    suficiente para cada um ficar abaixo do teto por busca. Quanto menor
    o `espacamento_km`, mais completo tende a ser o resultado, mas mais
    chamadas à API são feitas (mais custo). Ajuste `largura_km`/
    `altura_km` para cobrir cidades maiores.

    Aviso de incerteza: a estrutura exata do parâmetro `locationBias`
    (circle/center/radius) é a documentada para a Text Search (New) pelo
    que eu conheço, mas não tenho como testar contra a API real a partir
    daqui (meu ambiente não tem acesso à internet) - se der erro de
    parâmetro inválido, vale conferir a documentação atual.
    """
    lat_centro, lng_centro = _geocodificar_cidade(cidade, api_key, contador=contador)
    pontos = gerar_grade(lat_centro, lng_centro, largura_km, altura_km, espacamento_km)

    raio_m = min(50000, int(espacamento_km * 1000 * 0.75))

    vistos = set()
    combinados = []

    for idx, (lat, lng) in enumerate(pontos):
        if progresso_callback:
            progresso_callback(idx + 1, len(pontos))

        body = {
            "textQuery": categoria,
            "languageCode": "pt-BR",
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": raio_m,
                }
            },
        }
        lugares = _buscar_com_body(body, api_key, max_resultados=None, contador=contador)

        for lugar in lugares:
            chave = lugar.get("id") or (
                lugar.get("displayName", {}).get("text", "") + "|" + lugar.get("formattedAddress", "")
            )
            if chave in vistos:
                continue
            vistos.add(chave)
            combinados.append(lugar)

    combinados_da_cidade = [
        lugar for lugar in combinados
        if _endereco_pertence_a_cidade(lugar.get("formattedAddress", ""), cidade)
    ]

    if estatisticas is not None:
        estatisticas["total_bruto"] = len(combinados)
        estatisticas["total_filtrado"] = len(combinados_da_cidade)
        estatisticas["removidos"] = len(combinados) - len(combinados_da_cidade)

    return combinados_da_cidade


def buscar_multiplas_queries(queries, api_key, max_resultados_por_query=None, contador=None):
    """Roda buscar_lugares() para cada texto em `queries` e junta tudo,
    removendo duplicados (mesmo Place ID aparecendo em mais de uma busca).

    Por que isso existe: a Text Search (New) tem um teto prático de
    resultados por busca (na prática, algo perto de ~60 - comportamento
    observado, não documentação oficial explícita que eu tenha conferido
    100%). Se o número real de estabelecimentos for maior que isso, UMA
    busca genérica ("clínica de estética em Votuporanga SP") não trará o
    restante - o Google simplesmente não devolve mais nada além do teto
    para essa mesma busca, então repetir a busca idêntica traz os mesmos
    resultados de novo.

    A saída prática é dividir a busca em várias mais específicas, que cada
    uma cobre uma fatia diferente do universo de lugares - por exemplo, por
    bairro/região da cidade, ou por sub-categoria/sinônimo (ex.:
    "harmonização facial", "clínica de emagrecimento", "estética corporal"
    em vez de só "clínica de estética"). Cada busca dessas tem seu próprio
    teto de ~60, então juntas cobrem mais terreno. Isso só ajuda de fato se
    existir mais gente para encontrar - dividir uma busca que já esgotou o
    universo real de resultados não cria estabelecimentos novos.
    """
    vistos = set()
    combinados = []

    for query in queries:
        query = query.strip()
        if not query:
            continue
        lugares = buscar_lugares(query, api_key, max_resultados_por_query, contador=contador)
        for lugar in lugares:
            chave = lugar.get("id") or (
                lugar.get("displayName", {}).get("text", "") + "|" + lugar.get("formattedAddress", "")
            )
            if chave in vistos:
                continue
            vistos.add(chave)
            combinados.append(lugar)

    return combinados


def eh_celular(telefone):
    """Classifica telefone BR como celular/fixo pela quantidade de dígitos."""
    if not telefone:
        return "sem telefone"
    digitos = re.sub(r"\D", "", telefone)
    if digitos.startswith("55") and len(digitos) > 11:
        digitos = digitos[2:]
    if len(digitos) == 11 and digitos[2] == "9":
        return "sim"
    if len(digitos) == 10:
        return "não"
    return "indeterminado"


# Segmentos de path que não são nome de usuário (páginas institucionais do
# próprio Instagram, não perfil da clínica).
_IG_PATHS_RESERVADOS = {
    "accounts", "explore", "legal", "about", "developer", "business", "p",
    "reel", "reels", "stories", "tv", "embed", "graphql", "robots.txt",
    "oembed", "directory", "web", "api", "login", "share", "static",
}

# Regex ancorada no host real (exige "instagram.com" logo após "//" ou
# "//www.") - evita falso positivo com subdomínios como
# "static.cdninstagram.com" ou "scontent.cdninstagram.com", que contêm a
# string "instagram.com" mas NÃO são o domínio instagram.com.
_IG_LINK_RE = re.compile(
    r'href=["\'](?:https?:)?//(?:www\.)?instagram\.com(/[^"\'#?\s]*)?', re.IGNORECASE
)


def buscar_instagram(site_url, contador=None):
    """Acessa a home do site (chamada HTTP comum, NÃO é API do Google) e
    procura um link de PERFIL para instagram.com (ex.: instagram.com/nome).
    `contador` é um dict opcional {"chamadas": 0} incrementado a cada
    acesso a site."""
    if not site_url:
        return ""

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ProspeccaoBot/1.0)"}
        resp = requests.get(site_url, headers=headers, timeout=10)
        if contador is not None:
            contador["chamadas"] = contador.get("chamadas", 0) + 1
        if resp.status_code != 200:
            return "erro ao acessar site"

        html = resp.text
        for m in _IG_LINK_RE.finditer(html):
            path = (m.group(1) or "").strip("/")
            primeiro_segmento = path.split("/")[0] if path else ""
            if not primeiro_segmento:
                continue
            chave = primeiro_segmento.lower()
            if chave in _IG_PATHS_RESERVADOS:
                continue
            if re.search(r"\.(js|css|png|jpg|jpeg|svg|ico|json|php)$", chave):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", primeiro_segmento):
                continue
            return f"https://www.instagram.com/{primeiro_segmento}/"

        return "não encontrado"
    except requests.RequestException:
        if contador is not None:
            contador["chamadas"] = contador.get("chamadas", 0) + 1
        return "erro ao acessar site"


def _normalizar_link_instagram(url):
    url = url.split("?")[0].split("#")[0]
    if not url.endswith("/"):
        url += "/"
    return url


def resolver_instagram(site_url, contador=None):
    """Descobre o Instagram de um estabelecimento a partir do seu `site`.

    Bug corrigido: em muitos casos o Google já cadastra o próprio perfil do
    Instagram COMO o "site" do estabelecimento (comum em negócios pequenos
    que não têm site próprio). Antes, o código tentava abrir esse link como
    se fosse um site comum e fazia scraping da página em busca de outro
    link de Instagram dentro dela - o que quase sempre falhava (Instagram
    bloqueia acesso automatizado sem login, retornando erro ou uma parede
    de login), gerando "erro ao acessar site" mesmo com o link certo já
    disponível. Agora, se o `site_url` já é um link do instagram.com, ele é
    usado diretamente, sem tentar buscar - mais rápido, mais barato (uma
    chamada HTTP a menos) e sem essa falha."""
    if not site_url:
        return ""
    if "instagram.com" in site_url.lower():
        return _normalizar_link_instagram(site_url)
    return buscar_instagram(site_url, contador=contador)


# Lista de primeiros nomes comuns no Brasil, usada só para tentar
# identificar se o NOME DO ESTABELECIMENTO cita o nome do dono/responsável
# (comum em pequenos negócios, ex.: "Murilo Pneus", "Auto Mecânica do Zé").
# AVISO IMPORTANTE: isso não é dado oficial da Places API (ela não tem campo
# de "proprietário") - é um PALPITE por texto. Pode errar (nome de bairro ou
# cidade que coincide com um nome próprio, apelido, etc.) e não cobre todo
# nome brasileiro. Trate sempre como "a confirmar", nunca como fato.
NOMES_BR = {
    "joao","jose","antonio","francisco","carlos","paulo","pedro","lucas","luiz","marcos",
    "luis","gabriel","rafael","daniel","marcelo","bruno","eduardo","felipe","rodrigo",
    "marcio","andre","edson","fabio","alexandre","fernando","gustavo","ricardo","claudio",
    "roberto","sergio","vinicius","adriano","leandro","mario","wagner","henrique","diego",
    "ailton","alessandro","anderson","cesar","cicero","cristiano","denis","douglas","elias",
    "emerson","everton","geraldo","gilmar","guilherme","helio","hugo","ivan","jair",
    "jefferson","jorge","julio","junior","leonardo","maicon","manoel","mauricio","milton",
    "moacir","nelson","nilton","osvaldo","otavio","reinaldo","renato","robson","rogerio",
    "ronaldo","samuel","sandro","tiago","valdemir","valter","vicente","victor","vitor",
    "walter","wellington","william","maria","ana","francisca","antonia","adriana","juliana",
    "marcia","fernanda","patricia","aline","sandra","camila","amanda","bruna","jessica",
    "leticia","julia","luciana","vanessa","mariana","gabriela","valeria","simone","cristina",
    "daniela","debora","monica","sonia","tatiane","vera","viviane","claudia","denise",
    "elaine","eliane","fabiana","ivone","jaqueline","karina","kelly","luzia","marta",
    "michele","natalia","priscila","raquel","regina","renata","roberta","silvana","silvia",
    "suzana","teresa","thais","paulinho","zezinho","toninho","tonho","nene","bizo","helinho",
    "elinho","kendi","chico","gordo","indio","zeca","juninho","edu","edinho","reginaldo",
    "adilson","alberto","aldo","arnaldo","arthur","augusto","benedito","cassio","celso",
    "davi","david","dirceu","edilson","edimar","edivaldo","emanuel","emilio","erasmo",
    "estevao","euclides","eugenio","ezequiel","filipe","flavio","gerson","gilson",
    "heitor","ismael","israel","itamar","jacinto","jacob","jarbas","jeferson","jeronimo",
    "joaquim","joel","jonas","jonatas","josue","leomar","lucio","luciano","luizinho",
    "marciano","mateus","matheus","mauro","messias","miguel","murilo","neto","nivaldo",
    "odair","olavo","orlando","oswaldo","pascoal","raul","romario","romeu","romulo",
    "sebastiao","severino","silvio","tadeu","teodoro","tobias","ulisses","valdomiro",
    "valentim","valmir","vanderlei","vilson","wanderson","wilson","xavier","zacarias",
}

_PREFIXOS_SANTO = {"sao", "santo", "santa"}


def extrair_nome_provavel(nome_estabelecimento):
    """Tenta achar um primeiro nome brasileiro comum dentro do nome do
    estabelecimento. Ignora um nome logo após 'São/Santo/Santa' (evita
    confundir nome de santo/bairro com nome de pessoa, ex.: 'São Miguel').
    Heurística, não confirmação - pode ter falsos positivos/negativos."""
    tokens = re.findall(r"[A-Za-zÀ-ÿ]+", nome_estabelecimento)
    for i, tok in enumerate(tokens):
        norm = _normalizar_texto(tok)
        if norm not in NOMES_BR:
            continue
        if i > 0 and _normalizar_texto(tokens[i - 1]) in _PREFIXOS_SANTO:
            continue
        return tok
    return ""


def montar_linha(lugar):
    """Converte um item retornado pela Places API numa linha (dict) para a
    planilha, sem ainda resolver o Instagram (que exige acesso ao site)."""
    return {
        "Nome": lugar.get("displayName", {}).get("text", ""),
        "Provável nome do dono (não confirmado)": extrair_nome_provavel(lugar.get("displayName", {}).get("text", "")),
        "Endereço": lugar.get("formattedAddress", ""),
        "Telefone": lugar.get("nationalPhoneNumber", ""),
        "É celular?": eh_celular(lugar.get("nationalPhoneNumber", "")),
        "Site": lugar.get("websiteUri", ""),
        "Google Meu Negócio": lugar.get("googleMapsUri", ""),
        "Nota": lugar.get("rating", ""),
        "Nº de avaliações": lugar.get("userRatingCount", ""),
    }
