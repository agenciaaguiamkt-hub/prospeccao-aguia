"""
Busca de empresas por CNAE (classificacao oficial da Receita Federal),
usando a API v5 da Casa dos Dados, que expoe os dados publicos de CNPJ.

POR QUE UMA FONTE SEPARADA: a Google Places API NAO tem campo de CNAE -
ela so faz busca por texto livre. Para filtrar por CNAE de verdade e
preciso consultar o cadastro de CNPJ da Receita. Em troca, o cadastro de
CNPJ nao tem site, Instagram nem nota de avaliacao - por isso, depois de
achar as empresas, cada uma e procurada tambem na Places API so para
pegar o link do Google Meu Negocio, o telefone e o site.

CUSTO: a Casa dos Dados cobra por CNPJ retornado (R$ 0,01 por CNPJ na
tabela consultada em agosto/2026 - confira em
https://portal.casadosdados.com.br/precos, pode ter mudado). Cada empresa
enriquecida gasta tambem 1 chamada da Places API.

AVISO DE INCERTEZA: este modulo foi escrito a partir da documentacao
oficial (https://docs.casadosdados.com.br/) e NAO foi testado contra a API
real, porque isso exige uma chave paga com saldo. Se algum campo vier
diferente do esperado, o mais provavel e que o nome do campo na resposta
tenha mudado - confira a doc antes de assumir que e bug deste codigo.
"""

import requests

URL_PESQUISA = "https://api.casadosdados.com.br/v5/cnpj/pesquisa"
URL_PLACES = "https://places.googleapis.com/v1/places:searchText"

# Quantos CNPJs pedir por pagina. Nao achei o teto oficial documentado com
# 100% de certeza, entao uso um valor conservador.
LIMITE_POR_PAGINA = 100

# Campos pedidos ao Google so para enriquecer: o minimo necessario, para
# nao pagar por dados que nao vao para a planilha.
FIELD_MASK_ENRIQUECER = (
    "places.displayName,"
    "places.formattedAddress,"
    "places.nationalPhoneNumber,"
    "places.websiteUri,"
    "places.googleMapsUri,"
    "places.rating,"
    "places.userRatingCount"
)


def normalizar_cnaes(texto):
    """Aceita CNAEs digitados de varios jeitos ('4520-0/01', '4520001',
    separados por virgula/espaco/ponto-e-virgula) e devolve uma lista de
    codigos so com digitos, do jeito que a API espera."""
    bruto = (texto or "").replace(";", ",").replace(" ", ",")
    codigos = []
    for parte in bruto.split(","):
        digitos = "".join(c for c in parte if c.isdigit())
        if digitos:
            codigos.append(digitos)
    return codigos


def buscar_empresas_por_cnae(
    api_key,
    cnaes,
    uf,
    municipio,
    limite_total=100,
    incluir_secundaria=True,
    somente_ativas=True,
    contador=None,
    progresso_callback=None,
):
    """Busca empresas por CNAE + UF + municipio na Casa dos Dados.

    - `cnaes`: lista de codigos CNAE so com digitos (use normalizar_cnaes).
    - `limite_total`: teto de empresas a trazer. IMPORTANTE: e isso que
      controla o custo, porque a cobranca e por CNPJ retornado.
    - `contador`: dict {"chamadas": 0}, incrementado a cada chamada HTTP.
    """
    if not api_key:
        raise RuntimeError(
            "Faltou a chave da Casa dos Dados (segredo CASA_DOS_DADOS_API_KEY). "
            "Pegue a sua em https://portal.casadosdados.com.br/plataforma/api/chave"
        )
    if not cnaes:
        raise RuntimeError("Informe ao menos um codigo CNAE.")

    headers = {"api-key": api_key, "Content-Type": "application/json"}
    empresas = []
    pagina = 1

    while len(empresas) < limite_total:
        faltam = limite_total - len(empresas)
        corpo = {
            "codigo_atividade_principal": cnaes,
            "incluir_atividade_secundaria": bool(incluir_secundaria),
            "limite": min(LIMITE_POR_PAGINA, faltam),
            "pagina": pagina,
        }
        if incluir_secundaria:
            corpo["codigo_atividade_secundaria"] = cnaes
        if somente_ativas:
            corpo["situacao_cadastral"] = ["ATIVA"]
        if uf:
            corpo["uf"] = [uf.strip().lower()]
        if municipio:
            corpo["municipio"] = [municipio.strip().lower()]

        resp = requests.post(URL_PESQUISA, json=corpo, headers=headers, timeout=60)
        if contador is not None:
            contador["chamadas"] = contador.get("chamadas", 0) + 1

        if resp.status_code == 401:
            raise RuntimeError(
                "Casa dos Dados recusou a chave (401). Confira o segredo "
                "CASA_DOS_DADOS_API_KEY."
            )
        if resp.status_code == 403:
            raise RuntimeError(
                "Casa dos Dados devolveu 403. Normalmente isso significa saldo "
                "zerado ou plano sem acesso a Pesquisa Avancada - confira em "
                "https://portal.casadosdados.com.br/"
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Erro na API da Casa dos Dados: {resp.status_code} - {resp.text[:300]}"
            )

        dados = resp.json()
        lote = dados.get("cnpjs", []) or []
        empresas.extend(lote)

        if progresso_callback:
            progresso_callback(len(empresas), min(limite_total, dados.get("total", limite_total) or limite_total))

        # Parou de vir gente: ou acabou o resultado, ou a pagina veio vazia.
        if not lote or len(lote) < corpo["limite"]:
            break
        pagina += 1

    return empresas[:limite_total]


def _texto_endereco(empresa):
    """Monta um endereco legivel a partir do bloco 'endereco' da resposta."""
    end = empresa.get("endereco", {}) or {}
    pedacos = [
        " ".join(x for x in [end.get("tipo_logradouro"), end.get("logradouro")] if x),
        end.get("numero"),
        end.get("complemento"),
        end.get("bairro"),
        end.get("municipio"),
        end.get("uf"),
        end.get("cep"),
    ]
    return ", ".join(str(p).strip() for p in pedacos if p and str(p).strip())


def _nomes_socios(empresa):
    """Nomes do quadro societario. Isso NAO e palpite: e o QSA publicado
    pela Receita Federal. Ainda assim, socio no papel nem sempre e quem
    toca o negocio no dia a dia."""
    socios = empresa.get("quadro_societario", []) or []
    nomes = [s.get("nome", "").strip() for s in socios if s.get("nome")]
    return "; ".join(nomes)


def buscar_no_google(nome, municipio, uf, google_api_key, contador=None):
    """Procura a empresa na Places API so para pegar o link do Google Meu
    Negocio, telefone e site. Usa 1 chamada por empresa.

    Devolve {} quando nao acha - e isso acontece bastante: empresa que nao
    tem perfil no Google, ou cujo nome fantasia na Receita e diferente do
    nome usado na fachada. Trate o resultado como 'melhor esforco'."""
    if not nome or not google_api_key:
        return {}

    consulta = " ".join(x for x in [nome, municipio, uf] if x)
    corpo = {"textQuery": consulta, "languageCode": "pt-BR", "pageSize": 1}
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": google_api_key,
        "X-Goog-FieldMask": FIELD_MASK_ENRIQUECER,
    }

    try:
        resp = requests.post(URL_PLACES, json=corpo, headers=headers, timeout=30)
    except requests.RequestException:
        if contador is not None:
            contador["chamadas"] = contador.get("chamadas", 0) + 1
        return {}

    if contador is not None:
        contador["chamadas"] = contador.get("chamadas", 0) + 1
    if resp.status_code != 200:
        return {}

    lugares = resp.json().get("places", []) or []
    if not lugares:
        return {}
    return lugares[0]


def eh_celular(telefone):
    """Classifica telefone BR como celular/fixo pela quantidade de digitos."""
    if not telefone:
        return "sem telefone"
    digitos = "".join(c for c in telefone if c.isdigit())
    if digitos.startswith("55") and len(digitos) > 11:
        digitos = digitos[2:]
    if len(digitos) == 11 and digitos[2] == "9":
        return "sim"
    if len(digitos) == 10:
        return "nao"
    return "indeterminado"


def montar_linha_cnae(empresa, dados_google=None):
    """Converte uma empresa da Casa dos Dados (+ o que o Google achou) numa
    linha da planilha."""
    dados_google = dados_google or {}
    end = empresa.get("endereco", {}) or {}
    situacao = empresa.get("situacao_cadastral", {}) or {}
    telefone = dados_google.get("nationalPhoneNumber", "")

    nome_fantasia = (empresa.get("nome_fantasia") or "").strip()
    razao_social = (empresa.get("razao_social") or "").strip()

    return {
        "Nome do estabelecimento": nome_fantasia or razao_social,
        "Razao social": razao_social,
        "Socios (Receita Federal)": _nomes_socios(empresa),
        "CNPJ": empresa.get("cnpj", ""),
        "Endereco": _texto_endereco(empresa),
        "Municipio": end.get("municipio", ""),
        "UF": end.get("uf", ""),
        "Situacao cadastral": situacao.get("situacao_cadastral", ""),
        "Data de abertura": empresa.get("data_abertura", ""),
        "Google Meu Negocio": dados_google.get("googleMapsUri", ""),
        "Telefone (Google)": telefone,
        "E celular?": eh_celular(telefone),
        "Site": dados_google.get("websiteUri", ""),
        "Nota": dados_google.get("rating", ""),
        "N de avaliacoes": dados_google.get("userRatingCount", ""),
    }
