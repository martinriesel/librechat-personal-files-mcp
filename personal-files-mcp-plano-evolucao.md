# Personal Files MCP — plano de evolução e continuidade

Data: 13/11/2026. Responsável: Martin Riesel.
Repositório informado: https://github.com/martinriesel/librechat-personal-files-mcp

## 1. Objetivo e decisões

Martin desenvolveu o MCP e ele está em fase de testes. Quer resolver duas frentes na mesma evolução:

1. Downloads públicos funcionando diretamente atrás do Traefik/Dokploy, sem manter Nginx.
2. Continuidade entre chats de um projeto LibreChat: identificar o projeto atual e usar seus documentos, resumo, catálogo e busca automaticamente.

Não copiar automaticamente todo o histórico das conversas. Preservar decisões e estado útil em documentos explícitos e permitir recuperação nos chats seguintes.

Este documento é um plano, não confirmação de implementação. Nenhum código do repositório foi alterado nesta conversa. A versão atual do GitHub não foi efetivamente lida; o código instalado enviado pelo usuário foi usado como evidência.

## 2. Referências existentes

- docs/gifted-boyd-dokploy-referencia.md: referência operacional principal da VPS, redes, volumes, correções e indexação bem-sucedida.
- docs/documentacao-librechat-personal-files-mcp.md: documentação histórica da arquitetura, módulos, publicação e instalação com Nginx. Não representa integralmente o estado atual verificado.

Não duplicar a documentação de migração nem tratar instruções antigas de instalação como tarefas atuais.

## 3. Ambiente e fatos confirmados

- Personal Files: container rieselgpt-mcp-servers-ybdax9-librechat-personal-files-1; processo UID 2000/GID 2500.
- Volume privado: rieselgpt-librechat-mlhfos_librechat_private; conteúdo observado em /var/lib/docker/volumes/rieselgpt-librechat-mlhfos_librechat_private/_data.
- Estrutura: /data/private/<userId>/ no MCP; registry em /data/private/_system/links.db, conforme documentação.
- LibreChat: rieselgpt-librechat-mlhfos-librechat-1; imagem observada ghcr.io/danny-avila/librechat-dev:latest.
- RAG: rieselgpt-librechat-mlhfos-rag_api-1.
- LiteLLM: rieselgpt-litellm-5grzc3-litellm-1; imagem observada ghcr.io/berriai/litellm:main-latest. Banco separado postgres:16-alpine.
- Personal Files e RAG compartilham rieselgpt-backend; associação RAG e DNS rag_api foram confirmados após Deploy.
- Após conectar LiteLLM à rede compartilhada e fazer Deploy, RAG resolveu litellm. Inspect final do LiteLLM após mudança não foi fornecido.
- /app/app/config.py do RAG confirmou RAG_OPENAI_BASEURL e RAG_OPENAI_API_KEY no ramo OpenAIEmbeddings.
- RAG_OPENAI_BASEURL=http://litellm:4000/v1 foi confirmado no ambiente do RAG. Orientação: EMBEDDINGS_PROVIDER=openai, EMBEDDINGS_MODEL=azure_ai/text-embedding-3-small, desde que corresponda ao nome publicado no proxy. Provedor/modelo finais não foram reenviados.
- Indexação de projects/Hallenplane_Alphaplan/documentacao-investigacao-sql.md retornou indexed:true, rag_file_id ae4d15d2-5cfa-4c33-8294-b424cb96a11d, com confirmação pela ferramenta.

Credenciais não fazem parte deste documento. Manter chaves nos locais protegidos administrados pelo usuário.

## 4. Permissões e isolamento: estado atual

A pasta projects era root:2500, modo 2755, sem escrita para UID 2000. Uma varredura também encontrou arquivos sem escrita em outras áreas do volume. Foi fornecido procedimento de ACL para todo o conteúdo privado, sem backup a pedido explícito do usuário, preservando donos e sem seguir links simbólicos:

- Diretórios: u:2000:rwx e default u:2000:rwx.
- Arquivos regulares: u:2000:rwX.
- find -P com -xdev, volume e mount previamente validados.

Não foi enviada saída integral da aplicação nem contagem final. Teste direto pelas ferramentas confirmou criação, escrita e exclusão de arquivo temporário em projects/Hallenplane_Alphaplan; pasta confirmada na listagem.

Testes básicos autorizados pelo desenvolvedor:

- Raiz relativa: somente entradas associadas ao usuário atual.
- Listar ..: invalid_path, path traversal '..' rejected.
- Listar /data/private: invalid_path, absolute path rejected.

Não foi lido conteúdo de outros usuários. Não houve auditoria completa. Todos os usuários compartilham o UID do serviço, portanto o isolamento depende da autenticação e do código, não de ACLs por usuário.

Pendências de segurança: origem e confiança em X-User-Id, links simbólicos, concorrência entre validação e abertura, mover/publicar, busca por usuário/projeto. Preferir contas de teste e arquivos sentinela fictícios.

## 5. Frente A — downloads diretos sem Nginx

### 5.1 Comportamento atual confirmado

Módulo instalado: /opt/venv/lib/python3.12/site-packages/personal_files_mcp/publishing.py.

O código fornecido mostrou:

- handle_get e handle_head chamam _serve.
- Registry valida existência do token, revogação e expiração.
- storage.resolve valida novamente o caminho.
- registry.touch atualiza acesso.
- Retorno inclui X-Accel-Redirect para /_protected/<userId>/<relative_path>.
- Content-Disposition attachment, Content-Type quando disponível, nosniff e Cache-Control private,no-store.
- HTTP 200 com corpo vazio, inclusive para GET.

Portanto, o endpoint atual não entrega os bytes; dependia de Nginx com location internal e alias. Criar uma rota Traefik não muda esse comportamento.

### 5.2 Mudança proposta

Preferência: entrega direta pelo MCP, independente de proxy. Avaliar FileResponse da versão instalada de Starlette/FastAPI. StreamingResponse com descritor seguro é alternativa quando necessário para proteção contra troca de caminhos.

Antes do patch, ler arquivos completos e versões reais:

- publishing.py;
- storage.py, especialmente resolve e regras de caminhos;
- app.py, montagem de rotas GET/HEAD;
- config.py e dependências;
- tests/test_publishing.py e testes de storage/app.

Não assumir tipo de retorno de storage.resolve nem suporte de parâmetros sem verificar.

Requisitos:

1. Manter validação de token, expiração, revogação, existência e isolamento do dono.
2. Resolver caminho através do ponto central de segurança; confirmar arquivo regular.
3. Entregar bytes sem carregar o arquivo inteiro em memória.
4. Remover X-Accel-Redirect e dependência de /_protected/.
5. Preservar download como attachment, nome seguro, MIME, nosniff e no-store.
6. HEAD sem corpo, com cabeçalhos apropriados.
7. Decidir e testar suporte a Range/download parcial na versão instalada.
8. Tratar erros de arquivo ausente/permissão sem revelar caminho interno ou traceback.
9. Rever janela TOCTOU: validar caminho e abrir depois pode permitir substituição por symlink. Documentar abordagem e risco residual; considerar abertura segura com descritor, sem bloquear o processamento assíncrono.
10. Manter tokens existentes e registry; não regenerar nem publicar documentos privados por rotina.

Download público não exige X-User-Id: o token é a autorização. Ferramentas de criar/revogar links continuam autenticadas e restritas ao dono.

### 5.3 Traefik/Dokploy após patch

PUBLIC_BASE_URL informado pelo usuário após correção: https://gpt.riesel.com.br/files. Valor efetivo após Deploy ainda não foi reenviado.

No app Compose MCP, usar Domains do Dokploy:

- Host gpt.riesel.com.br.
- Serviço librechat-personal-files.
- Porta interna 8080.
- Path /files/.
- HTTPS ativo.
- Strip Path desativado, Internal Path vazio.
- Manter rota geral / do LibreChat.

Verificar labels geradas e precedência da rota específica. Conectar serviço ao dokploy-network para acesso do proxy, mantendo redes internas e rieselgpt-backend. Não publicar 8080 no host; não montar volume privado no Traefik. Não expor /mcp, /data/private ou /_protected/ através dessa regra.

Para Compose, mudanças de domínio exigem Deploy. Não editar traefik.yml para roteamento normal; ele contém configuração de inicialização. Domains gera labels. Se precisar de ajuste manual, inspecionar labels existentes antes de adicionar para evitar routers duplicados.

Rate limit antigo do Nginx: rate=5r/s, burst=10, resposta 429. Reimplementar política deliberadamente no Traefik, validando IP real e proxies confiáveis; não assumir equivalência exata entre algoritmos nem confiar em cabeçalhos arbitrários do cliente.

### 5.4 Teste público criado

Arquivo notas/teste-link-migracao.txt com texto fictício, sem dados pessoais. Publicação teve sucesso, mas URL retornada apontava ao domínio antigo gpt.loca.riesel.com.br. Link recebeu TTL de um dia, expiração 14/11/2026 16:09:45 UTC. Token não é necessário para continuidade; recuperar status pelas ferramentas quando for retomar. Acesso HTTP ao link não foi verificado. Após novo Deploy, testar URL gerada e conteúdo real. Revogar link de teste e remover arquivo quando não forem mais necessários, com autorização apropriada.

### 5.5 Critérios de aceite

- GET retorna conteúdo idêntico ao original, não apenas HTTP 200.
- HEAD não retorna corpo.
- Texto, binário, arquivo vazio, arquivo grande e nome não ASCII.
- Token inexistente, revogado, expirado.
- Arquivo removido, sem leitura, diretório e symlink escapando do root.
- Concorrência/revogação e consumo de memória.
- Range, se suportado.
- Sem vazamento de userId/caminho em cabeçalhos, erros ou redirects.
- /_protected/ não acessível; /mcp não exposto pela rota pública.
- Persistência dos tokens e funcionamento após Deploy.

## 6. Frente B — contexto e memória por projeto

### 6.1 Necessidade

Projetos devem permitir continuidade entre chats sem exigir que o usuário repita o contexto. MCP já salva Markdown, atualiza catálogo e indexa RAG, mas não recebe atualmente identificação verificada do projeto ativo.

No prompt do agente, o usuário colocou:

```text
ProjectID: {{LIBRECHAT_BODY_CONVERSATIONID}}
```

O assistente recebeu literalmente o placeholder, sem substituição. Além disso, conversationId é ID da conversa, não do projeto; mudar o rótulo não muda o significado. Usuário e data foram preenchidos, mas nenhum ID/nome de projeto foi recebido.

Não assumir que placeholders de cabeçalhos MCP funcionam em system prompt. Não usar o nome da pasta nem o modelo como autoridade para determinar projeto.

### 6.2 Pesquisa encontrada

Fontes consultadas na web:

- https://www.metabase.com/blog/librechat-self-hosted — relato de base compartilhada sobre RAG API exposta por MCP próprio. Precedente para memória MCP/RAG; não comprova identificação automática de projeto.
- https://github.com/LibreChat-AI/LibreChat/issues/13495 — discussão Projects: shared context & workspace customization, incluindo conhecimento/memória por projeto e chatProjectId. Não comprova implementação na imagem do usuário.
- https://www.librechat.ai/docs/features/projects — documentação atual descreve instruções e arquivos de referência compartilhados entre chats, não todo o histórico compartilhado automaticamente. Verificar disponibilidade real na versão instalada.
- https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers — documenta LIBRECHAT_BODY_* e requestHeaders para contexto de requisição, exemplo X-Conversation-ID com LIBRECHAT_BODY_CONVERSATIONID. Contexto não deve ser reutilizado entre execuções.

### 6.3 Investigação antes de implementar

1. Confirmar versão/commit da imagem LibreChat, não apenas tag latest.
2. Ler implementação de templates e configuração MCP que realmente está em uso.
3. Confirmar suporte a requestHeaders no Admin Panel ou necessidade de definição de infraestrutura em YAML. Não migrar toda a configuração do usuário por suposição.
4. Inspecionar campos de metadados da requisição em chat comum e em dois projetos diferentes, sem capturar conteúdo ou credenciais.
5. Confirmar conversationId e se chatProjectId é enviado/resolvido; não inventar placeholder LIBRECHAT_BODY_CHATPROJECTID como fato suportado.
6. Confirmar novos chats, regeneração, chats movidos, execução por agente e duas abas simultâneas.

Se projectId vier diretamente, validá-lo e associá-lo à identidade autenticada. Se não vier, considerar resolução autorizada conversationId → projeto via integração restrita. Não conceder MongoDB irrestrito nem inventar endpoint de consulta.

Contexto deve ser por requisição, não um 'projeto ativo do usuário' global: duas abas podem executar projetos diferentes simultaneamente. Cache/conexões não podem vazar contexto entre chats.

### 6.4 Modelo de dados proposto

Escopos independentes:

- Usuário: preferências e documentos gerais.
- Projeto: resumo, decisões, procedimentos, arquivos e busca.
- Conversa: origem das mudanças e ligação ao projeto; não substitui projeto.

Usar ID estável do projeto como chave e nome somente para apresentação. Renomear não deve quebrar associação. Pasta existente projects/Hallenplane_Alphaplan não deve ser automaticamente considerada projeto LibreChat sem vínculo explícito.

Definir registry de projetos, associação userId/projectId/pasta, nomes e estado. Migração inicial deve permitir vincular pastas existentes sem duplicar arquivos ou índices. Compartilhamento entre usuários não está implicitamente autorizado; projetos permanecem no escopo do dono até requisitos/ACL explícitos.

Ausência de projeto deve ser um estado explícito (escopo pessoal), não fallback silencioso ao último projeto.

### 6.5 Ferramentas e catálogo propostos

Nomes abaixo são sugestões de desenvolvimento, não ferramentas já disponíveis:

- get_project_context: retorna identificação verificada, resumo principal, documentos e pendências relevantes.
- Ferramentas de salvar/atualizar devem registrar contexto e origem, mantendo compatibilidade com chamadas existentes.
- Busca com escopo de projeto como padrão quando houver contexto; expansão para escopo pessoal somente deliberada.
- Vincular pasta existente a projeto mediante confirmação, não adivinhar pela semântica.

Evoluir memory-index.json com versão de schema e metadados de projeto/conversa. Escrita atômica, locking e atualização consistente com criar/mover/deletar. Preservar documentação pessoal e entradas antigas; separar salvar de indexar e publicar.

Manter um resumo principal e documentos específicos. Não anexar o chat inteiro nem carregar todo o projeto no contexto de cada execução. Recuperar resumo e fontes relevantes de forma seletiva.

### 6.6 RAG por projeto

Isolamento por usuário deve continuar obrigatório. Adicionar filtro real por projeto ou conjunto autorizado de documentos antes de resultados chegarem ao modelo. Pastas separadas sozinhas não filtram RAG.

Investigar suporte real do rag_api a metadados/filtros/file IDs; evitar assumir endpoint/parâmetro inexistente. Se necessário, registry mantém IDs autorizados do projeto e consulta restrita a esse conjunto. Não fazer busca global e tratar pós-filtro frágil como isolamento suficiente.

Preservar idempotência e estado indexed/pending/failed; fazer verificação pós-indexação. Em erro, diferenciar falha de envio, processamento, autenticação, consulta e frase não encontrada. Evitar mascarar 401/500 como mero atraso.

Mover/renomear documento deve atualizar associação e índice sem duplicação. Remover documento ou vínculo de projeto deve ter semântica definida, sem apagar automaticamente o original. Proibir publicar por efeito colateral da indexação.

### 6.7 Recuperação automática versus instrução ao agente

Disponibilizar ferramenta não obriga o modelo a consultá-la. Definir como o contexto inicial será carregado:

- Instrução do agente para consultar get_project_context no início, quando disponível; ou
- Integração que injeta resumo verificado no começo da execução, se suportada e implementada.

Não prometer carregamento automático antes do teste. Contexto traz identificação e referências, não autorização para ler qualquer projeto.

### 6.8 Critérios de aceite

- Dois chats do mesmo projeto recuperam a mesma documentação/resumo.
- Projetos diferentes não misturam memória nem resultados de busca.
- Duas abas simultâneas não trocam contexto.
- Chat fora de projeto não herda projeto anterior.
- Renomear projeto mantém vínculo.
- Mover chat para dentro/fora atualiza contexto no próximo turno.
- Usuário diferente não acessa dados privados nem por ID manipulado.
- Placeholder ausente/literal resulta em ausência/erro controlado, nunca diretório com template.
- Cache, descoberta de tools e runs de agente não reutilizam cabeçalhos indevidos.
- Arquivos/catálogos antigos continuam funcionando.

## 7. Plano de implementação

1. Obter e ler código atual, testes, dependências e commit de produção.
2. Implementar entrega direta e testes de publicação em branch separada.
3. Implantar imagem de teste e rota Domains /files/ no Dokploy; validar download real e segurança.
4. Instrumentar somente metadados de contexto do LibreChat; testar dois projetos e chat comum.
5. Escolher passagem direta de projectId ou resolver associação por integração autorizada.
6. Implementar contexto por requisição e validação de dono.
7. Evoluir registry, catálogo e associação das pastas existentes.
8. Implementar consulta e indexação restritas por projeto com verificação.
9. Definir recuperação inicial pelo agente, com teste de continuidade.
10. Executar testes de concorrência, isolamento, regressão e persistência.
11. Atualizar README, configuração de exemplo, documentação, changelog e decisões técnicas; retirar alegação de Nginx obrigatório se entrega direta for implementada.
12. Atualizar referência operacional somente após confirmação dos resultados e registrar rollback por versão/imagem.

## 8. Próximo passo exato

Obter arquivos completos publishing.py, storage.py, app.py, config.py, dependências e testes do repositório atual. Em paralelo, verificar como a imagem LibreChat instalada resolve metadados de corpo e se disponibiliza chatProjectId ao MCP. Não modificar schema, apagar índices ou implementar placeholder de projeto por suposição.

Pedido para retomar em novo chat:

> Continue a evolução do meu Personal Files MCP usando personal-files-mcp-plano-evolucao.md e gifted-boyd-dokploy-referencia.md. Quero downloads diretos sem Nginx e memória por projeto. Primeiro confira o código atual e o contexto de projeto que o LibreChat realmente envia. Preserve usuários, arquivos, tokens e índices existentes.

Documento privado. Sem senhas, tokens de serviço, chaves ou conteúdo privado de outros usuários. Salvo como plano; indexação e publicação são ações separadas.
