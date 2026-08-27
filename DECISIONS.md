# DECISIONS.md — decisões tomadas além do briefing

Registra toda decisão tomada pelo agente além do briefing `opcode-agent-briefing.md`,
conforme exigido na seção 0 ("Qualquer decisão não coberta aqui deve ser registrada em
DECISIONS.md dentro do projeto, nunca inventada silenciosamente.").

---

## D001 — Build backend e empacotamento
**Decisão:** Usar `hatchling` como build backend e `pyproject.toml` (PEP 621) único arquivo
de configuração de projeto/dependências/lint/testes.
**Motivo:** Stack moderna padrão Python 3.12; evita `setup.py`/`setup.cfg` legados. Hatchling
é leve e suporta layout `src/`.
**Data:** 2026-08-26

## D002 — Detecção de MIME type
**Decisão:** Usar `python-magic` (libmagic) para inferir MIME em `storage.stat()` e no
endpoint público, com fallback para ` mimetypes.guess_type` quando libmagic não estiver
disponível no runtime.
**Motivo:** O briefing exige `mime` em `FileInfo` e no `Content-Disposition` da publicação.
libmagic é mais preciso que `mimetypes` da stdlib. Fallback garanta funcionamento mesmo
em slim images sem libmagic (caso decida-se reduzir camadas no futuro).
**Data:** 2026-08-26

## D003 — Tipagem de dados retornados pelas tools
**Decisão:** Todas as tools MCP retornam texto estruturado (`str`), nunca exceções cruas.
Erros seguem o formato `ERROR: <categoria> — <motivo>` em uma linha, seguido de detalhe
quando aplicável. Sucesso retorna um bloco legível (chave/valor ou lista).
**Motivo:** Briefing §4.6 exige "Erros como conteúdo textual estruturado (`ERROR: ...` +
motivo), nunca exceção crua". Esse contrato também maximiza utilidade para o LLM.
**Data:** 2026-08-26

## D004 — UUID v4 como file_id no rag_api
**Decisão:** `file_id` enviado ao `POST /embed` é `uuid.uuid4()` (str), gerado pelo MCP e
persistido em `docindex` (`rag_file_id`). Nunca exposto ao LLM como argumento.
**Motivo:** Briefing §4.4: "file_id é UUID v4 gerado pelo MCP, NUNCA escolhido pelo LLM".
Evita colisão/cross-user e permite DELETE canônico.
**Data:** 2026-08-26

## D005 — Healthz reporta modo degradado
**Decisão:** Endpoint `GET /healthz` retorna JSON com campos: `status`
(`ok`|`degraded`), `rag` (`available`|`disabled`|`unreachable`), `storage` (`ok`|`error`),
`version`. HTTP 200 sempre (para o orquestrador), status refletido no corpo.
**Motivo:** Briefing §5: "healthz reporta degradado" quando JWT_SECRET fraco/ausente.
HTTP 200 mantém compat com probes simples; corpo dá detalhe.
**Data:** 2026-08-26

## D006 — Execução de testes sem rede
**Decisão:** Fase A/B/C usam apenas `pytest` + `httpx.ASGITransport` (TestClient) +
`respx` para mocks. Nenhum teste dispara chamadas de rede reais, garantindo verde em CI
sem rag_api/containers.
**Motivo:** Briefing Fase A: "maximizar o que testa sozinho (unitário + integração local
com mocks) antes de pedir restart."
**Data:** 2026-08-26

## D007 — Ordem de implementação por fases
**Decisão:** Implementar as fases A→B→C→F (empacotamento) antes de Fase D/E, porque estas
últimas dependem do usuário reiniciar a stack. Todo o código que roda localmente fica pronto
e testado; o empacotamento final só depende de teste de smoke local. Fase D/E são
instruções entregues ao usuário no final.
**Motivo:** Briefing §6 define critérios de aceite por fase e exige PARAR antes de pedir
restart. Antecipar o empacotamento (Fase F) mantém o repositório publicável assim que o
usuário validar a integração real.
**Data:** 2026-08-26

## D008 — MCP stateless HTTP + FastMCP lifespan forwarding
**Decisão:** Usar `FastMCP.http_app(path="/", stateless_http=True)` montado em `/mcp` no
FastAPI, com o lifespan do sub-app MCP encaminhado via `async with mcp_app.lifespan(app)`.
**Motivo:** FastMCP 3.4+ exige que o `StreamableHTTPSessionManager` seja inicializado
através do lifespan; sem isso, tool calls falham com "Task group is not initialized".
O path="/" no sub-app + mount("/mcp") resulta em rotas efetivas em `/mcp` (sem duplicar
o prefixo).
**Data:** 2026-08-27

## D009 — Acesso ao AppContext e user_id nas tools via ctx.fastmcp + request_context
**Decisão:** O `AppContext` é anexado à instância `FastMCP` como `_app_context`; as tools
acessam via `ctx.fastmcp._app_context`. O `user_id` é extraído dos headers da requisição
Starlette original via `ctx.request_context.request.headers.get("X-User-Id")` e validado
com `identity.validate_user_id()`.
**Motivo:** No modo stateless HTTP, o `Context` do FastMCP não expõe `request` diretamente,
mas `request_context.request` é o `starlette.requests.Request` original (com headers).
O middleware do FastAPI pai valida o header, mas o estado não é herdado pelo sub-app;
acessar headers diretamente garante fail-closed consistente.
**Data:** 2026-08-27

## D010 — storage.resolve() rejeita ".." em qualquer segmento (incluindo "a/../b")
**Decisão:** A validação em `resolve()` rejeita qualquer ocorrência de `".."` como
segmento de caminho, mesmo em formas como `a/../b`. Apenas segmentos individuais
válidos são permitidos.
**Motivo:** Briefing §4.2: "Rejeitar `..` em qualquer segmento (`docs/../../x`,
`a/../b` ok? NÃO — rejeitar qualquer `..`)". Implementado via split e checagem
`segment == ".."` antes do join.
**Data:** 2026-08-27

## D011 — Umask aplicado como 0o666 & ~umask (não 0o644 & ~umask)
**Decisão:** Arquivos criados recebem `os.chmod(path, 0o666 & ~umask)` (padrão 0o664
com umask 002). Diretórios usam `0o777 & ~umask` (0o775).
**Motivo:** `path.write_bytes()` cria com modo 0o666 menos umask do processo; aplicar
`os.chmod` pós-escrita com base 0o666 garante group-writable (0o664) conforme
configuração `UMASK=002` do container.
**Data:** 2026-08-27

## D012 — Pattern filter em storage.list() aplica-se apenas a arquivos, não a diretórios durante recursão
**Decisão:** Em `storage.list(recursive=True, pattern="*.md")`, o filtro `fnmatch`
é aplicado apenas a itens não-diretórios; diretórios são sempre percorridos para
não perder matches aninhados.
**Motivo:** Caso contrário, um diretório chamado `docs` seria filtrado por `*.md`
e seus filhos (`docs/b.md`) nunca seriam visitados.
**Data:** 2026-08-27

## D013 — Canary phrase extraída da primeira linha não-heading, não vazia, sem controles
**Decisão:** `rag_client._canary_phrase()` percorre linhas do conteúdo, pula vazias
e headings (`# `), limpa caracteres de controle (< 0x20), retorna prefixo de 80 chars.
Se não houver frase extraível, assume sucesso (confia no embed).
**Motivo:** Briefing §4.4 + issue #305: canário determinístico para verificar indexação
real no rag_api. Primeira linha não-heading é estável e representativa.
**Data:** 2026-08-27

## D014 — Public endpoint bypassa IdentityMiddleware
**Decisão:** `IdentityMiddleware` faz bypass para `/healthz` e `/files*` (path
startswith). O endpoint `/files/{token}` valida o token no registry e checa
`user_id` do header contra o `user_id` do link (defesa em profundidade).
**Motivo:** Links públicos devem ser acessíveis sem `X-User-Id` do agente; o token
opaco é a credencial. O header `X-User-Id` no download é opcional e serve apenas
como defesa extra contra drift do registry.
**Data:** 2026-08-27

## D015 — MCP em `/mcp` sem redirect de trailing slash
**Decisão:** Montar o sub-app MCP na raiz `app.mount("/", mcp_app)` com
`mcp.http_app(path="/mcp")`, registrando `/healthz` e `/files/{token}` ANTES do
mount. Adicionar handler explícito 405 para métodos não-GET/HEAD em `/files`.
**Motivo:** `app.mount("/mcp", mcp_app)` com `http_app(path="/")` fazia o Starlette
emitir 307 de `/mcp` para `/mcp/`. O cliente streamable-http do LibreChat aponta
para `/mcp` (sem barra) e pode não seguir o 307 de forma confiável. Com o mount na
raiz + path interno `/mcp`, a rota resolve direto, sem redirect. O catch-all do
mount exige handler 405 explícito em `/files` para não virar 404.
**Data:** 2026-08-27

## D016 — Diretórios de storage criados no Dockerfile (runtime)
**Decisão:** Criar `/data/private`, `/data/share`, `/data/private/_system` na imagem
com `chown 2000:2500`, antes de `USER mcp`.
**Motivo:** O container roda como non-root (UID 2000). Sem os diretórios pré-criados
na imagem, o startup falhava com `PermissionError: /data` quando o bind do host não
estava montado ainda (ex.: primeiro boot antes do host preparation). Serve como
fallback; em produção os binds do host sobrepõem.
**Data:** 2026-08-27

## D017 — Instalação não-editável no builder do Dockerfile
**Decisão:** No stage builder, `pip install ".[dev]"` (não-editável) em vez de
`pip install -e`.
**Motivo:** A instalação editável gera `.pth` apontando para `/build/src`, que não
existe no stage runtime (que só copia `/opt/venv`). Resultava em
`ModuleNotFoundError: personal_files_mcp` no container. A instalação regular coloca
o pacote em `site-packages`, que é copiado junto.
**Data:** 2026-08-27

## D018 — Nginx: proxy /files/ + /share/ legado + /_protected/ + o+x no root
**Decisão:** Trocar o `location /files/` legado (que servia estáticos de
`/data/share`) por `proxy_pass http://librechat-personal-files:8080/files/`;
mover os estáticos legados para `location /share/` (alias `/data/share/`);
adicionar `location /_protected/` (`internal` + `alias /data/private/`) para o
X-Accel-Redirect; adicionar `limit_req_zone` + `limit_req`; e aplicar
`chmod 2771` (o+x) em `./data/private` no host.
**Motivo:** O worker do nginx (alpine) roda como UID 101, fora do grupo `lcfiles`
(2500). O `chmod 2770` original (`drwxrws---`) impedia a travessia do diretório
raiz, causando `Permission denied` ao servir o arquivo via X-Accel. O `o+x`
permite travessia sem permitir listagem (que exigiria `o+r`); o acesso continua
gated pelo token no MCP + `_protected internal`. A migração dos legados foi
decidida com o usuário (opção "Manter em /share/ separado").
**Data:** 2026-08-27

## D019 — Content-Type do link público vem do registro
**Decisão:** `publishing._serve` envia `Content-Type` = `record.mime_type` no
response de X-Accel-Redirect.
**Motivo:** Sem isso, o nginx usava o default `application/octet-stream`. O MIME
correto (`text/markdown` etc.) já é persistido no registry em `create_link`.
**Data:** 2026-08-27

## D020 — Adaptação do rag_client à API real do rag_api (dev/latest)
**Decisão:** O cliente foi reescrito para a API real do
`ghcr.io/danny-avila/librechat-rag-api-dev` (divergente do briefing §4.4, que
descrevia payloads de uma versão antiga):
  - `/embed`: **multipart/form-data** (`file_id`, `file` binário, `entity_id` opcional),
    não JSON `{file_id, filename, contents}`.
  - `/query`: exige `file_id` (busca em UM arquivo), owner-scoped via metadata `user_id`.
  - `/query_multiple`: busca em vários `file_ids`; **sem** checagem de owner — o
    chamador deve passar apenas os file_ids do próprio usuário.
  - `DELETE /documents`: corpo é **array** de ids (`["id"]`), não `{file_id: ...}`.
  - Resposta de /query e /query_multiple: `[[{page_content, metadata:{file_id,user_id}}, distance], ...]`.
**Impacto nas tools:**
  - `search_knowledge` usa `/query_multiple` com os `rag_file_id` do docindex do usuário.
  - `index_document` envia multipart (basename como filename) + canário via `/query`.
  - `remove_from_knowledge` envia array no DELETE.
  - Ownership: JWT `id`/`sub` = userId; o rag_api grava metadata `user_id` no embed
    e compara em `/query` (isolamento cross-user preservado).
**Data:** 2026-08-27

## D021 — Cliente HTTP RAG por operação (event loop)
**Decisão:** `tools._run_rag` roda cada operação RAG em UM `asyncio.run` com um
único `RagClient` criado e fechado no mesmo event loop (via `coro_factory`).
**Motivo:** `httpx.AsyncClient` é ligado ao event loop onde foi criado. Reutilizar
o mesmo cliente entre múltiplos `asyncio.run()` causava
`RuntimeError: Event loop is closed` no `index_document` (embed + canary em loops
separados). O `_run_rag` evita o compartilhamento cross-loop.
**Data:** 2026-08-27

## D022 — Canário: remover marcadores markdown antes do match
**Decisão:** `rag_client._clean_for_canary` agora aplica `_strip_markdown` (remove
`**bold**`, `*italic*`, `\`code\``, headings `#`, links `[x](y)`) na frase-canário
extraída.
**Motivo:** O loader do rag_api normaliza o texto (ex.: remove `**` de negrito e
junta linhas com trailing spaces). O canário usava a frase crua (com `**`) e fazia
substring match exato, então `**Data do registro:**` nunca casava com
`Data do registro:` no chunk → o canário falhava 3x e o `index_document` deletava
os chunks no cleanup (falso `indexing_failed`). Stripar markdown alinha a frase
com o output do loader. Sintoma reportado: `rag_canary_failed ... após 3 tentativas`
em documentos com formatação markdown (negrito/heading).
**Data:** 2026-08-27
