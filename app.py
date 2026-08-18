"""
Interface visual da prospecção (Streamlit), feita para rodar na nuvem
(Streamlit Community Cloud) - veja DEPLOY_NUVEM.md para o passo a passo.

SEGURANÇA:
- A chave da Google Places API NUNCA fica no código nem aparece na tela.
  Ela é lida de st.secrets["GOOGLE_PLACES_API_KEY"], que fica guardada
  de forma criptografada pelo Streamlit Cloud e só existe no servidor -
  o navegador do usuário nunca recebe esse valor.
- A app fica protegida por uma senha simples (st.secrets["APP_PASSWORD"]),
  para impedir que qualquer pessoa na internet gaste sua cota da API.
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
    buscar_no_google,
    montar_linha_cnae,
    consultar_saldo,
    proxima_renovacao,
)

st.set_page_config(page_title="Prospecção de Clínicas", page_icon="🔎")

# ----------------------- CONFIG / SECRETS -----------------------
API_KEY = st.secrets.get("GOOGLE_PLACES_API_KEY", "")
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
CASA_DOS_DADOS_API_KEY = st.secrets.get("CASA_DOS_DADOS_API_KEY", "")
LIMITE_BUSCAS_POR_SESSAO = 20  # trava simples contra abuso, não é robusta

if not API_KEY:
    st.error(
        "Faltou configurar o segredo GOOGLE_PLACES_API_KEY nesta app. "
        "Veja DEPLOY_NUVEM.md para saber como adicionar em "
        "Settings → Secrets no Streamlit Cloud."
    )
    st.stop()

# ----------------------- GATE DE SENHA -----------------------
if APP_PASSWORD:
    if "autenticado" not in st.session_state:
        st.session_state.autenticado = False

    if not st.session_state.autenticado:
        st.title("🔒 Prospecção via Google Places")
        senha = st.text_input("Senha de acesso", type="password")
        if st.button("Entrar"):
            if senha == APP_PASSWORD:
                st.session_state.autenticado = True
                st.rerun()
            else:
                st.error("Senha incorreta.")
        st.stop()
else:
    st.warning(
        "Nenhuma senha configurada (APP_PASSWORD ausente nos Secrets) - "
        "esta app está acessível a qualquer pessoa com o link, e ela "
        "consome sua cota/faturamento da Google Places API. Configure uma "
        "senha assim que possível (veja DEPLOY_NUVEM.md).",
        icon="⚠️",
    )

# ----------------------- RATE LIMIT SIMPLES -----------------------
if "buscas_feitas" not in st.session_state:
    st.session_state.buscas_feitas = 0

# ----------------------- UI -----------------------
st.title("🔎 Prospecção via Google Places")
st.caption(
    "Busca lugares na Google Places API (New), classifica telefone "
    "celular/fixo, traz o link do Google Meu Negócio e procura o "
    "Instagram no site de cada um."
)

# ----------------------- SALDO DO PLANO -----------------------
# Plano contratado na Casa dos Dados: 5.000 consultas por mes, renovando
# todo dia 18. Esses dois numeros sao so para desenhar a barra e a data -
# o SALDO em si vem da API deles, nao de contagem nossa.
PLANO_CONSULTAS_MES = 5000
DIA_RENOVACAO = 18


@st.cache_data(ttl=60, show_spinner=False)
def _saldo_em_cache(chave):
    """Cache de 1 minuto para nao consultar o saldo a cada clique na tela.
    A busca limpa esse cache no fim, para o numero cair na hora."""
    return consultar_saldo(chave)


def mostrar_saldo():
    if not CASA_DOS_DADOS_API_KEY:
        return
    try:
        restantes, _detalhes = _saldo_em_cache(CASA_DOS_DADOS_API_KEY)
    except Exception as e:
        st.caption(f"Não consegui ler o saldo da Casa dos Dados agora ({e}).")
        return

    usadas = max(0, PLANO_CONSULTAS_MES - restantes)
    fracao = min(1.0, max(0.0, restantes / PLANO_CONSULTAS_MES))
    renova = proxima_renovacao(dia=DIA_RENOVACAO)
    dias = (renova - date.today()).days

    if fracao > 0.4:
        cor, fundo, borda = "#1B5E20", "#E8F5E9", "#66BB6A"
    elif fracao > 0.15:
        cor, fundo, borda = "#E65100", "#FFF8E1", "#FFB74D"
    else:
        cor, fundo, borda = "#B71C1C", "#FFEBEE", "#EF5350"

    # Milhar com ponto, do jeito brasileiro. Formato os numeros aqui em
    # vez de dar replace no HTML inteiro - senao uma virgula em algum
    # estilo CSS viraria ponto e quebraria o card.
    n_restantes = f"{restantes:,}".replace(",", ".")
    n_plano = f"{PLANO_CONSULTAS_MES:,}".replace(",", ".")
    n_usadas = f"{usadas:,}".replace(",", ".")
    plural = "s" if dias != 1 else ""

    st.markdown(
        f"""<div style="background:{fundo};border:1px solid {borda};
        border-radius:8px;padding:14px 18px;margin:4px 0 14px 0;color:#1a1a1a;">
        <div style="font-size:1.15em;">
        <strong style="color:{cor};">{n_restantes}</strong> consultas restantes
        <span style="opacity:0.7;">de {n_plano} no plano</span></div>
        <div style="background:#00000018;border-radius:99px;height:9px;margin:9px 0 7px 0;">
        <div style="background:{borda};width:{fracao * 100:.1f}%;height:9px;
        border-radius:99px;"></div></div>
        <div style="font-size:0.82em;opacity:0.8;">
        {n_usadas} usadas neste ciclo &nbsp;&middot;&nbsp; renova em
        {renova.strftime("%d/%m/%Y")} (em {dias} dia{plural})
        </div></div>""",
        unsafe_allow_html=True,
    )


mostrar_saldo()

modo = st.radio(
    "Modo de busca",
    [
        "Cobertura total da cidade (recomendado)",
        "Busca por CNAE (dados da Receita Federal)",
        "Buscas manuais (uma por linha)",
    ],
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


if modo == "Busca por CNAE (dados da Receita Federal)":
    # Fora de st.form de propósito: dentro de um formulário o Streamlit só
    # reexecuta o script quando você aperta o botão, e aí a previsão de
    # custo ficaria congelada no valor antigo enquanto você mexe nos campos.
    categoria = cidade = ""
    largura_km = altura_km = espacamento_km = None
    queries_texto = ""

    cnae_texto = st.text_input(
        "Código CNAE",
        value="4520001",
        help=(
            "O código oficial da atividade econômica, como está no cadastro da "
            "Receita Federal. Pode digitar com ou sem pontuação (4520-0/01 ou "
            "4520001) e pode colocar vários separados por vírgula. Se não souber "
            "o código, procure em concla.ibge.gov.br."
        ),
    )
    col_uf, col_mun = st.columns([1, 3])
    with col_uf:
        uf_cnae = st.text_input("UF", value="SP")
    with col_mun:
        municipio_cnae = st.text_input("Município", value="Votuporanga")

    limite_cnae = st.number_input(
        "Máximo de empresas a trazer",
        min_value=10,
        max_value=2000,
        value=100,
        step=10,
        help="Comece baixo para conferir se o CNAE está certo antes de gastar.",
    )

    enriquecer_google = st.checkbox(
        f"Buscar telefone, site e link do Google Meu Negócio "
        f"(+ R$ {_reais(float(limite_cnae) * CUSTO_PLACES_BRL)} nesta busca)",
        value=True,
        help=(
            "O cadastro da Receita não tem telefone atualizado, site nem nota. "
            "Marcando aqui, cada empresa é procurada no Google pelo nome, o que "
            "custa cerca de R$ "
            + _reais(CUSTO_PLACES_BRL)
            + " por empresa. Desmarque para fazer uma busca "
            "quase de graça e conferir a lista antes. Nem sempre acha: empresa sem "
            "perfil no Google, ou com nome de fachada diferente do nome registrado."
        ),
    )

    custo_receita = float(limite_cnae) * CUSTO_CNPJ_BRL
    custo_google = float(limite_cnae) * CUSTO_PLACES_BRL if enriquecer_google else 0.0
    custo_total = custo_receita + custo_google

    linha_google = (
        f"<br>Buscar cada uma no Google &nbsp;&middot;&nbsp; <strong>R$ {_reais(custo_google)}</strong>"
        if enriquecer_google
        else '<br><span style="opacity:0.7;">Busca no Google desligada &nbsp;&middot;&nbsp; R$ 0,00</span>'
    )

    st.markdown(
        f"""<div style="background:#FFF8E1;border:1px solid #FFB74D;
        border-radius:8px;padding:14px 18px;margin:8px 0 12px 0;color:#1a1a1a;">
        <div style="font-size:1.15em;margin-bottom:6px;">
        Esta busca custa no máximo <strong>R$ {_reais(custo_total)}</strong></div>
        <div style="font-size:0.88em;line-height:1.6;">
        Pegar {int(limite_cnae)} empresas na Receita Federal &nbsp;&middot;&nbsp;
        <strong>R$ {_reais(custo_receita)}</strong>{linha_google}
        </div>
        <div style="font-size:0.78em;opacity:0.75;margin-top:8px;">
        É um teto, não uma cobrança garantida: se a cidade tiver menos empresas
        que isso, você paga menos. Base de cálculo: R$ {f"{CUSTO_CNPJ_BRL:.3f}".replace(".", ",")} por
        CNPJ (plano Básico 1) e US$ {CUSTO_PLACES_USD:.3f} por consulta ao Google
        a US$ 1 = R$ {_reais(DOLAR_BRL)}. Valores de 07/08/2026 - confirme na sua
        fatura, principalmente a cotação.
        </div></div>""",
        unsafe_allow_html=True,
    )

    buscar_ig = True
    enviar = st.button("Buscar", type="primary")
else:
    cnae_texto = uf_cnae = municipio_cnae = ""
    limite_cnae = 100
    enriquecer_google = True

    with st.form("busca"):
        if modo == "Cobertura total da cidade (recomendado)":
            categoria = st.text_input("Categoria de negócio", value="clínica de estética")
            cidade = st.text_input("Cidade", value="Votuporanga SP")

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
                value="clínica de estética em Votuporanga SP",
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

    if modo == "Busca por CNAE (dados da Receita Federal)":
        cnaes = normalizar_cnaes(cnae_texto)
        if not cnaes:
            st.error("Informe ao menos um código CNAE (só números, ex.: 4520001).")
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
        progresso_cnae = st.progress(0.0)
        status_cnae = st.empty()
        total_emp = len(empresas) or 1

        for i, empresa in enumerate(empresas):
            nome = (empresa.get("nome_fantasia") or empresa.get("razao_social") or "").strip()
            dados_google = {}
            if enriquecer_google:
                status_cnae.write(f"Procurando no Google: {nome or '(sem nome)'}")
                dados_google = buscar_no_google(
                    nome,
                    (empresa.get("endereco", {}) or {}).get("municipio", ""),
                    (empresa.get("endereco", {}) or {}).get("uf", ""),
                    API_KEY,
                    contador=contador_api,
                )
            linhas_prontas.append(montar_linha_cnae(empresa, dados_google))
            progresso_cnae.progress((i + 1) / total_emp)

        status_cnae.empty()
        progresso_cnae.empty()

        # A busca acabou de gastar consultas: joga fora o saldo em cache
        # para o contador do topo mostrar o numero novo no proximo rerun.
        _saldo_em_cache.clear()

    elif modo == "Cobertura total da cidade (recomendado)":
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

    buffer = io.BytesIO()
    df.to_excel(buffer, index=False)
    buffer.seek(0)

    nome_arquivo = f"prospeccao_{int(time.time())}.xlsx"
    st.download_button(
        "⬇️ Baixar planilha (.xlsx)",
        data=buffer,
        file_name=nome_arquivo,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.info(
        f"Chamadas à Places API: **{contador_api['chamadas']}** · "
        f"Chamadas à Casa dos Dados: **{contador_cdd['chamadas']}** · "
        f"Acessos a sites (não é API do Google): **{contador_sites['chamadas']}** · "
        f"Buscas usadas nesta sessão: **{st.session_state.buscas_feitas}/{LIMITE_BUSCAS_POR_SESSAO}**"
    )
