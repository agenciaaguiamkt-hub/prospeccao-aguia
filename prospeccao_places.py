"""
Prospecção via Google Places API (Text Search - New) - versão terminal.

Para o dia a dia, use a versão na nuvem (veja DEPLOY_NUVEM.md) ou rode
`streamlit run app.py` localmente. Este arquivo é útil para testes rápidos
ou depuração via terminal.

A chave da API NÃO fica escrita neste arquivo (dado sensível). Ela é lida
da variável de ambiente GOOGLE_PLACES_API_KEY; se não existir, o script
pergunta no terminal com entrada oculta (não aparece na tela).
"""

import os
import sys
from getpass import getpass

import pandas as pd

from prospeccao_core import buscar_multiplas_queries, resolver_instagram, montar_linha

# Uma ou mais buscas. Cada busca tem um teto próprio de resultados (perto
# de ~60 na prática) - se acha que falta gente, adicione mais linhas mais
# específicas (por bairro, por sub-categoria etc.). Duplicados entre
# buscas são removidos automaticamente.
QUERIES = [
    "clínica de estética em Votuporanga SP",
]
ARQUIVO_SAIDA = "prospeccao_votuporanga.xlsx"


def obter_api_key():
    chave = os.environ.get("GOOGLE_PLACES_API_KEY")
    if chave:
        return chave
    if not sys.stdin.isatty():
        raise RuntimeError(
            "GOOGLE_PLACES_API_KEY não configurada e não há terminal interativo "
            "para pedir a chave."
        )
    return getpass("Cole sua chave da Google Places API (não aparece na tela): ").strip()


def main():
    api_key = obter_api_key()
    if not api_key:
        print("Nenhuma chave informada. Encerrando.")
        return

    contador_api = {"chamadas": 0}
    contador_sites = {"chamadas": 0}

    print(f"Buscando lugares na Places API ({len(QUERIES)} busca(s))...")
    lugares = buscar_multiplas_queries(QUERIES, api_key, contador=contador_api)
    print(f"{len(lugares)} lugares encontrados (após remover duplicados entre buscas).")

    linhas = []
    for lugar in lugares:
        linha = montar_linha(lugar)
        site = linha["Site"]
        print(f"Verificando site: {linha['Nome'] or '(sem nome)'}")
        linha["Instagram"] = resolver_instagram(site, contador=contador_sites) if site else ""
        linhas.append(linha)

    df = pd.DataFrame(linhas)
    df.to_excel(ARQUIVO_SAIDA, index=False)

    print("\n--- RESUMO ---")
    print(f"Planilha salva em: {ARQUIVO_SAIDA}")
    print(f"Chamadas à Places API (Text Search): {contador_api['chamadas']}")
    print(f"Acessos a sites (não conta como chamada de API do Google): {contador_sites['chamadas']}")


if __name__ == "__main__":
    main()
