# Arquitetura de Extensão: Integração Google Workspace (Google Drive) & SSO Corporativo no Google Chrome

Este documento estabelece o desenho arquitetural para estender o `librechat-personal-files-mcp`, viabilizando o suporte nativo ao **Google Workspace / Google Drive** corporativo em conjunto com o armazenamento local e a integração Microsoft OneDrive previamente projetada. A extensão preserva a totalidade das ferramentas originais, introduz um espelhamento simétrico de ferramentas prefixadas com `gdrive_*` e aproveita a infraestrutura com estações de trabalho utilizando o navegador **Google Chrome**, conta corporativa Google Workspace e autenticação com atrito mínimo (*zero-password / zero-prompt* via `login_hint` e `hd`).

---

## 1. Visão Geral e Princípios Arquiteturais

### 1.1 Contexto e Desafio
O `librechat-personal-files-mcp` opera como serviço *stateless* HTTP em `/mcp`, isolando dados por usuário em `/data/private/<userId>/...` com base no cabeçalho imutável `X-User-Id` injetado pelo LibreChat.

A extensão para Google Workspace atende aos seguintes pilares:
1. **Preservação e Paridade Funcional**: Manter a integridade de todas as ferramentas locais e OneDrive existentes, fornecendo o conjunto completo de ferramentas homólogas prefixadas com `gdrive_*` para manipulação de arquivos na nuvem do Google.
2. **Segurança e Menor Privilégio (*Least Privilege*)**: Utilização estrita do escopo OAuth `https://www.googleapis.com/auth/drive.file`. O servidor MCP **não** tem permissão de visualizar, ler ou modificar qualquer documento, planilha ou arquivo pessoal pré-existente no Drive do colaborador; ele acessa única e exclusivamente os arquivos e diretórios criados pela própria aplicação sob uma pasta raiz dedicada (ex.: `LibreChat Files`).
3. **SSO Fluido no Google Chrome**: Quando o usuário já está autenticado no Chrome com seu perfil Google Workspace corporativo, o fluxo OAuth aproveita os cabeçalhos `X-User-Email` e o domínio institucional (`hd`) para eliminar seleções manuais de conta e telas de login adicionais.
4. **Aplicação Interna (*Internal App*) no Google Cloud**: Elimina a necessidade de submissão do app ao processo público de verificação de escopos sensíveis do Google, impedindo a exibição de avisos de segurança ("App não verificado") para os colaboradores da empresa.

---

## 2. Topologia do Sistema e Fluxo de Autenticação Híbrida

O fluxo adota o padrão **OAuth 2.0 Authorization Code Grant com PKCE** integrado ao Nginx corporativo e orquestrado via rotas dedicadas no FastAPI.

```
┌─────────────────┐       (1) Tool Call: gdrive_login()          ┌──────────────────────────────┐
│  LibreChat      │─────────────────────────────────────────────▶│ librechat-personal-files-mcp │
│  (Chat UI)      │◀─────────────────────────────────────────────│  (FastAPI + FastMCP)         │
└────────┬────────┘       (2) URL com state assinado (JWT)       └──────────────┬───────────────┘
         │                    + login_hint & hd                                 │
         │ (3) Abre URL de Consentimento no Chrome                              │
         ▼                                                                      │
┌─────────────────┐                                                             │
│  Google Chrome  │──(4) Valida sessão Google Workspace corporativa             │
│ (Perfil Corpor.)│      sem solicitar senha                                    │
└────────┬────────┘                                                             │
         │                                                                      │
         │ (5) Redireciona para /auth/google/callback                           │
         ▼                                                                      │
┌─────────────────┐       (6) proxy_pass /auth/google/                          │
│ Nginx On-Premise│─────────────────────────────────────────────────────────────┘
└─────────────────┘       (7) Handshake com oauth2.googleapis.com
                                 & grava .gdrive_tokens.enc
```

### 2.1 Mecanismo de Associação de Sessão (*State Handshake*)
1. Ao invocar `gdrive_login`, o MCP recupera o `X-User-Id` e o `X-User-Email` dos headers da requisição.
2. O servidor gera um token JWT efêmero (10 minutos) assinado com o `JWT_SECRET`, embutindo `userId` e um nonce PKCE.
3. A URL de consentimento é gerada apontando para `https://accounts.google.com/o/oauth2/v2/auth` com os parâmetros:
   * `client_id`: ID do cliente registrado no Google Cloud.
   * `redirect_uri`: `https://<DOMINIO>/auth/google/callback`.
   * `response_type`: `code`.
   * `scope`: `https://www.googleapis.com/auth/drive.file offline_access`.
   * `access_type`: `offline` e `prompt`: `consent` (garante o envio do `refresh_token` duradouro).
   * `login_hint`: Endereço de e-mail do usuário extraído do cabeçalho `X-User-Email` (o Chrome seleciona a conta instantaneamente).
   * `hd`: Domínio restrito do Google Workspace (ex.: `empresa.com.br`), impedindo logins com contas Gmail particulares.
   * `state`: O token JWT efêmero assinado.
4. Após autorização com um clique no Chrome, o Google redireciona para o callback no Nginx.
5. O endpoint valida o `state`, extrai o `userId` garantido, troca o `code` pelos tokens e grava as credenciais cifradas.

### 2.2 Armazenamento Criptografado de Tokens
Os tokens de acesso e renovação do Google são persistidos de forma segura e estritamente isolada:
* **Caminho**: `/data/private/<userId>/.gdrive_tokens.enc`
* **Criptografia**: Cifrador Fernet (AES-128-CBC + HMAC SHA-256) com derivação de chave via PBKDF2 combinando o `JWT_SECRET` corporativo e o `userId`.
* **Renovação Transparente**: Antes de cada invocação da Google Drive API v3, o cliente verifica a expiração do `access_token`. Se estiver expirado ou a menos de 60 segundos do vencimento, o `refresh_token` é utilizado para renovação silenciosa em segundo plano.

---

## 3. Padrão de Design: Camada de Provedores de Armazenamento (*Provider Pattern*)

O servidor implementa a interface abstrata `IStorageProvider`, unificando a API de consumo para os três backends suportados:

```
                          ┌──────────────────────────┐
                          │    IStorageProvider      │
                          └─────────────┬────────────┘
                                        │
         ┌──────────────────────────────┼──────────────────────────────┐
         ▼                              ▼                              ▼
┌──────────────────┐          ┌──────────────────┐          ┌──────────────────┐
│  LocalStorage    │          │  OneDriveStorage │          │  GDriveStorage   │
│  Provider        │          │  Provider        │          │  Provider        │
│ (/data/private/) │          │ (/approot)       │          │ (drive.file)     │
└──────────────────┘          └──────────────────┘          └──────────────────┘
```

### 3.1 Gestão de Pastas no Google Drive
Diferente dos sistemas de arquivos convencionais baseados em caminhos absolutos (*paths*), o Google Drive opera baseado em IDs de objetos (`fileId`):
* **Pasta Raiz da Aplicação**: Na primeira conexão do usuário, o provedor consulta se a pasta configurada (ex.: `LibreChat Files`) já existe sob a raiz (`'root' in parents and name = 'LibreChat Files' and mimeType = 'application/vnd.google-apps.folder' and trashed = false`). Caso não exista, ela é criada e seu `folder_id` é memorizado no arquivo de metadados do usuário.
* **Resolução de Caminhos Hierárquicos**: Para caminhos como `docs/relatorios/deck.html`, o `GDriveStorageProvider` faz a resolução recursiva de diretórios ou os cria de forma idempotente, mantendo cache em memória dos IDs para minimizar requisições à API.

---

## 4. Matriz do Catálogo de Tools

Todas as ferramentas locais e OneDrive permanecem intactas. Disponibilizam-se as ferramentas homólogas prefixadas com `gdrive_*`:

### 4.1 Ferramentas de Gerenciamento de Autenticação

| Tool | Assinatura | Descrição |
|---|---|---|
| `gdrive_login` | *nenhum* | Gera URL de autorização OAuth 2.0 com `login_hint` e restrição `hd` para o Chrome. |
| `gdrive_auth_status` | *nenhum* | Informa se o usuário está autenticado, dados da conta e cota do Google Drive. |
| `gdrive_logout` | *nenhum* | Revoga tokens na API do Google e deleta `.gdrive_tokens.enc` da pasta privada do usuário. |

### 4.2 Ferramentas de Arquivos (Storage)

| Tool Local Original | Ferramenta Replicada GDrive | Particularidades da Implementação Google Drive |
|---|---|---|
| `list_files` | `gdrive_list_files` | Lista itens dentro da pasta do app no Drive via `drive.files.list`. Suporta `pageSize` e `pageToken`. |
| `read_file` | `gdrive_read_file` | Faz o download do fluxo de dados com `alt=media`. Limite de 2 MB em memória para textos UTF-8. |
| `read_multiple_files` | `gdrive_read_multiple_files` | Executa downloads concorrentes assíncronos via `asyncio.gather`. |
| `write_file` | `gdrive_write_file` | Cria ou substitui arquivo via upload simples (multipart) ou resumable upload para binários. |
| `update_file` | `gdrive_update_file` | Atualiza conteúdo de arquivo existente garantindo que o caminho pré-existe. |
| `edit_file` | `gdrive_edit_file` | Baixa o arquivo textual, aplica edições de bloco e realiza re-upload atômico. |
| `delete_file` | `gdrive_delete_file` | Move o arquivo ou pasta para a lixeira do Google Drive (`trashed=true`). |
| `move_file` | `gdrive_move_file` | Atualiza `addParents` e `removeParents` via `drive.files.update` para mover entre diretórios. |
| `get_file_info` | `gdrive_get_file_info` | Retorna `md5Checksum`, tamanho, datas RFC3339, `mimeType` e `webViewLink`. |

### 4.3 Ferramentas de Documentação e RAG

| Tool Local Original | Ferramenta Replicada GDrive | Particularidades da Implementação Google Drive |
|---|---|---|
| `save_documentation` | `gdrive_save_documentation` | Grava em `docs/` dentro da pasta do app no Drive e sincroniza o `memory-index.json`. |
| `update_documentation` | `gdrive_update_documentation` | Atualiza documento em `docs/` e reconcilia metadados no índice remoto. |
| `get_document_metadata` | `gdrive_get_document_metadata` | Lê a entrada do documento no `memory-index.json` remoto hospedado no Drive. |
| `search_knowledge` | *Inalterado* | Continua utilizando `rag_api` central do LibreChat. |
| `index_document` | `gdrive_index_document` | Transfere buffer do Google Drive para ingestão vetorial no `rag_api`. |
| `remove_from_knowledge` | `gdrive_remove_from_knowledge`| Limpa os chunks correspondentes ao documento no pgvector via `rag_api`. |
| `get_index_status` | `gdrive_get_index_status` | Apresenta contadores unificados de documentos indexados locais e remotos. |

### 4.4 Ferramentas de Publicação e Links Diretos

| Tool Local Original | Ferramenta Replicada GDrive | Particularidades da Implementação Google Drive |
|---|---|---|
| `publish_file` | `gdrive_publish_file` | Insere permissão `role=reader` e `type=anyone` (ou `type=domain`) via `drive.permissions.create`. |
| `unpublish_file` | `gdrive_unpublish_file` | Remove permissões públicas ou de domínio associadas ao arquivo no Drive. |
| `get_public_link` | `gdrive_get_public_link` | Retorna o link corporativo ativo com token de compartilhamento. |
| `list_public_links` | `gdrive_list_public_links` | Lista arquivos que possuem permissões compartilhadas ativas. |
| *Nova Ferramenta* | `gdrive_get_direct_link` | Gera URL de download direto (`export=download` ou link de gateway inline). |

---

## 5. Engenharia de Links Diretos: Arquivos `.xml` e `.html`

O Google Drive não atua como servidor de hospedagem estática e bloqueia chamadas CORS públicas em seus endpoints brutos. Para servir dados e renderizar páginas de forma confiável:

### 5.1 Arquivos `.xml` (Dados e Diagramas draw.io)
* **Download Direto**: O link `https://drive.google.com/uc?export=download&id={fileId}` força o download do arquivo, mas não permite renderização dinâmica em visualizadores externos devido à falta do cabeçalho `Access-Control-Allow-Origin: *`.
* **Solução CORS**: Para alimentar visualizadores como diagrams.net (`#U{URL}`) ou scripts web, a URL deve ser roteada através do gateway do MCP, que entrega o arquivo com `Access-Control-Allow-Origin: *`.

### 5.2 Arquivos `.html` Únicos (Apresentações Reveal.js e Relatórios)
A tentativa de abrir um arquivo HTML diretamente pelo Google Drive exibe uma tela com o código-fonte ou força o download. A visualização nativa sem atrito no Google Chrome é viabilizada pelo **Modo Gateway Pass-Through**:

```nginx
# Endpoint no Nginx corporativo que faz streaming inline do HTML salvo no Google Drive
location ^~ /gdrive/view/ {
    proxy_pass http://librechat-personal-files:8080/gdrive/view/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

* **Mecânica Operacional**:
  1. A tool `gdrive_publish_file(path)` registra um token público opaco no banco SQLite local (`links.db`).
  2. Ao acessar `https://librechat.empresa.local/gdrive/view/{token}`, o endpoint local faz a busca autenticada via Google Drive API (`drive.files.get(alt='media')`) e responde para o Chrome com:
     ```http
     HTTP/1.1 200 OK
     Content-Type: text/html; charset=utf-8
     Content-Disposition: inline
     X-Frame-Options: SAMEORIGIN
     Access-Control-Allow-Origin: *
     ```
  3. O navegador Chrome renderiza os slides Reveal.js, fontes tipográficas e scripts instantaneamente, em tela cheia e sem qualquer visualizador intermediário.

---

## 6. Alterações no Código-Fonte e Estrutura de Diretórios

A arquitetura do repositório evolui para um modelo multiprovedor modular:

```
src/personal_files_mcp/
├── identity.py             # Validação estrita de X-User-Id e X-User-Email
├── docindex.py             # Gerenciamento de memory-index.json (local e remoto)
├── rag_client.py           # Comunicação JWT HS256 com rag_api
├── publishing.py           # Registry SQLite de links + endpoints /files/{token}
├── config.py               # Configurações unificadas (Local, Azure e Google)
├── app.py                  # FastAPI: rotas /auth/google/* e /gdrive/view/*
├── tools.py                # Catálogo unificado de tools (locais, onedrive_* e gdrive_*)
├── providers/
│   ├── base.py             # Interface abstrata IStorageProvider
│   ├── local.py            # Provedor sistema de arquivos local (/data/private)
│   ├── onedrive.py         # Provedor Microsoft Graph API (/approot)
│   └── gdrive.py           # NOVO: Provedor Google Drive API (drive.file)
├── gdrive_auth.py          # NOVO: OAuth2 Google, PKCE, token refresh e crypto Fernet
└── gdrive_client.py        # NOVO: cliente assíncrono httpx para Google Drive API v3
```

---

## 7. Configuração de Infraestrutura e Implantação

### 7.1 Configurações de Ambiente (`.env`)

Adicionar ao arquivo `.env` corporativo:

```dotenv
# Configurações Google Workspace / Google Cloud
GOOGLE_CLIENT_ID="seu-client-id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="seu-client-secret"
GOOGLE_REDIRECT_URI="https://librechat.empresa.local/auth/google/callback"
GOOGLE_WORKSPACE_DOMAIN="empresa.com.br"
GOOGLE_APP_FOLDER_NAME="LibreChat Files"
GOOGLE_SCOPES="https://www.googleapis.com/auth/drive.file offline_access"
```

### 7.2 Atualização do Nginx Corporativo

Adicionar ao bloco de configuração do Nginx responsável pelo LibreChat e pelo MCP:

```nginx
# Callback de autenticação Google OAuth2
location ^~ /auth/google/ {
    proxy_pass http://librechat-personal-files:8080/auth/google/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# Gateway para visualização direta de HTML do Google Drive
location ^~ /gdrive/view/ {
    proxy_pass http://librechat-personal-files:8080/gdrive/view/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

---

## 8. Tratamento de Falhas, Resiliência e Políticas de Segurança

1. **Princípio Fail-Closed**: Se o usuário não conectou a conta Google ou o consentimento foi revogado, qualquer chamada a `gdrive_*` retorna `ERROR: gdrive_unauthorized — Execute gdrive_login primeiro.`. As ferramentas locais e OneDrive continuam operando normalmente.
2. **Resiliência a Quotas do Google (HTTP 403 / 429)**: Implementação de interceptador HTTP com algoritmo de *Truncated Exponential Backoff* com *Jitter*, conforme especificação oficial do Google Cloud para tratamento de `rateLimitExceeded` ou `userRateLimitExceeded`.
3. **Restrição Estrita de Domínio Institucional**:
   * O parâmetro `hd` na URL de autorização força a restrição no lado da interface de consentimento do Google.
   * O endpoint `/auth/google/callback` valida em tempo de execução se o campo `hd` do ID Token decodificado bate com `GOOGLE_WORKSPACE_DOMAIN`. Contas `@gmail.com` ou de outros domínios são sumariamente rejeitadas com HTTP 403.
4. **Isolamento de Concorrência**: O índice `memory-index.json` remoto utiliza validação de versão via header `If-Match` com base no `etag` retornado pelo Google Drive, prevenindo perdas de escrita sob chamadas concorrentes.
