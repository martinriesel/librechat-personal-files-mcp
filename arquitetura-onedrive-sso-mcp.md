# Arquitetura de Extensão: Integração Microsoft OneDrive (AppFolder) & SSO Corporativo

Este documento estabelece o desenho arquitetural para estender o `librechat-personal-files-mcp`, viabilizando o suporte nativo ao **Microsoft OneDrive** corporativo em conjunto com o armazenamento local já existente. A extensão preserva todas as ferramentas originais, introduz um espelhamento simétrico de ferramentas para o OneDrive e aproveita a infraestrutura on-premise com estações de trabalho no domínio, navegador Microsoft Edge e autenticação sem senha (*passwordless*) via Windows Hello (PRT).

---

## 1. Visão Geral e Princípios Arquiteturais

### 1.1 Contexto e Desafio
O servidor MCP atual opera de maneira *stateless* via HTTP no endpoint `/mcp`, isolando o armazenamento de arquivos em disco local (`/data/private/<userId>/...`) e identificando o usuário unicamente pelo header corporativo `X-User-Id` injetado pelo LibreChat.

A nova funcionalidade atende a dois requisitos centrais:
1. **Preservação e Paridade Funcional**: Manter a integridade das 20 ferramentas existentes e disponibilizar ferramentas homólogas prefixadas com `onedrive_*` para operar diretamente no armazenamento em nuvem do usuário.
2. **Segurança de Menor Privilégio (*Least Privilege*)**: Isolar a atuação do MCP estritamente dentro da subpasta de aplicativo do OneDrive (`OneDrive/Aplicativos/<NomeDoApp>`), sem conceder acesso a todo o disco do colaborador.
3. **Experiência *Zero-Prompt* no Domínio**: Alavancar o *Primary Refresh Token* (PRT) injetado nativamente pelo Microsoft Edge em computadores no domínio corporativo com Windows Hello para realizar o consentimento OAuth sem atrito de senhas.

---

## 2. Topologia do Sistema e Fluxo de Autenticação Híbrida

O fluxo adota o padrão **OAuth 2.0 Authorization Code Grant com PKCE** integrado ao Nginx corporativo e orquestrado via FastAPI.

```
┌─────────────────┐       (1) Tool Call: onedrive_login()        ┌──────────────────────────────┐
│  LibreChat      │─────────────────────────────────────────────▶│ librechat-personal-files-mcp │
│  (Chat UI)      │◀─────────────────────────────────────────────│  (FastAPI + FastMCP)         │
└────────┬────────┘       (2) URL com state assinado (JWT)       └──────────────┬───────────────┘
         │                                                                      │
         │ (3) Abre URL de Login no Edge                                        │
         ▼                                                                      │
┌─────────────────┐                                                             │
│ Microsoft Edge  │──(4) Injeta PRT do Windows Hello                            │
│ (Dominio / SSO) │      sem solicitar senha                                    │
└────────┬────────┘                                                             │
         │                                                                      │
         │ (5) Redireciona para /auth/azure/callback                            │
         ▼                                                                      │
┌─────────────────┐       (6) proxy_pass /auth/azure/                           │
│ Nginx On-Premise│─────────────────────────────────────────────────────────────┘
└─────────────────┘       (7) Handshake com login.microsoftonline.com
                                 & grava .azure_tokens.enc
```

### 2.1 Mecanismo de Associação de Sessão (*State Handshake*)
Como o MCP é *stateless* e recebe o usuário via cabeçalho HTTP, a vinculação entre o navegador web e o ID do chat é garantida criptograficamente:
1. Ao invocar `onedrive_login`, o MCP gera um token de estado assinado (JWT efêmero, validade de 10 minutos) contendo o `userId` obtido do header `X-User-Id`, utilizando o `JWT_SECRET` compartilhado.
2. O usuário abre o link no Edge: `https://<DOMINIO>/auth/azure/login?state=<JWT_STATE>`.
3. O Edge interage com o Microsoft Entra ID transmitindo o PRT corporativo. Como a máquina está no domínio com Windows Hello, o consentimento do escopo `Files.ReadWrite.AppFolder` ocorre instantaneamente.
4. A Microsoft redireciona para `https://<DOMINIO>/auth/azure/callback?code=...&state=...`.
5. O endpoint no MCP valida a assinatura do `state`, extrai o `userId` garantido, troca o `code` pelo par `(access_token, refresh_token)` e armazena os segredos cifrados em disco.

### 2.2 Armazenamento Criptografado de Tokens
Os tokens de cada usuário são persistidos isoladamente dentro da sua própria pasta privada existente:
* **Caminho**: `/data/private/<userId>/.azure_tokens.enc`
* **Criptografia**: AES-128-CBC / Fernet com chave derivada via PBKDF2 (SHA-256) combinando o `JWT_SECRET` corporativo e o `userId`.
* **Renovação Transparente**: Um middleware interno no cliente Graph verifica a expiração do `access_token` antes de cada requisição. Se expirado, realiza a renovação automática usando o `refresh_token` sem intervenção do usuário.

---

## 3. Padrão de Design: Desacoplamento e Camada de Storage

Para manter o código modular e permitir a replicação limpa das ferramentas, o backend adota a padronização de interfaces de repositório (*Provider Pattern*):

```
                   ┌──────────────────────────┐
                   │    IStorageProvider      │  (Interface Abstrata)
                   └─────────────┬────────────┘
                                 │
            ┌────────────────────┴────────────────────┐
            ▼                                         ▼
┌─────────────────────────┐               ┌─────────────────────────┐
│   LocalStorageProvider  │               │  OneDriveStorageProvider│
│ (Path /data/private/...)│               │ (/me/drive/special/...) │
└─────────────────────────┘               └─────────────────────────┘
```

* **`LocalStorageProvider`**: Encapsula as chamadas de sistema POSIX existentes em `src/personal_files_mcp/storage.py`.
* **`OneDriveStorageProvider`**: Consome os endpoints da Microsoft Graph API baseados no prefixo canônico `/me/drive/special/approot`.
  * Upload de arquivos até 4 MB: `PUT /me/drive/special/approot:/{path}:/content`
  * Upload fragmentado (> 4 MB): `POST /me/drive/special/approot:/{path}:/createUploadSession`
  * Download direto: `GET /me/drive/special/approot:/{path}:/content`
  * Operações de metadados: `GET`, `PATCH`, `DELETE` em `/me/drive/special/approot:/{path}`

---

## 4. Matriz do Catálogo de Tools

Todas as 20 ferramentas originais permanecem inalteradas. Novas ferramentas homólogas são disponibilizadas para interagir exclusivamente com o OneDrive corporativo:

### 4.1 Ferramentas de Gerenciamento de Autenticação

| Tool | Assinatura | Descrição |
|---|---|---|
| `onedrive_login` | *nenhum* | Inicia o fluxo de autorização gerando a URL compatível com SSO/Edge. |
| `onedrive_auth_status` | *nenhum* | Retorna se a conta está conectada, validade da sessão e cota do AppFolder. |
| `onedrive_logout` | *nenhum* | Revoga tokens no Entra ID e elimina `.azure_tokens.enc` do usuário. |

### 4.2 Ferramentas de Arquivos (Storage)

| Tool Local Original | Ferramenta Replicada OneDrive | Particularidades da Implementação OneDrive |
|---|---|---|
| `list_files` | `onedrive_list_files` | Lista o diretório `/approot`. Suporta paginação via `@odata.nextLink`. |
| `read_file` | `onedrive_read_file` | Faz o streaming do conteúdo textual via Graph API. Limite de 2 MB em memória. |
| `read_multiple_files` | `onedrive_read_multiple_files` | Executa downloads assíncronos concorrentes via `asyncio.gather`. |
| `write_file` | `onedrive_write_file` | Grava/sobrescreve em `/approot:/{path}:/content`. Cria diretórios automaticamente. |
| `update_file` | `onedrive_update_file` | Garante pré-existência do item antes de enviar o payload de atualização. |
| `edit_file` | `onedrive_edit_file` | Baixa o arquivo, aplica as substituições de string no servidor e faz o re-upload. |
| `delete_file` | `onedrive_delete_file` | Remove o item físico de `/approot:/{path}` (enviando para a lixeira do OneDrive). |
| `move_file` | `onedrive_move_file` | Emite `PATCH` com `parentReference` para mover/renomear dentro do `/approot`. |
| `get_file_info` | `onedrive_get_file_info` | Retorna hashes (SHA1/QuickXorHash), tamanho, data e link web nativo. |

### 4.3 Ferramentas de Documentação e RAG

| Tool Local Original | Ferramenta Replicada OneDrive | Particularidades da Implementação OneDrive |
|---|---|---|
| `save_documentation` | `onedrive_save_documentation` | Grava o documento em `/approot/docs/` e atualiza `memory-index.json` remoto. |
| `update_documentation` | `onedrive_update_documentation` | Atualiza documento em `docs/` e reconcilia metadados no catálogo remoto. |
| `get_document_metadata` | `onedrive_get_document_metadata` | Lê a entrada do documento dentro do `memory-index.json` no OneDrive. |
| `search_knowledge` | *Inalterado* | Continua utilizando `rag_api`. A busca é unificada pelo tenant do usuário. |
| `index_document` | `onedrive_index_document` | Faz o download temporário do OneDrive e envia o buffer ao `rag_api`. |
| `remove_from_knowledge` | `onedrive_remove_from_knowledge`| Remove os vetores associados ao caminho do OneDrive no `rag_api`. |
| `get_index_status` | `onedrive_get_index_status` | Status consolidado de arquivos indexados locais e remotos. |

### 4.4 Ferramentas de Publicação e Links Diretos

| Tool Local Original | Ferramenta Replicada OneDrive | Particularidades da Implementação OneDrive |
|---|---|---|
| `publish_file` | `onedrive_publish_file` | Cria link de compartilhamento organizacional via `createLink` na Graph API. |
| `unpublish_file` | `onedrive_unpublish_file` | Revoga a permissão de compartilhamento associada ao arquivo no OneDrive. |
| `get_public_link` | `onedrive_get_public_link` | Retorna link corporativo ativo com token de compartilhamento. |
| `list_public_links` | `onedrive_list_public_links` | Lista arquivos da pasta do app que possuem links de compartilhamento ativos. |
| *Nova Ferramenta* | `onedrive_get_direct_link` | Gera URL com `@microsoft.graph.downloadUrl` (temporária) ou `?download=1`. |

---

## 5. Engenharia de Links Diretos: Arquivos `.xml` e `.html`

Devido às políticas de segurança do SharePoint/OneDrive (bloqueio de execução estática e ausência de cabeçalhos CORS abertos), adota-se a seguinte estratégia para servir arquivos brutos e páginas web:

### 5.1 Arquivos `.xml` e Dados Brutos
* A tool `onedrive_get_direct_link(path, link_type="download")` gera links que anexam o parâmetro `?download=1` à URL de compartilhamento institucional, instruindo o serviço a responder com o binário original em vez de abrir a interface do portal Microsoft 365.

### 5.2 Arquivos `.html` Únicos (Apresentações e Relatórios)
O OneDrive força `Content-Disposition: attachment` ou exibe o código-fonte em um visualizador para arquivos `.html`. Para viabilizar a visualização direta no navegador sem download manual prévio, implementa-se o **Modo Gateway Pass-Through** através da infraestrutura Nginx já existente:

```nginx
# Endpoint no Nginx corporativo que faz streaming inline do HTML salvo no OneDrive
location ^~ /onedrive-view/ {
    proxy_pass http://librechat-personal-files:8080/onedrive/view/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

* **Mecânica**: O endpoint `/onedrive/view/{token}` recebe a requisição, baixa o arquivo via Graph API autenticado no backend e entrega para o Edge com os seguintes cabeçalhos:
  ```http
  HTTP/1.1 200 OK
  Content-Type: text/html; charset=utf-8
  Content-Disposition: inline
  X-Frame-Options: SAMEORIGIN
  ```
Isso permite carregar decks e documentos HTML de forma segura e nativa no navegador corporativo.

---

## 6. Alterações no Código-Fonte e Estrutura de Diretórios

A estrutura do repositório `mcp-personal-files` é expandida preservando os módulos existentes:

```
src/personal_files_mcp/
├── identity.py             # Inalterado: validação estrita de X-User-Id
├── storage.py              # Inalterado: provedor de armazenamento local
├── docindex.py             # Inalterado: gerencia memory-index.json local
├── rag_client.py           # Inalterado: comunicação JWT com rag_api
├── publishing.py           # Inalterado: links SQLite e X-Accel-Redirect
├── config.py               # Expandido: configurações Azure (Client ID, Tenant, Scopes)
├── app.py                  # Expandido: registro de rotas /auth/azure e /onedrive/view
├── tools.py                # Expandido: registro das tools onedrive_*
├── onedrive_client.py      # NOVO: cliente assíncrono httpx para Microsoft Graph API
├── onedrive_auth.py        # NOVO: rotinas OAuth2 PKCE, token refresh e crypto Fernet
└── onedrive_storage.py     # NOVO: implementação do provedor OneDrive (/approot)
```

---

## 7. Configuração de Infraestrutura e Implantação

### 7.1 Configurações de Ambiente (`.env`)

Adicionar ao arquivo `.env` corporativo:

```dotenv
# Configurações do Azure / Microsoft Graph
AZURE_TENANT_ID="seu-tenant-id-guid-aqui"
AZURE_CLIENT_ID="seu-client-id-guid-aqui"
AZURE_CLIENT_SECRET="seu-client-secret-aqui"
AZURE_SCOPES="Files.ReadWrite.AppFolder offline_access User.Read"
AZURE_REDIRECT_URI="https://librechat.empresa.local/auth/azure/callback"
```

### 7.2 Atualização do Nginx Corporativo

Adicionar ao bloco de configuração do Nginx responsável pelo LibreChat e pelo MCP:

```nginx
# Callback de autenticação Microsoft Graph OAuth2
location ^~ /auth/azure/ {
    proxy_pass http://librechat-personal-files:8080/auth/azure/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# Gateway para visualização direta de HTML do OneDrive
location ^~ /onedrive/view/ {
    proxy_pass http://librechat-personal-files:8080/onedrive/view/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

---

## 8. Tratamento de Falhas e Políticas de Segurança

1. **Princípio Fail-Closed**: Se um usuário não executou a autorização do OneDrive ou se o `refresh_token` foi revogado pela TI, as tools `onedrive_*` retornam imediatamente `ERROR: onedrive_unauthorized — Execute onedrive_login primeiro.`. As ferramentas locais permanecem plenamente operacionais.
2. **Resiliência a *Rate Limiting* (HTTP 429)**: O cliente Graph implementa interceptador automático para o cabeçalho `Retry-After`, aplicando *backoff* exponencial com *jitter* para evitar bloqueios de API.
3. **Integridade de Concorrência**: Operações no índice de documentação do OneDrive (`memory-index.json`) utilizam controle de concorrência otimista via cabeçalho `If-Match` com eTag do Graph, evitando sobrescritas acidentais em chamadas simultâneas.
4. **Isolamento de Tenants**: O registro de aplicativo no Entra ID configurado como *Single Tenant* bloqueia qualquer tentativa de conexão proveniente de contas externas ou pessoais, aceitando exclusivamente identidades emitidas pelo Active Directory da organização.
