# Links públicos do Personal Files MCP — solução com Dokploy e Traefik

## Resultado confirmado

O Personal Files MCP gerou um link no domínio correto, e Martin confirmou que o link abriu e funcionou.

Não foi necessário manter o Nginx.

A confirmação cobre geração e abertura do link de teste. Downloads parciais, HEAD, expiração e revogação ainda não foram testados nesta sessão.

## 1. Problemas encontrados

### Endereço-base antigo

A ferramenta gerava links para:

```text
https://old-chat.example.com/files/
```

O LibreChat atual utiliza:

```text
https://chat.example.com
```

A variável foi corrigida no Dokploy para:

```env
PUBLIC_BASE_URL=https://chat.example.com/files
```

O teste final confirmou que a ferramenta passou a gerar URLs nesse endereço.

### Dependência do Nginx

O código antigo de `publishing.py` validava o token, mas retornava:

```python
headers = {
    "X-Accel-Redirect": accel,
    "Content-Disposition": attachment_header,
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
}

return Response(status_code=200, content=b"", headers=headers)
```

O corpo da resposta era vazio. O arquivo real era entregue pelo Nginx através de uma rota interna:

```nginx
location ^~ /_protected/ {
    internal;
    alias /data/private/;
}
```

Portanto, encaminhar `/files/` pelo Traefik não bastava para entregar o arquivo enquanto o MCP dependesse de `X-Accel-Redirect`.

## 2. Mudança no MCP

Martin decidiu não manter o Nginx.

O agente programador informou que alterou `src/personal_files_mcp/publishing.py` para entregar o arquivo diretamente usando `FileResponse` do Starlette/FastAPI.

A mudança informada mantém a validação do token e do caminho e elimina a dependência de:

- `X-Accel-Redirect`;
- rota interna `/_protected/`;
- montagem do volume privado em um servidor Nginx.

O código final não foi revisado integralmente nesta conversa. O teste funcional posterior confirmou que o arquivo passou a ser entregue pelo endereço público.

O suporte a Range/HTTP 206 foi informado pelo agente, mas ainda não foi verificado aqui.

## 3. Imagem implantada

O agente forneceu a imagem de teste:

```text
ghcr.io/martinriesel/librechat-personal-files-mcp:1.1.0-test
```

O container em execução foi confirmado na VPS:

```text
<compose-project>-librechat-personal-files-1
```

A saída de `docker ps` confirmou essa imagem em execução.

## 4. Correção do Compose

### Contexto de build inválido

Foi usada inicialmente esta configuração:

```yaml
build:
  context: https://github.com/martinriesel/librechat-personal-files-mcp:1.1.0-test
```

Ela misturava uma URL de repositório GitHub com sintaxe de tag de imagem Docker.

O Deploy falhava ao carregar o contexto remoto:

```text
invalid response status 404
failed to stat policy file Dockerfile.rego
```

A correção orientada foi substituir o bloco `build` do Personal Files pela imagem publicada:

```yaml
services:
  librechat-personal-files:
    image: ghcr.io/martinriesel/librechat-personal-files-mcp:1.1.0-test
    restart: unless-stopped
    user: "2000:2500"
```

Não era necessário criar `Dockerfile.rego`.

O serviço Office tinha build separado e não precisava dessa substituição.

### Conflito de nome fixo

A configuração incluía:

```yaml
container_name: librechat-personal-files
```

Esse nome já pertencia a um container antigo em execução:

```text
Nome: /librechat-personal-files
Projeto Compose: librechat
Imagem: librechat-librechat-personal-files
```

Foi orientado remover o `container_name` fixo do Compose atual, permitindo o nome gerenciado pelo projeto Dokploy.

A instância antiga foi preservada, mas parada e impedida de reiniciar automaticamente:

```bash
docker update --restart=no librechat-personal-files
docker stop librechat-personal-files
```

A saída posterior mostrou somente a instância do Dokploy em execução.

Não foram excluídos os dados antigos nem removidos volumes.

## 5. Dados preservados no volume migrado

O serviço atual utiliza os volumes externos existentes:

```yaml
volumes:
  personal_private:
    external: true
    name: <existing-private-volume>

  personal_shared:
    external: true
    name: <existing-share-volume>
```

Mounts do serviço:

```yaml
volumes:
  - personal_private:/data/private
  - personal_shared:/data/share
```

Não substituir esses volumes por diretórios relativos vazios como `./data/private`.

A instância antiga utilizava bind mounts diferentes:

```text
/opt/LibreChat/data/private → /data/private
/opt/LibreChat/data/share → /data/share
```

Essas fontes antigas foram preservadas.

## 6. Encaminhamento no Dokploy/Traefik

A configuração orientada na aba Domains do aplicativo Compose foi:

| Campo | Valor |
|---|---|
| Host | `chat.example.com` |
| Serviço | `librechat-personal-files` |
| Porta interna | `8080` |
| Path | `/files/` |
| HTTPS | Ativado |
| Strip Path | Desativado |
| Internal Path | Vazio |

A rota geral `/` do domínio deve continuar apontando para o LibreChat.

A rota específica deve encaminhar:

```text
/files/TOKEN
```

sem remover o prefixo.

O serviço precisa estar acessível ao Traefik pela rede usada pelo Dokploy, mantendo as demais redes internas necessárias.

Não é necessário:

- montar os arquivos privados no Traefik;
- publicar a porta 8080 diretamente no host;
- publicar `/mcp` pela rota de downloads;
- expor `/data/private` ou `/_protected/`.

Não foi fornecido um dump final das labels e redes nesta etapa. Portanto, os campos acima registram a configuração orientada; o teste público confirmou o funcionamento do caminho resultante.

## 7. Fluxo final

```text
Navegador
    ↓ HTTPS
Traefik
    ↓ /files/TOKEN
Personal Files MCP
    ↓ valida token, estado e caminho
Entrega direta do arquivo
    ↓
Navegador recebe o conteúdo
```

A validação e entrega do arquivo ficam no MCP.

O Traefik atua como proxy HTTP, sem precisar acessar o volume privado.

## 8. Teste realizado

Arquivo utilizado:

```text
notas/teste-link-migracao.txt
```

Conteúdo fictício, sem dados pessoais ou credenciais.

A ferramenta `publish_file` retornou uma URL no formato correto:

```text
https://chat.example.com/files/<token>
```

O link foi criado com validade de um dia.

Martin abriu o link e confirmou:

```text
O Link funcionou.
```

Não é necessário registrar o token temporário neste documento.

## 9. Validações pendentes

- HEAD sem corpo e com cabeçalhos adequados.
- Downloads parciais e retomada, caso suportados.
- Arquivos binários e grandes.
- Nome de arquivo com caracteres especiais.
- Token inexistente, expirado e revogado.
- Arquivo removido ou sem permissão.
- Segurança contra troca por link simbólico entre validação e abertura.
- Limitação de requisições no Traefik.
- Persistência após novo Deploy.
- Revisão integral do patch e dos testes automatizados.

O limite de requisições que existia no Nginx não foi confirmado como reimplementado no Traefik.

## 10. Separação da função de projetos

A mesma imagem também incluiu alterações para contexto de projetos, conforme relato do agente programador.

Isso não foi necessário para corrigir a entrega dos links.

A ferramenta `get_project_context` não estava disponível neste chat no momento do teste. Portanto, a descoberta do projeto atual permanece uma validação separada.

## 11. Regras para futuras alterações

- Usar imagem/versionamento explícitos para repetir o teste.
- Preservar os volumes externos e o registry de links.
- Não reativar a instância antiga sem avaliar conflito e fontes de dados.
- Não considerar HTTP 200 sozinho como prova de download: verificar conteúdo.
- Manter publicação como ação explícita.
- Não expor caminhos privados diretamente.
- Não registrar credenciais ou tokens públicos temporários em documentação.
