# Configuração do MongoDB e Dokploy para o Personal Files MCP

Este documento descreve a arquitetura de integração entre o **Personal Files MCP** (`librechat-personal-files-mcp`), o **LibreChat**, o **MongoDB** e o **Traefik/Dokploy**.

---

## 1. Rede Compartilhada (Docker Network)

Para que o MCP consulte o MongoDB do LibreChat com segurança e baixa latência:

- **Rede interna compartilhada**: O container do MCP e o container do MongoDB devem pertencer à mesma rede Docker interna (ex.: `rieselgpt-backend`).
- **Segurança da porta 27017**: A porta `27017` do MongoDB **não deve ser publicada para a Internet** nem para o host externo. O tráfego ocorre estritamente dentro da rede Docker privada.
- **Configuração no Dokploy**:
  - Nas configurações do container do MCP no Dokploy, adicione a rede do backend (ex.: `rieselgpt-backend`).
  - Certifique-se de aplicar a alteração e recriar o container (verifique as redes ativas com `docker inspect <container>`).

---

## 2. Usuário e Permissões Dedicadas no MongoDB (`mcp_project_reader`)

O MCP precisa apenas de permissão de leitura (`find`) em duas coleções: `conversations` e `chatprojects`. Nunca conceda permissões de escrita ou privilégios administrativos.

### Criação da Função Customizada e Usuário via `mongosh`

Execute no terminal do container MongoDB (ou via `mongosh` autenticado como admin):

```javascript
// 1. Conectar ao banco do LibreChat
use LibreChat;

// 2. Criar a role restrita mcpProjectReader
db.createRole({
  role: "mcpProjectReader",
  privileges: [
    {
      resource: { db: "LibreChat", collection: "conversations" },
      actions: [ "find" ]
    },
    {
      resource: { db: "LibreChat", collection: "chatprojects" },
      actions: [ "find" ]
    }
  ],
  roles: []
});

// 3. Criar o usuário dedicado mcp_project_reader com senha forte
db.createUser({
  user: "mcp_project_reader",
  pwd: "COLOQUE_UMA_SENHA_FORTE_AQUI", // altere para sua senha segura
  roles: [
    { role: "mcpProjectReader", db: "LibreChat" }
  ]
});
```

### Responsabilidade de Isolamento e Projeções

> [!IMPORTANT]
> A função MongoDB restringe o acesso às **coleções**, mas **não filtra linhas ou campos**. O código do MCP é responsável por:
> 1. Filtrar sempre pelo usuário autenticado: `{"user": user_id}`.
> 2. Utilizar projeções estritas: `{"chatProjectId": 1}` em `conversations` e `{"_id": 1, "name": 1, "description": 1}` em `chatprojects`.
> 3. Nunca expor conversas ou projetos de outro usuário (`tenant isolation`).

---

## 3. Variáveis de Ambiente no Dokploy / Docker

Configure no container do MCP (ex.: Dokploy Environment):

```bash
# Conexão MongoDB (substitua usuário, senha, host da rede Docker e porta interna)
LIBRECHAT_MONGO_URI=mongodb://mcp_project_reader:SUA_SENHA@mongodb:27017/LibreChat?authSource=LibreChat
LIBRECHAT_MONGO_DB=LibreChat
LIBRECHAT_MONGO_TIMEOUT_MS=2000

# Header de Conversa enviado pelo LibreChat
CONVERSATION_HEADER=X-Conversation-Id

# URL pública base gerada pelo MCP para links de arquivos
PUBLIC_BASE_URL=https://chat.seu-dominio.com.br/files

# Diagnósticos (úteis para validação inicial de cabeçalhos e resolução)
PERSONAL_FILES_LOG_LEVEL=INFO
PERSONAL_FILES_CONTEXT_DIAGNOSTICS=true
```

> [!WARNING]
> Nunca versione credenciais reais no repositório GitHub e não use senhas fictícias como fallback padrão no código.

---

## 4. Configuração do `librechat.yaml` (Headers MCP)

No arquivo de configuração do LibreChat (`librechat.yaml`), configure os headers do servidor `personal-files`:

```yaml
mcpServers:
  personal-files:
    type: streamable-http
    url: http://librechat-personal-files:8080/mcp
    timeout: 120000
    chatMenu: false
    headers:
      X-User-ID: '{{LIBRECHAT_USER_ID}}'
      X-User-Email: '{{LIBRECHAT_USER_EMAIL}}'
    requestHeaders:
      X-Conversation-Id: '{{LIBRECHAT_BODY_CONVERSATIONID}}'
```

O LibreChat injeta dinamicamente o ID da conversa ativa através de `requestHeaders`, permitindo que o MCP resolva automaticamente o projeto vinculado.

---

## 5. Rota Pública `/files/` no Traefik (Dokploy)

Para servir arquivos publicados diretamente sem necessidade de proxy Nginx adicional:

1. No Dokploy, acesse a aplicação do MCP.
2. Adicione ou edite o domínio:
   - **Host**: `chat.seu-dominio.com.br` (o mesmo domínio do LibreChat)
   - **Path**: `/files/`
   - **Port**: `8080`
   - **HTTPS**: Ativado (Let's Encrypt automático pelo Traefik)

O Traefik roteia qualquer requisição iniciada por `/files/<token>` diretamente para o endpoint de download do MCP com streaming `FileResponse` e suporte a HTTP Range (RFC 7233).

---

## 6. Preservação de Volumes e Implantação

- **Preservação de Dados**: O volume montado em `/data/private` contém todos os arquivos e índices dos usuários. **Nunca apague ou recrie esse volume** em atualizações de versão.
- **Sem `container_name` fixo**: Permita que o Dokploy gerencie o ciclo de vida dos containers para viabilizar rolling updates e rollbacks seguros.
- **Uso de Imagens Publicadas**: Utilize tags estáveis publicadas no GitHub Container Registry (`ghcr.io/martinriesel/librechat-personal-files-mcp:<tag>`).
