"""
Interface visual da prospecção (Streamlit), feita para rodar na nuvem
(Streamlit Community Cloud) - veja DEPLOY_NUVEM.md para o passo a passo.

SEGURANÇA:
- A chave da Google Places API NUNCA fica no código nem aparece na tela.
  Ela é lida de st.secrets["GOOGLE_PLACES_API_KEY"], que fica guardada
  de forma criptografada pelo Streamlit Cloud e só existe no servidor -
  o navegador do usuário nunca recebe esse valor.
- A app NAO tem mais senha (removida a pedido): qualquer pessoa com o
  link entra e gasta a cota do Google e da Casa dos Dados. A protecao
  contra custo passou a ser o teto de chamadas ao Google no codigo
  (TETO_GOOGLE_MES). Nao da para travar a cota do lado do Google nessa
  API: no console, todas as cotas da Places API (New) sao marcadas como
  nao ajustaveis e a opcao "Editar cota" fica desabilitada.
- Há um limite de buscas por sessão de navegador, para conter abuso básico.

LIMITAÇÃO HONESTA: isso é proteção de nível "ferramenta interna pequena",
não segurança de nível enterprise. O rate limit é por sessão em memória -
não sobrevive a reinícios do app e não é robusto contra um atacante
dedicado. Para algo mais forte, seria necessário um backend dedicado
(ex.: FastAPI) com autenticação de verdade, rate limit por IP no servidor
e um API gateway na frente. Não implementei isso agora porque dobraria a
complexidade de deploy (duas infraestruturas em vez de uma) sem ganho
relevante para o volume de uso de uma ferramenta de prospecção de uma
agência pequena. Se o uso crescer bastante, vale reconsiderar.

COBERTURA TOTAL DA CIDADE: a Places API tem um teto prático de
resultados por chamada (perto de ~60, comportamento observado do
Google - não uma trava deste código, e não documentado por mim com
100% de certeza). O modo "Cobertura total da cidade" contorna isso
automaticamente, dividindo a área em uma grade de pontos geográficos e
buscando em cada um (ver buscar_cidade_completa em prospeccao_core.py).
Isso significa várias chamadas à API por busca - mais completo, porém
mais caro do que uma busca única.
"""

import io
import time
from datetime import date

import pandas as pd
import streamlit as st
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

import math

from prospeccao_core import (
    buscar_multiplas_queries,
    buscar_cidade_completa,
    resolver_instagram,
    montar_linha,
)

from cnae_core import (
    normalizar_cnaes,
    variantes_municipio,
    buscar_empresas_por_cnae,
    enriquecer_com_google,
    nome_para_busca,
    montar_linha_cnae,
    consultar_saldo,
    proxima_renovacao,
)

st.set_page_config(page_title="Prospecção de empresas", page_icon="🔎")

# ----------------------- CONFIG / SECRETS -----------------------
API_KEY = st.secrets.get("GOOGLE_PLACES_API_KEY", "")
CASA_DOS_DADOS_API_KEY = st.secrets.get("CASA_DOS_DADOS_API_KEY", "")
LIMITE_BUSCAS_POR_SESSAO = 20  # trava simples contra abuso, não é robusta

if not API_KEY:
    st.error(
        "Faltou configurar o segredo GOOGLE_PLACES_API_KEY nesta app. "
        "Veja DEPLOY_NUVEM.md para saber como adicionar em "
        "Settings → Secrets no Streamlit Cloud."
    )
    st.stop()

# O gate de senha foi removido a pedido: a app agora abre direto.
# Consequencia: qualquer pessoa com o link usa a app e gasta a sua cota
# do Google e da Casa dos Dados. A defesa passou a ser o TETO_GOOGLE
# abaixo (e, principalmente, a trava de cota no Google Cloud).

# ----------------------- TRAVA DE GASTO -----------------------
# A franquia gratuita do Google e de 1.000 chamadas/mes no SKU Enterprise
# (que e onde as nossas caem, por pedirem telefone e site). Passou disso,
# vira dinheiro. Este teto para no 900, 100 abaixo da franquia.
#
# LIMITACAO HONESTA: nao existe trava do lado do Google. Conferi no
# console em 05/09/2026: em Plataforma Google Maps > Cotas > Places API
# (New), TODAS as 21 cotas aparecem como "Ajustavel: Nao" e a opcao
# "Editar cota" esta desabilitada. A trava tem que ser aqui no codigo.
# Este contador e compartilhado por todas as abas e todos os usuarios da
# mesma instancia do app, e zera sozinho na virada do mes. O que ele NAO
# resiste: um reinicio do Streamlit Cloud zera a contagem, e ai o mes
# inteiro pode passar de 900 outra vez. A margem de 100 cobre chamada
# feita fora do app com a mesma chave e imprecisao no que o Google conta
# como cobravel - ela NAO protege contra reinicio.
TETO_GOOGLE_MES = 900


# st.cache_resource devolve sempre o mesmo objeto para todas as sessoes,
# entao abrir uma aba nova nao zera mais a contagem.
@st.cache_resource
def _cota_google():
    return {"mes": "", "chamadas": 0}


# Devolve o contador ja com o reset da virada de mes aplicado.
def cota_google_atual():
    cota = _cota_google()
    mes = date.today().strftime("%Y-%m")
    if cota["mes"] != mes:
        cota["mes"] = mes
        cota["chamadas"] = 0
    return cota

if "buscas_feitas" not in st.session_state:
    st.session_state.buscas_feitas = 0
# o contador do Google agora e global, ver cota_google_atual acima

# ----------------------- UI -----------------------
st.title("🔎 Prospecção de empresas")
st.caption(
    "Monta listas de empresas prontas para prospecção: nome, telefone, site "
    "e link do Google Meu Negócio. Você pode buscar por CNAE (cadastro da "
    "Receita Federal), por categoria de negócio ou por busca manual."
)

# ----------------------- OS DOIS SALDOS -----------------------
# Sao duas contas diferentes, de empresas diferentes, e elas se esgotam de
# jeitos diferentes. Por isso dois cards, lado a lado:
#
# 1) Casa dos Dados - paga os dados da Receita (CNPJ, socios, endereco).
#    O saldo vem da API deles. NAO da para deduzir o tamanho do plano a
#    partir do saldo, porque credito nao usado acumula enquanto a
#    assinatura estiver ativa - por isso a barra usa como referencia o
#    maior saldo visto no ciclo, e nao um numero fixo chutado.
# 2) Google Places - paga telefone, site e link do Google Meu Negocio.
#    Aqui o denominador e conhecido de verdade: o TETO_GOOGLE_MES que nos
#    mesmos definimos. E o que sobra NAO acumula, some na virada do mes.
DIA_RENOVACAO = 18

# ----------------------- SEMAFORO -----------------------
# Mesma linguagem visual nos dois cards: verde/amarelo/vermelho, com
# emoji e rotulo escritos, para nao depender so da cor (quem enxerga mal
# cor, ou olha a tela no sol, le a palavra).
# Ordem: (emoji, rotulo, cor do texto, fundo, borda).
SEMAFORO = (
    ("🟢", "Tranquilo", "#1B5E20", "#E8F5E9", "#66BB6A"),
    ("🟡", "Atenção", "#E65100", "#FFF8E1", "#FFB74D"),
    ("🔴", "Crítico", "#B71C1C", "#FFEBEE", "#EF5350"),
)


def _nivel(valor, limite_atencao, limite_critico):
    """0 = tranquilo, 1 = atencao, 2 = critico. Funciona tanto para numero
    absoluto (Casa dos Dados) quanto para fracao 0-1 (Google)."""
    if valor > limite_atencao:
        return 0
    if valor > limite_critico:
        return 1
    return 2


@st.cache_data(ttl=60, show_spinner=False)
def _saldo_em_cache(chave):
    """Cache de 1 minuto para nao consultar o saldo a cada clique na tela.
    A busca limpa esse cache no fim, para o numero cair na hora."""
    return consultar_saldo(chave)


# Maior saldo visto no ciclo atual, compartilhado por todas as sessoes.
@st.cache_resource
def _referencia_saldo():
    return {"ciclo": "", "maior": 0}


def _milhar(n):
    """1234 -> '1.234', do jeito brasileiro."""
    return f"{int(n):,}".replace(",", ".")


def _primeiro_dia_proximo_mes(hoje=None):
    """Quando a franquia do Google zera: sempre dia 1o."""
    hoje = hoje or date.today()
    if hoje.month == 12:
        return date(hoje.year + 1, 1, 1)
    return date(hoje.year, hoje.month + 1, 1)


def _card_saldo(titulo, numero, unidade, fracao, rodape, nivel):
    emoji, rotulo, cor, fundo, borda = SEMAFORO[nivel]
    st.markdown(
        f"""<div style="background:{fundo};border:1px solid {borda};
        border-radius:8px;padding:12px 16px;color:#1a1a1a;">
        <div style="display:flex;justify-content:space-between;
        align-items:flex-start;gap:10px;">
        <span style="font-size:0.72em;text-transform:uppercase;
        letter-spacing:0.05em;opacity:0.65;">{titulo}</span>
        <span style="font-size:0.74em;white-space:nowrap;color:{cor};
        font-weight:700;">{emoji} {rotulo}</span></div>
        <div style="font-size:1.3em;line-height:1.3;margin-top:2px;">
        <strong style="color:{cor};">{numero}</strong></div>
        <div style="font-size:0.8em;opacity:0.8;">{unidade}</div>
        <div style="background:#00000018;border-radius:99px;height:8px;margin:8px 0 6px 0;">
        <div style="background:{borda};width:{fracao * 100:.1f}%;height:8px;
        border-radius:99px;"></div></div>
        <div style="font-size:0.76em;opacity:0.8;">{rodape}</div>
        </div>""",
        unsafe_allow_html=True,
    )


def _card_casa_dos_dados():
    """Desenha o card e devolve (nivel, restantes) - ou None se nao deu."""
    if not CASA_DOS_DADOS_API_KEY:
        return None
    try:
        restantes, _detalhes = _saldo_em_cache(CASA_DOS_DADOS_API_KEY)
    except Exception as e:
        st.caption(f"Não consegui ler o saldo da Casa dos Dados agora ({e}).")
        return None

    renova = proxima_renovacao(dia=DIA_RENOVACAO)
    ref = _referencia_saldo()
    ciclo = renova.strftime("%Y-%m")
    if ref["ciclo"] != ciclo:
        ref["ciclo"], ref["maior"] = ciclo, restantes
    ref["maior"] = max(ref["maior"], restantes)
    fracao = min(1.0, max(0.0, restantes / ref["maior"])) if ref["maior"] else 0.0

    # Semaforo pelo numero ABSOLUTO, nao pela fracao: a referencia da barra
    # se redefine quando o app reinicia, entao a fracao voltaria a 100% e
    # ficaria verde mesmo com 50 consultas na conta.
    nivel = _nivel(restantes, 1000, 300)
    _card_saldo(
        "1 · Dados da Receita (Casa dos Dados)",
        _milhar(restantes),
        "consultas restantes",
        fracao,
        f"Renova em {renova.strftime('%d/%m')} · o que sobra acumula",
        nivel,
    )
    return nivel, restantes


def _card_google():
    """Desenha o card e devolve (nivel, restantes, data em que zera)."""
    cota = cota_google_atual()
    restantes = max(0, TETO_GOOGLE_MES - cota["chamadas"])
    fracao = restantes / TETO_GOOGLE_MES if TETO_GOOGLE_MES else 0.0

    # Aqui o denominador e real (o teto que nos definimos), entao o
    # semaforo pode seguir a fracao mesmo.
    nivel = _nivel(fracao, 0.50, 0.20)
    zera = _primeiro_dia_proximo_mes()
    _card_saldo(
        "2 · Telefone e Google Meu Negócio (Google)",
        f"{_milhar(restantes)} de {_milhar(TETO_GOOGLE_MES)}",
        "chamadas restantes",
        fracao,
        f"Zera em {zera.strftime('%d/%m')} · o que sobra NÃO acumula",
        nivel,
    )
    return nivel, restantes, zera


col_receita, col_google = st.columns(2)
with col_receita:
    _est_cdd = _card_casa_dos_dados()
with col_google:
    _est_google = _card_google()

# No vermelho, o card sozinho nao basta: diz o que fazer.
if _est_cdd and _est_cdd[0] == 2:
    st.error(
        f"Casa dos Dados quase no fim: **{_milhar(_est_cdd[1])} consultas**. "
        "Recarregue no portal deles antes da próxima busca grande, senão a "
        "busca para no meio.",
        icon="🔴",
    )
if _est_google[0] == 2:
    st.error(
        f"Restam **{_milhar(_est_google[1])} chamadas** ao Google até "
        f"{_est_google[2].strftime('%d/%m')}. Quando zerar, a planilha continua "
        "saindo — mas sem telefone e link do Google. O telefone da Receita "
        "ainda entra, marcado na coluna de origem.",
        icon="🔴",
    )

st.caption(
    f"Cada empresa da lista consome **1 consulta** da Casa dos Dados e **1 a 2 "
    f"chamadas** do Google (a 2ª só quando a 1ª não acha). O teto de "
    f"{_milhar(TETO_GOOGLE_MES)} é nosso, proposital, abaixo da franquia "
    "gratuita de 1.000/mês do Google — é ele que impede virar cobrança."
)

modo = st.radio(
    "Modo de busca",
    [
        "Busca por Categoria de negócio",
        "Busca por CNAE",
        "Busca manual",
    ],
    index=1,  # CNAE e o modo padrao
    help=(
        "A Places API tem um teto prático de resultados por chamada (perto de "
        "~60 - comportamento observado do Google, não uma trava deste app). "
        "'Cobertura total' automatiza a divisão da cidade em uma grade de "
        "pontos geográficos e busca em cada um, juntando e removendo "
        "duplicados - sem você precisar digitar buscas manuais. Isso faz "
        "várias chamadas à API (mais custo) para conseguir mais completude."
    ),
)

# Presets de cobertura: escondem os detalhes técnicos (km/espaçamento) por
# trás de 3 opções em linguagem simples. Cada uma já vem calibrada com uma
# combinação de área + espaçamento que funciona bem para aquele perfil.
COBERTURA_PRESETS = {
    "padrao": {
        "emoji": "🔵",
        "nome": "Padrão (recomendado)",
        "desc": "Bom equilíbrio entre completude e custo para a maioria das cidades médias.",
        "largura": 20, "altura": 20, "espacamento": 4,
        "cor_fundo": "#E3F2FD", "cor_borda": "#42A5F5",
    },
    "maximo": {
        "emoji": "🟣",
        "nome": "Cobertura máxima",
        "desc": "Área maior e grade mais fina. Mais completo, porém mais chamadas à API (mais custo).",
        "largura": 35, "altura": 35, "espacamento": 3,
        "cor_fundo": "#F3E5F5", "cor_borda": "#AB47BC",
    },
}


def _estimar_chamadas(largura_km, altura_km, espacamento_km):
    n_pontos = (math.ceil(largura_km / espacamento_km) + 1) * (
        math.ceil(altura_km / espacamento_km) + 1
    )
    return n_pontos + 1  # +1 da chamada para localizar a cidade


# ----------------------- TABELA DE CUSTOS -----------------------
# Valores conferidos em 07/08/2026. Os três podem mudar sem aviso - se a
# conta não bater com a fatura, é aqui que se ajusta.
#
# CUSTO_CNPJ_BRL: plano Básico 1 da Casa dos Dados (R$ 29,90/mês por 5.000
#   consultas = R$ 0,006 por CNPJ). Se você trocar de plano, troque aqui.
# CUSTO_PLACES_USD: a Places API cobra por SKU conforme os campos pedidos.
#   Como pedimos telefone, site e nota (campos do tier Enterprise), cai na
#   faixa Text Search Enterprise, US$ 35 por 1.000 chamadas. Não tenho como
#   confirmar esse número contra a sua fatura real daqui - confira na
#   documentação de billing da Places API.
#   O Google ainda dá uma franquia mensal gratuita (na casa de 1.000
#   chamadas Enterprise/mês, valor que não consegui confirmar com 100% de
#   certeza), então na prática as primeiras buscas do mês tendem a sair de
#   graça. Por isso o número mostrado é um TETO, não uma cobrança certa.
# DOLAR_BRL: cotação aproximada. Serve para dar ordem de grandeza, não para
#   fechar contabilidade.
CUSTO_CNPJ_BRL = 0.006
CUSTO_PLACES_USD = 0.035
DOLAR_BRL = 5.12
CUSTO_PLACES_BRL = CUSTO_PLACES_USD * DOLAR_BRL


def _reais(valor):
    """Formata no padrão brasileiro: 1234.5 -> '1.234,50'."""
    return f"{valor:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")


# Texto que aparece na celula no lugar da URL crua (o link fica embutido).
ROTULO_LINK = "Clique aqui"


def montar_excel(df):
    """Monta um .xlsx de verdade (nao CSV): cada campo na sua coluna,
    largura ajustada, cabecalho congelado e filtro ligado.

    Sao duas abas, nessa ordem: 'Com telefone' primeiro, porque e a lista
    que se usa para ligar, e 'Sem telefone' depois, com o resto. A divisao
    e feita pela coluna Telefone estar vazia ou nao."""
    if "Telefone" in df.columns:
        tem = df["Telefone"].astype(str).str.strip() != ""
        com_tel, sem_tel = df[tem], df[~tem]
    else:
        com_tel, sem_tel = df, df.iloc[0:0]

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for nome_aba, dados in (("Com telefone", com_tel), ("Sem telefone", sem_tel)):
            dados.to_excel(writer, index=False, sheet_name=nome_aba)
            aba = writer.sheets[nome_aba]
            aba.freeze_panes = "A2"
            if len(dados.columns) and len(dados):
                aba.auto_filter.ref = aba.dimensions
            # Troca a URL crua por um "Clique aqui" ja clicavel. Sem isso o
            # Excel entrega a URL como texto comum: so vira link depois que
            # voce edita a celula e aperta Enter, uma por uma.
            for linha in aba.iter_rows(min_row=2):
                for celula in linha:
                    valor = celula.value
                    if isinstance(valor, str) and valor.startswith("http"):
                        celula.hyperlink = valor
                        celula.value = ROTULO_LINK
                        celula.font = Font(color="0563C1", underline="single")

            # Largura medida DEPOIS da troca - senao a coluna fica larga a toa
            # por causa do tamanho da URL que nem aparece mais.
            for i in range(1, aba.max_column + 1):
                tamanhos = [
                    len(str(aba.cell(row=r, column=i).value or ""))
                    for r in range(1, min(aba.max_row, 300) + 1)
                ]
                aba.column_dimensions[get_column_letter(i)].width = min(
                    55, max(12, max(tamanhos) + 2)
                )
    buffer.seek(0)
    return buffer, len(com_tel), len(sem_tel)


if modo == "Busca por CNAE":
    # Fora de st.form de propósito: dentro de um formulário o Streamlit só
    # reexecuta o script quando você aperta o botão, e aí a previsão de
    # custo ficaria congelada no valor antigo enquanto você mexe nos campos.
    categoria = cidade = ""
    largura_km = altura_km = espacamento_km = None
    queries_texto = ""

    cnae_texto = st.text_input(
        "Código CNAE",
        value="",
        placeholder="Ex.: 4520001 - separe vários por vírgula",
        help=(
            "O código oficial da atividade econômica, como está no cadastro da "
            "Receita Federal. Pode digitar com ou sem pontuação (4520-0/01 ou "
            "4520001) e pode colocar vários separados por vírgula. Se não souber "
            "o código, procure em concla.ibge.gov.br."
        ),
    )
    col_uf, col_mun = st.columns([1, 3])
    with col_uf:
        uf_cnae = st.text_input("UF", value="", placeholder="Ex.: SP")
    with col_mun:
        municipio_cnae = st.text_input(
            "Município", value="", placeholder="Ex.: Votuporanga"
        )

    limite_cnae = st.number_input(
        "Máximo de empresas a trazer",
        min_value=10,
        max_value=2000,
        value=100,
        step=10,
        help="Comece baixo para conferir se o CNAE está certo antes de gastar.",
    )

    # A busca de telefone/site/Google Meu Negocio deixou de ser opcional:
    # sem ela a planilha vem so com o cadastro da Receita, que nao tem
    # telefone atualizado. O checkbox e o bloco de custo foram removidos da
    # tela a pedido - a protecao contra gasto e o TETO_GOOGLE_MES.
    enriquecer_google = True

    buscar_ig = True
    enviar = st.button("Buscar", type="primary")
else:
    cnae_texto = uf_cnae = municipio_cnae = ""
    limite_cnae = 100
    enriquecer_google = True

    with st.form("busca"):
        if modo == "Busca por Categoria de negócio":
            categoria = st.text_input(
                "Categoria de negócio",
                value="",
                placeholder="Ex.: clínica de estética",
            )
            cidade = st.text_input(
                "Cidade", value="", placeholder="Ex.: Votuporanga SP"
            )

            preset_key = st.radio(
                "Área de cobertura",
                list(COBERTURA_PRESETS.keys()),
                format_func=lambda k: f"{COBERTURA_PRESETS[k]['emoji']} {COBERTURA_PRESETS[k]['nome']}",
                index=0,
                horizontal=True,
            )
            p = COBERTURA_PRESETS[preset_key]
            largura_km, altura_km, espacamento_km = p["largura"], p["altura"], p["espacamento"]
            estimativa = _estimar_chamadas(largura_km, altura_km, espacamento_km)

            st.markdown(
                f"""<div style="background:{p['cor_fundo']};border:1px solid {p['cor_borda']};
                border-radius:8px;padding:12px 16px;margin-bottom:6px;color:#1a1a1a;">
                <strong>{p['desc']}</strong><br>
                <span style="font-size:0.85em;">Cerca de {estimativa} chamadas à Places API nesta
                busca (pode ser mais se algum ponto tiver mais de 20 resultados).</span>
                </div>""",
                unsafe_allow_html=True,
            )

            queries_texto = ""
        else:
            categoria = cidade = ""
            largura_km = altura_km = espacamento_km = None
            queries_texto = st.text_area(
                "O que buscar (uma busca por linha, igual você digitaria no Google Maps)",
                value="",
                placeholder="Ex.: clínica de estética em Votuporanga SP",
                height=100,
            )

        buscar_ig = True  # buscar Instagram nos sites é padrão, sem opção de desligar
        enviar = st.form_submit_button("Buscar")

if enviar:
    if st.session_state.buscas_feitas >= LIMITE_BUSCAS_POR_SESSAO:
        st.error(
            f"Limite de {LIMITE_BUSCAS_POR_SESSAO} buscas nesta sessão atingido. "
            "Recarregue a página para começar uma nova sessão."
        )
        st.stop()

    st.session_state.buscas_feitas += 1

    contador_api = {"chamadas": 0}
    contador_sites = {"chamadas": 0}
    contador_cdd = {"chamadas": 0}
    estatisticas_cobertura = {}
    linhas_prontas = None  # o modo CNAE monta as linhas por conta própria

    if modo == "Busca por CNAE":
        cnaes = normalizar_cnaes(cnae_texto)
        if not cnaes:
            st.error("Informe ao menos um código CNAE (só números, ex.: 4520001).")
            st.stop()
        if not uf_cnae.strip() or not municipio_cnae.strip():
            st.error("Preencha a UF (ex.: SP) e o município (ex.: Votuporanga).")
            st.stop()

        with st.spinner("Consultando o cadastro de CNPJ da Receita Federal..."):
            try:
                empresas = buscar_empresas_por_cnae(
                    CASA_DOS_DADOS_API_KEY,
                    cnaes,
                    uf_cnae,
                    municipio_cnae,
                    limite_total=int(limite_cnae),
                    contador=contador_cdd,
                )
            except RuntimeError as e:
                st.error(str(e))
                st.stop()

        if not empresas:
            st.warning(
                f"Nenhuma empresa encontrada. Foi pesquisado o CNAE "
                f"**{', '.join(cnaes)}** no município "
                f"**{' ou '.join(variantes_municipio(municipio_cnae))}** "
                f"(UF **{uf_cnae.strip().upper()}**). Confira o código em "
                "concla.ibge.gov.br e a grafia do município - acima está "
                "exatamente o que foi enviado para a Casa dos Dados."
            )
            st.stop()

        st.success(
            f"{len(empresas)} empresas encontradas com CNAE {', '.join(cnaes)} "
            f"em {municipio_cnae}-{uf_cnae.upper()}."
        )

        with st.expander("Ver os dados crus da 1ª empresa (diagnóstico)"):
            st.caption(
                "Mostra exatamente o que a Casa dos Dados devolveu. Serve para "
                "conferir se algum campo que você precisa existe mas não está "
                "sendo aproveitado na planilha."
            )
            st.json(empresas[0])

        linhas_prontas = []
        travou_google = False  # para avisar do teto uma vez so
        cota_google = cota_google_atual()
        progresso_cnae = st.progress(0.0)
        status_cnae = st.empty()
        total_emp = len(empresas) or 1

        for i, empresa in enumerate(empresas):
            nome = nome_para_busca(empresa)
            dados_google = {}
            if enriquecer_google and cota_google["chamadas"] >= TETO_GOOGLE_MES:
                if not travou_google:
                    st.warning(
                        f"Teto de {TETO_GOOGLE_MES} chamadas ao Google "
                        "atingido neste mês. O restante da lista vem só com "
                        "os dados da Receita, sem telefone nem link do Google. "
                        "Isso existe para não passar da franquia gratuita.",
                        icon="🛑",
                    )
                    travou_google = True
            elif enriquecer_google:
                status_cnae.write(f"Procurando no Google: {nome or '(sem nome)'}")
                # A cota tem que contar CHAMADAS, nao empresas: a busca pode
                # gastar 2 (nome e, se falhar, endereco). Por isso meco o
                # antes/depois em vez de somar 1 fixo.
                antes = contador_api["chamadas"]
                dados_google = enriquecer_com_google(
                    empresa, API_KEY, contador=contador_api
                )
                cota_google["chamadas"] += contador_api["chamadas"] - antes
            linhas_prontas.append(montar_linha_cnae(empresa, dados_google, cnaes))
            progresso_cnae.progress((i + 1) / total_emp)

        status_cnae.empty()
        progresso_cnae.empty()

        # A busca acabou de gastar consultas: joga fora o saldo em cache
        # para o contador do topo mostrar o numero novo no proximo rerun.
        _saldo_em_cache.clear()

    elif modo == "Busca por Categoria de negócio":
        if not categoria or not cidade:
            st.error("Preencha categoria e cidade.")
            st.stop()

        progresso_grade = st.progress(0.0)
        status_grade = st.empty()

        def _callback_progresso(feito, total_pontos):
            status_grade.write(f"Varrendo grade geográfica: ponto {feito}/{total_pontos}")
            progresso_grade.progress(feito / total_pontos)

        with st.spinner("Localizando a cidade e varrendo a grade..."):
            try:
                lugares = buscar_cidade_completa(
                    categoria,
                    cidade,
                    API_KEY,
                    largura_km=largura_km,
                    altura_km=altura_km,
                    espacamento_km=espacamento_km,
                    contador=contador_api,
                    progresso_callback=_callback_progresso,
                    estatisticas=estatisticas_cobertura,
                )
            except RuntimeError as e:
                st.error(str(e))
                st.stop()

        status_grade.empty()
        progresso_grade.empty()
    else:
        queries = [q for q in queries_texto.splitlines() if q.strip()]
        if not queries:
            st.error("Preencha ao menos uma busca.")
            st.stop()

        rotulo = "Buscando na Places API..." if len(queries) == 1 else f"Buscando na Places API ({len(queries)} buscas)..."
        with st.spinner(rotulo):
            try:
                lugares = buscar_multiplas_queries(queries, API_KEY, contador=contador_api)
            except RuntimeError as e:
                st.error(str(e))
                st.stop()

    if linhas_prontas is not None:
        linhas = linhas_prontas
    elif estatisticas_cobertura.get("removidos"):
        st.success(
            f"{len(lugares)} lugares encontrados em {cidade} "
            f"(de {estatisticas_cobertura['total_bruto']} resultados brutos da região, "
            f"{estatisticas_cobertura['removidos']} foram descartados por serem de outras "
            "cidades - o locationBias da Places API é só uma dica de área, não um filtro "
            "rígido)."
        )
    else:
        st.success(f"{len(lugares)} lugares encontrados (após remover duplicados).")

    if linhas_prontas is None:
        linhas = []
        progresso = st.progress(0.0)
        status = st.empty()
        total = len(lugares) or 1

        for i, lugar in enumerate(lugares):
            linha = montar_linha(lugar)
            status.write(f"Verificando: {linha['Nome'] or '(sem nome)'}")
            if buscar_ig and linha["Site"]:
                linha["Instagram"] = resolver_instagram(linha["Site"], contador=contador_sites)
            else:
                linha["Instagram"] = ""
            linhas.append(linha)
            progresso.progress((i + 1) / total)

        status.empty()
        progresso.empty()

    df = pd.DataFrame(linhas)
    st.dataframe(df, use_container_width=True)

    buffer, n_com_tel, n_sem_tel = montar_excel(df)

    nome_arquivo = f"prospeccao_{int(time.time())}.xlsx"
    st.download_button(
        "⬇️ Baixar planilha (.xlsx)",
        data=buffer,
        file_name=nome_arquivo,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    st.caption(
        f"A planilha vem em 2 abas: **Com telefone** ({n_com_tel}) e "
        f"**Sem telefone** ({n_sem_tel})."
    )

    st.info(
        f"Chamadas à Places API: **{contador_api['chamadas']}** · "
        f"Chamadas à Casa dos Dados: **{contador_cdd['chamadas']}** · "
        f"Acessos a sites (não é API do Google): **{contador_sites['chamadas']}** · "
        f"Buscas usadas nesta sessão: **{st.session_state.buscas_feitas}/{LIMITE_BUSCAS_POR_SESSAO}**"
    )
