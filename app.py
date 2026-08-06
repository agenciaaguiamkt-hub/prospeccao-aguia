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

import pandas as pd
import streamlit as st

import math

from prospeccao_core import (
    buscar_multiplas_queries,
    buscar_cidade_completa,
    resolver_instagram,
    montar_linha,
)

st.set_page_config(page_title="Prospecção de Clínicas", page_icon="🔎")

# ----------------------- CONFIG / SECRETS -----------------------
API_KEY = st.secrets.get("GOOGLE_PLACES_API_KEY", "")
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
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

modo = st.radio(
    "Modo de busca",
    ["Cobertura total da cidade (recomendado)", "Buscas manuais (uma por linha)"],
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
    "rapido": {
        "emoji": "🟢",
        "nome": "Teste rápido",
        "desc": "Área pequena ao redor do centro da cidade. Ideal para testar antes de gastar mais.",
        "largura": 10, "altura": 10, "espacamento": 5,
        "cor_fundo": "#E8F5E9", "cor_borda": "#66BB6A",
    },
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


with st.form("busca"):
    if modo == "Cobertura total da cidade (recomendado)":
        categoria = st.text_input("Categoria de negócio", value="clínica de estética")
        cidade = st.text_input("Cidade", value="Votuporanga SP")

        preset_key = st.radio(
            "Área de cobertura",
            list(COBERTURA_PRESETS.keys()),
            format_func=lambda k: f"{COBERTURA_PRESETS[k]['emoji']} {COBERTURA_PRESETS[k]['nome']}",
            index=1,
            horizontal=True,
        )
        p = COBERTURA_PRESETS[preset_key]
        largura_km, altura_km, espacamento_km = p["largura"], p["altura"], p["espacamento"]
        estimativa = _estimar_chamadas(largura_km, altura_km, espacamento_km)

        st.markdown(
            f"""<div style="background:{p['cor_fundo']};border:1px solid {p['cor_borda']};
            border-radius:8px;padding:12px 16px;margin-bottom:6px;color:#1a1a1a;">
            <strong>{p['desc']}</strong><br>
            <span style="font-size:0.85em;">≈ {estimativa} chamadas à Places API nesta busca
            (pode ser mais se algum ponto tiver mais de 20 resultados).</span>
            </div>""",
            unsafe_allow_html=True,
        )

        with st.expander("Personalizar manualmente (avançado)"):
            personalizar = st.checkbox("Ajustar a área em km manualmente")
            if personalizar:
                largura_km = st.slider("Largura da área (km)", 5, 60, p["largura"], step=5)
                altura_km = st.slider("Altura da área (km)", 5, 60, p["altura"], step=5)
                espacamento_km = st.slider("Espaçamento entre pontos (km)", 1, 10, p["espacamento"], step=1)
                st.caption(
                    f"≈ {_estimar_chamadas(largura_km, altura_km, espacamento_km)} chamadas "
                    "à Places API com esses valores. Espaçamento menor = mais completo, "
                    "porém mais chamadas."
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

    buscar_ig = st.checkbox("Também procurar Instagram nos sites (mais lento)", value=True)
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

    if modo == "Cobertura total da cidade (recomendado)":
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

    st.success(f"{len(lugares)} lugares encontrados (após remover duplicados).")

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
        f"Acessos a sites (não é API do Google): **{contador_sites['chamadas']}** · "
        f"Buscas usadas nesta sessão: **{st.session_state.buscas_feitas}/{LIMITE_BUSCAS_POR_SESSAN}**"
    )
