# Diagnósticos e Logs

Este documento descreve o sistema de logs e diagnósticos do Personal Files MCP.

---

## 1. Variáveis de Controle

| Variável | Default | Descrição |
|----------|---------|-----------|
| `PERSONAL_FILES_LOG_LEVEL` | `INFO` | Nível de log da aplicação. Valores: `OFF`, `ERROR`, `WARNING`, `INFO`, `DEBUG`. |
| `PERSONAL_FILES_CONTEXT_DIAGNOSTICS` | `false` | Habilita diagnósticos JSON de contexto de projeto em cada request. |

---

## 2. Log Principal

O MCP usa o logger `personal_files_mcp` via `logging` padrão do Python. As mensagens incluem:

- Startup e lifespan (configurações carregadas, RAG API disponível, MongoDB conectado).
- Requisições HTTP (método, path, user_id, status).
- Operações de ferramentas MCP (save, index, transfer, reconcile).
- Erros de resolução de projeto e contexto.

**Formato**: texto plano no stdout do container. Para obter os logs no Dokploy:

```bash
# Via Docker
docker logs <container_id> --tail 100 -f

# Via Dokploy dashboard
# Acesse a aplicação → Logs
```

---

## 3. Diagnósticos de Contexto (`PERSONAL_FILES_CONTEXT_DIAGNOSTICS`)

Quando habilitado (`true`), cada request que passa pelo middleware de identidade emite uma linha JSON no log com:

```json
{
  "diag": "context",
  "request_id": "abc123",
  "user_id": "user_xyz",
  "conversation_id": "conv_abc",
  "conversation_status": "valid",
  "project_id": "proj_123",
  "project_name": "MeuProjeto",
  "resolver_status": "project",
  "timestamp": "2026-11-17T12:00:00Z"
}
```

As chaves são sanitizadas (sem dados sensíveis além dos IDs operacionais).

> [!TIP]
> Habilite os diagnósticos durante a validação inicial ou troubleshooting, mas **desabilite em produção** (`PERSONAL_FILES_CONTEXT_DIAGNOSTICS=false`) para reduzir volume de logs.

---

## 4. Troubleshooting Comum

### 4.1 Projeto não resolvido

**Sintoma**: `save_documentation` retorna `project_resolution_failed`.

**Checklist**:
1. Verificar se `LIBRECHAT_MONGO_URI` está configurada e acessível.
2. Verificar se o container do MCP e o MongoDB estão na mesma rede Docker.
3. Testar conectividade: `docker exec <mcp_container> python -c "from pymongo import MongoClient; c = MongoClient('mongodb://...', serverSelectionTimeoutMS=2000); print(c.server_info())"`.
4. Verificar se o usuário `mcp_project_reader` tem a role `mcpProjectReader`.
5. Habilitar `PERSONAL_FILES_CONTEXT_DIAGNOSTICS=true` e verificar os logs.

### 4.2 Conversa não encontrada (Turn 1)

**Sintoma**: `conversation_unavailable` com `retry_recommended: true`.

**Causa**: O LibreChat pode não ter inserido a conversa no MongoDB no momento da primeira chamada de ferramenta.

**Solução**: O MCP já aplica micro-retry (2 tentativas com 250ms de backoff). Se persistir, é uma condição de corrida incomum; a próxima chamada na mesma conversa resolverá corretamente.

### 4.3 Redes Docker após alteração no Dokploy

**Sintoma**: DNS timeout ou connection refused ao MongoDB.

**Solução**: Após alterar redes no Dokploy, **verifique as redes efetivas**:

```bash
docker inspect <mcp_container> --format '{{range .NetworkSettings.Networks}}{{.NetworkID}} {{end}}'
docker inspect <mongo_container> --format '{{range .NetworkSettings.Networks}}{{.NetworkID}} {{end}}'
```

Ambos devem compartilhar pelo menos uma rede. Se não, reaplique a configuração e recrie os containers.

### 4.4 Links públicos retornando 404

**Sintoma**: URL `https://dominio/files/<token>` retorna 404.

**Checklist**:
1. Verificar se a rota `/files/` está configurada no Traefik/Dokploy apontando para a porta `8080` do MCP.
2. Verificar se `PUBLIC_BASE_URL` corresponde ao domínio e path configurados.
3. Testar acesso direto ao container: `curl http://<container_ip>:8080/files/<token>`.
