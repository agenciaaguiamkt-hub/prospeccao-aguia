# Como colocar a ferramenta na nuvem (Streamlit Community Cloud)

Isso deixa a ferramenta acessível por um link, sem precisar do seu
computador ligado. É gratuito e não exige linha de comando nem instalar
Git — tudo pelo navegador.

Vale um alerta honesto antes de começar: esse caminho (Streamlit Community
Cloud) é o mais simples para você colocar no ar sozinho, mas não é o mesmo
nível de infraestrutura de uma empresa grande (sem API gateway dedicado,
sem WAF, sem autenticação por usuário individual). Para o volume de uso de
uma ferramenta de prospecção de agência pequena, isso é razoável — a chave
fica protegida (nunca sai do servidor) e o acesso é protegido por senha.
Se um dia o uso crescer muito, vale reavaliar com uma infra maior.

## Passo 1 — Criar uma conta no GitHub (se ainda não tiver)

1. Acesse https://github.com e crie uma conta gratuita.

## Passo 2 — Criar um repositório e subir os arquivos

Duas formas de fazer isso — escolha uma:

### Opção A — Sem Git, só upload pelo navegador (mais simples)

1. No GitHub, clique em "+" (canto superior direito) → "New repository".
2. Dê um nome, ex.: `prospeccao-aguia`. Marque como **Private** (não
   público) — mesmo sem a chave dentro do código, é mais seguro manter
   privado.
3. Clique em "Create repository".
4. Na página do repositório, clique em "Add file" → "Upload files".
5. Arraste estes arquivos (que eu te entreguei) para a área de upload:
   - `app.py`
   - `prospeccao_core.py`
   - `requirements.txt`
   - `.gitignore`
   - (não suba `secrets.toml.example` com valores reais preenchidos — o
     arquivo de exemplo em si, sem sua chave real, pode subir sem
     problema, só não crie um `secrets.toml` real e suba ele)
6. Clique em "Commit changes" para salvar.

### Opção B — Usando o Git que você já tem instalado

Já que você tem Git configurado, pode preferir isso (também facilita
atualizar a app depois: cada `git push` atualiza o site automaticamente).

1. Crie o repositório vazio no GitHub (mesmos passos 1-3 da Opção A).
2. No terminal, na pasta onde estão os arquivos (`app.py`,
   `prospeccao_core.py`, `requirements.txt`, `.gitignore`):
   ```
   git init
   git add app.py prospeccao_core.py requirements.txt .gitignore
   git commit -m "Ferramenta de prospecção"
   git branch -M main
   git remote add origin https://github.com/SEU-USUARIO/prospeccao-aguia.git
   git push -u origin main
   ```
   (troque `SEU-USUARIO` e o nome do repositório pelos seus).
3. Confira: o `.gitignore` já bloqueia `.streamlit/secrets.toml`, então
   mesmo que você tenha criado esse arquivo localmente para testar, ele
   não vai subir. Antes do primeiro `git add`, pode rodar
   `git status` e conferir que `secrets.toml` não aparece na lista.

## Passo 3 — Criar a conta no Streamlit Community Cloud

1. Acesse https://share.streamlit.io
2. Clique em "Sign up" / "Continue with GitHub" e autorize o acesso ao
   GitHub (login com a conta do Passo 1).

## Passo 4 — Publicar a app

1. Clique em "New app" (ou "Create app").
2. Escolha o repositório `prospeccao-aguia` que você criou.
3. Em "Main file path", digite: `app.py`
4. Antes de clicar em Deploy, clique em "Advanced settings" (ou no ícone
   de engrenagem) → aba **Secrets**.
5. Cole exatamente isto, substituindo pelos valores reais:
   ```toml
   GOOGLE_PLACES_API_KEY = "sua_chave_real_da_google_places_api"
   APP_PASSWORD = "uma_senha_que_só_você_e_sua_equipe_conhecem"
   ```
6. Clique em "Save", depois em "Deploy".
7. Aguarde 1-2 minutos. Você recebe um link (algo como
   `https://prospeccao-aguia.streamlit.app`) — esse é o endereço da sua
   ferramenta, acessível de qualquer lugar, protegido por senha.

## Depois de publicado

- Para trocar a senha ou a chave, vá em Settings → Secrets no painel do
  Streamlit Cloud e edite os valores — não precisa mexer no código nem
  reenviar arquivos.
- Se quiser atualizar o código no futuro (ex.: eu te mandar uma versão
  nova de `app.py`), é só repetir o Passo 2.5 (Add file → Upload files)
  substituindo o arquivo — a app na nuvem atualiza sozinha.
- Compartilhe o link + a senha só com quem precisa usar a ferramenta.
