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

import unicodedata
from datetime import date

import requests

URL_PESQUISA = "https://api.casadosdados.com.br/v5/cnpj/pesquisa"
URL_SALDO = "https://api.casadosdados.com.br/v5/saldo"
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
    "places.googleMapsUri"
)


UFS = {
    "ac", "al", "ap", "am", "ba", "ce", "df", "es", "go", "ma", "mt", "ms",
    "mg", "pa", "pb", "pr", "pe", "pi", "rj", "rn", "rs", "ro", "rr", "sc",
    "sp", "se", "to",
}


def _tirar_acentos(txt):
    nfkd = unicodedata.normalize("NFKD", txt or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def variantes_municipio(texto):
    """Devolve as grafias do municipio que valem a pena mandar na busca.

    POR QUE ISSO EXISTE: a doc da Casa dos Dados usa exemplos SEM acento
    ("sao paulo"), porque o cadastro da Receita guarda os nomes sem acento.
    Resultado: buscar "aracatuba" acha e buscar a mesma coisa com cedilha
    nao acha nada. Foi exatamente isso que fez Aracatuba voltar vazio
    enquanto Votuporanga (que nao tem acento) funcionava.

    Como o campo `municipio` da API aceita uma LISTA, mando as duas grafias
    de uma vez e deixo a API casar com a que existir no banco dela. Assim
    funciona tanto se ela guardar com acento quanto sem - nao consegui
    confirmar qual das duas e a oficial, entao cubro as duas.

    Tambem tolera a UF digitada junto ("Aracatuba/SP", "Aracatuba - SP").
    """
    txt = (texto or "").strip()
    if len(txt) > 3 and txt[-3] in "/,- " and txt[-2:].lower() in UFS:
        txt = txt[:-3]
    txt = txt.strip(" ,/-")

    com_acento = txt.lower()
    sem_acento = _tirar_acentos(txt).lower()
    if sem_acento == com_acento:
        return [com_acento]
    return [sem_acento, com_acento]


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
            corpo["uf"] = [_tirar_acentos(uf).strip().lower()]
        if municipio:
            corpo["municipio"] = variantes_municipio(municipio)

        # tipo_resultado=completo e OBRIGATORIO para vir endereco, socios,
        # data de abertura, porte etc. O padrao da API e "simples", que
        # devolve so CNPJ, razao social, nome fantasia e situacao - foi o
        # que gerou a primeira planilha quase vazia.
        resp = requests.post(
            URL_PESQUISA,
            json=corpo,
            headers=headers,
            params={"tipo_resultado": "completo"},
            timeout=60,
        )
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


def consultar_saldo(api_key, timeout=15):
    """Le o saldo REAL da conta na Casa dos Dados (GET /v5/saldo).

    POR QUE LER DA FONTE em vez de manter um contador proprio: qualquer
    consulta feita fora deste app - pelo site deles, por outra pessoa da
    equipe, por outra ferramenta - nao passaria pelo nosso contador, e o
    numero na tela viraria mentira em poucos dias. Lendo o saldo deles, o
    que aparece e sempre o que a Casa dos Dados vai cobrar de verdade.
    Como efeito colateral, a renovacao mensal do plano tambem se resolve
    sozinha: quando eles recarregam, o numero sobe aqui.

    Devolve (saldo_total, detalhes_por_tipo). O `detalhes` e um dict do
    tipo {"assinatura": {"valor": 4850, "expira_em": "..."}, ...}."""
    if not api_key:
        raise RuntimeError("Sem chave da Casa dos Dados configurada.")

    resp = requests.get(URL_SALDO, headers={"api-key": api_key}, timeout=timeout)
    if resp.status_code == 401:
        raise RuntimeError("a chave foi recusada (401)")
    if resp.status_code != 200:
        raise RuntimeError(f"a API respondeu {resp.status_code}")

    dados = resp.json() or {}
    return int(dados.get("saldo_total") or 0), (dados.get("saldos") or {})


def proxima_renovacao(hoje=None, dia=18):
    """Proxima data em que o plano renova. O padrao e dia 18, que e a data
    de cobranca do plano contratado - se mudar de plano, mude aqui."""
    hoje = hoje or date.today()
    if hoje.day < dia:
        return date(hoje.year, hoje.month, dia)
    if hoje.month == 12:
        return date(hoje.year + 1, 1, dia)
    return date(hoje.year, hoje.month + 1, dia)


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
    toca o negocio no dia a dia.

    MEI nao tem quadro societario, mas nesse caso a razao social JA E o
    nome da pessoa, precedido do numero do CPF/CNPJ - por exemplo
    "97.542.972 GISELI FERREIRA". Entao, quando o QSA vem vazio, tiro o
    numero da frente e aproveito o nome."""
    socios = empresa.get("quadro_societario", []) or []
    nomes = [s.get("nome", "").strip() for s in socios if s.get("nome")]
    if nomes:
        return "; ".join(nomes)

    razao = (empresa.get("razao_social") or "").strip()
    partes = razao.split()
    while partes and not any(c.isalpha() for c in partes[0]):
        partes.pop(0)
    limpo = " ".join(partes)
    return limpo if limpo and limpo != razao else ""


def _atividade_principal(empresa):
    """Devolve (codigo_cnae, descricao) da atividade principal."""
    ativ = empresa.get("atividade_principal") or {}
    if not isinstance(ativ, dict):
        return "", ""
    return (ativ.get("codigo") or ""), (ativ.get("descricao") or "")


def _so_digitos(texto):
    return "".join(c for c in str(texto or "") if c.isdigit())


def nome_para_busca(empresa):
    """Nome que vale a pena mandar para o Google.

    Prefere o nome fantasia. Sem ele, usa a razao social - mas tirando os
    numeros de CPF/CNPJ que a Receita gruda no nome do MEI, seja na frente
    ("69.194.265 ISABELLE FALEIROS DE SOUZA") ou no fim ("MARLI APARECIDA
    DE MELO OLIVEIRA 044080738"). Mandar esses digitos junto estraga a
    busca: o Google passa a procurar o numero, nao a pessoa."""
    fantasia = (empresa.get("nome_fantasia") or "").strip()
    if fantasia:
        return fantasia

    partes = (empresa.get("razao_social") or "").strip().split()
    while partes and not any(c.isalpha() for c in partes[0]):
        partes.pop(0)
    while partes and not any(c.isalpha() for c in partes[-1]):
        partes.pop()
    return " ".join(partes)


def telefone_da_receita(empresa):
    """Telefone declarado no cadastro da Receita Federal.

    Prefere celular, que costuma ter WhatsApp. ATENCAO: esse numero e o que
    a empresa declarou no cadastro, pode ter anos e estar desligado. Por
    isso ele so entra como reserva, quando o Google nao tem nada - e a
    planilha marca a origem para voce saber no que confiar."""
    contatos = empresa.get("contato_telefonico", []) or []
    if not isinstance(contatos, list):
        return ""
    celulares = [c for c in contatos if isinstance(c, dict) and c.get("tipo") == "celular"]
    for c in celulares + [x for x in contatos if isinstance(x, dict)]:
        completo = (c.get("completo") or "").strip()
        if completo:
            return completo
        ddd, numero = str(c.get("ddd") or "").strip(), str(c.get("numero") or "").strip()
        if numero:
            return f"({ddd}) {numero}" if ddd else numero
    return ""


def _consultar_places(consulta, google_api_key, contador=None):
    """Uma chamada a Places API Text Search. Devolve o 1o lugar ou {}."""
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
    return lugares[0] if lugares else {}


def enriquecer_com_google(empresa, google_api_key, contador=None):
    """Procura a empresa no Google para pegar telefone, site e link do
    Google Meu Negocio. Faz ate DUAS chamadas:

    1) pelo nome limpo + municipio + UF;
    2) so se a 1a nao achar nada, pelo endereco completo.

    A 2a tentativa acha bastante MEI que nao tem o nome na fachada, mas tem
    um risco real: um endereco pode devolver o vizinho, o predio ou a sala
    ao lado. Por isso marco de onde veio ('_origem') e a planilha mostra
    isso na coluna de origem do telefone - confira antes de ligar.

    Devolve {} quando as duas tentativas falham."""
    if not google_api_key:
        return {}

    end = empresa.get("endereco", {}) or {}
    municipio, uf = end.get("municipio", ""), end.get("uf", "")

    nome = nome_para_busca(empresa)
    if nome:
        consulta = " ".join(x for x in [nome, municipio, uf] if x)
        lugar = _consultar_places(consulta, google_api_key, contador)
        if lugar:
            lugar["_origem"] = "nome"
            return lugar

    endereco = _texto_endereco(empresa)
    if endereco:
        lugar = _consultar_places(endereco, google_api_key, contador)
        if lugar:
            lugar["_origem"] = "endereco"
            return lugar

    return {}


def formatar_cnpj(cnpj):
    """00.000.000/0000-00 - sem isso o Excel trata como numero e come o
    zero a esquerda."""
    d = "".join(c for c in str(cnpj or "") if c.isdigit())
    if len(d) != 14:
        return str(cnpj or "")
    return f"{d[0:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:14]}"


def montar_linha_cnae(empresa, dados_google=None, cnaes_buscados=None):
    """Converte uma empresa da Casa dos Dados (+ o que o Google achou) numa
    linha da planilha.

    TELEFONE: vem primeiro da ficha do Google, que e mantida pelo dono e
    tende a estar atual. Se o Google nao tiver, cai para o telefone
    declarado no cadastro da Receita - que preenche mais linhas, mas pode
    estar velho. A coluna "Origem do telefone" diz qual e qual.

    CNAE BUSCADO E: a Casa dos Dados devolve a empresa tanto quando o CNAE
    procurado e a atividade principal quanto quando e uma das secundarias.
    Por isso a coluna "CNAE principal" as vezes mostra um codigo diferente
    do que voce digitou. Aqui eu comparo os dois: se o CNAE principal esta
    entre os buscados, foi por atividade principal; senao, so pode ter
    vindo pelas secundarias."""
    dados_google = dados_google or {}
    buscados = [_so_digitos(c) for c in (cnaes_buscados or []) if _so_digitos(c)]
    end = empresa.get("endereco", {}) or {}

    telefone = dados_google.get("nationalPhoneNumber", "")
    if telefone:
        origem = (
            "Google (achado pelo endereço)"
            if dados_google.get("_origem") == "endereco"
            else "Google"
        )
    else:
        telefone = telefone_da_receita(empresa)
        origem = "Receita Federal (pode estar desatualizado)" if telefone else ""

    cnae_principal = _atividade_principal(empresa)[0]
    if not buscados:
        onde_entra = ""
    elif _so_digitos(cnae_principal) in buscados:
        onde_entra = "Principal"
    else:
        onde_entra = "Secundária"

    nome_fantasia = (empresa.get("nome_fantasia") or "").strip()
    razao_social = (empresa.get("razao_social") or "").strip()

    return {
        "Nome do estabelecimento": nome_fantasia or razao_social,
        "Socios / responsavel": _nomes_socios(empresa),
        "Telefone": telefone,
        "Origem do telefone": origem,
        "Google Meu Negocio": dados_google.get("googleMapsUri", ""),
        "Site": dados_google.get("websiteUri", ""),
        "CNPJ": formatar_cnpj(empresa.get("cnpj", "")),
        "CNAE principal": cnae_principal,
        "CNAE buscado é": onde_entra,
        "Endereco": _texto_endereco(empresa),
        "Bairro": end.get("bairro", ""),
        "Municipio": end.get("municipio", ""),
        "UF": end.get("uf", ""),
    }
