# Como rodar a busca de clínicas localmente (para teste/depuração)

Para o uso do dia a dia, o recomendado é publicar a ferramenta na nuvem —
veja `DEPLOY_NUVEM.md`. Este guia aqui é para rodar no seu computador,
útil para testar antes de publicar.

Existem duas formas de usar localmente:

- **`app.py`** — interface visual no navegador: você digita a busca numa
  telinha, clica em "Buscar" e baixa a planilha por um botão.
- **`prospeccao_places.py`** — versão de terminal.

Os dois dependem de `prospeccao_core.py` — mantenha os arquivos na mesma
pasta.

**Importante sobre a chave de API:** por segurança, a chave não fica mais
escrita em nenhum arquivo. Você precisa informá-la de uma das formas
abaixo antes de rodar:

- Para `app.py`: crie uma pasta chamada `.streamlit` e dentro dela um
  arquivo `secrets.toml` com o conteúdo do `secrets.toml.example` (que eu
  te entreguei), preenchendo com sua chave real.
- Para `prospeccao_places.py`: o script vai perguntar a chave no terminal
  (a digitação fica oculta, não aparece na tela) - ou você pode
  configurá-la como variável de ambiente `GOOGLE_PLACES_API_KEY` antes de
  rodar.

## Windows

1. Instale o Python: acesse https://www.python.org/downloads/ e baixe a
   versão mais recente.
   - **Importante:** na primeira tela do instalador, marque a caixa
     "Add python.exe to PATH" antes de clicar em Instalar.
2. Coloque os três arquivos (`app.py`, `prospeccao_core.py` e
   `prospeccao_places.py`) na mesma pasta, por exemplo `C:\prospeccao\`.
3. Abra o Prompt de Comando: aperte a tecla Windows, digite `cmd` e
   pressione Enter.
4. Navegue até a pasta (troque pelo caminho real):
   ```
   cd C:\prospeccao
   ```
5. Instale as bibliotecas necessárias (só precisa fazer isso uma vez):
   ```
   pip install streamlit requests pandas openpyxl
   ```
6a. Para usar a **interface visual** (recomendado):
   ```
   streamlit run app.py
   ```
   Isso abre uma aba no seu navegador automaticamente. Preencha a busca,
   clique em "Buscar" e depois em "⬇️ Baixar planilha". Para fechar,
   feche a aba e aperte Ctrl+C no terminal.

6b. Para usar a **versão de terminal** (igual antes):
   ```
   python prospeccao_places.py
   ```
   Aguarde. Ele mostra no terminal cada site checado, e no final avisa
   "Planilha salva em: prospeccao_votuporanga.xlsx" na mesma pasta.

## Mac

1. Abra o Terminal (Spotlight → digite "Terminal").
2. Verifique se já tem Python: digite `python3 --version`. Se não tiver,
   baixe em https://www.python.org/downloads/.
3. Navegue até a pasta onde salvou os arquivos, por exemplo:
   ```
   cd ~/Downloads
   ```
4. Instale as bibliotecas (uma vez só):
   ```
   pip3 install streamlit requests pandas openpyxl
   ```
5a. Interface visual: `streamlit run app.py`
5b. Terminal: `python3 prospeccao_places.py`

## O que esperar

- O processo demora alguns segundos a minutos (depende de quantos sites
  existem e da velocidade de cada um).
- Na interface visual, uma barra de progresso mostra o andamento; no
  terminal, aparece o nome de cada site sendo checado.
- No final aparece um resumo com o total de chamadas feitas à Google
  Places API e o total de sites acessados (contados separadamente).
- A planilha abre normalmente no Excel ou Google Sheets, com as colunas:
  Nome, Endereço, Telefone, É celular?, Site, Google Meu Negócio, Nota,
  Nº de avaliações, Instagram.

## Se der erro

- `'python' não é reconhecido...`: o Python não foi adicionado ao PATH.
  Reinstale marcando a caixa mencionada no passo 1, ou reinicie o
  computador após instalar.
- Erro de conexão / token de página inválido: rode novamente — às vezes a
  API demora alguns segundos a mais para liberar a próxima página de
  resultados.
- Não há mais limite fixo de resultados — a busca traz tudo que a Places
  API devolver. Se vier um número menor do que você esperava (comumente
  perto de ~60), é a própria API que parou de paginar para essa busca —
  isso é normal e não indica erro no script.
- "Places API (New) has not been used in project... or it is disabled":
  entre no link que aparece na própria mensagem de erro e clique em
  "Ativar" (também confirme se o faturamento/billing está ativo no
  projeto do Google Cloud). Espere 1-2 minutos e tente de novo.
