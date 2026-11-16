# Configuração do OneDrive para Personal Files MCP

Este guia prepara a integração OneDrive para a instalação LibreChat. A integração fica desativada por padrão; as ferramentas OneDrive só aparecem quando `STORAGE_PROVIDERS` inclui `onedrive` e as configurações Azure estão completas.

## 1. Registrar o aplicativo no Microsoft Entra ID

No portal Azure, em **Microsoft Entra ID → App registrations → New registration**:

1. Registre como aplicativo **single tenant** para o diretório corporativo.
2. Em **Authentication**, adicione uma plataforma **Web** e a URI de callback que será configurada em `AZURE_REDIRECT_URI`, por exemplo:
   `https://gpt.example.com/auth/azure/callback`
3. Em **API permissions**, adicione permissões **delegated**:
   - Microsoft Graph `Files.ReadWrite.AppFolder`
   - `User.Read` (apenas para identificar a conta conectada)
   - `offline_access` (para renovação de tokens)
4. A política de consentimento organizacional deve permitir estas permissões. Não conceda `Files.ReadWrite`, `Files.ReadWrite.All` ou permissões de aplicação.
5. Crie um client secret em **Certificates & secrets** e guarde o valor no gerenciador de segredos. O valor só é exibido uma vez.

O AppFolder é acessado pelo endpoint Graph `/me/drive/special/approot`. Nenhuma ferramenta deve operar fora desse root.

## 2. Gerar e guardar a chave de cifragem

`ONEDRIVE_TOKEN_ENCRYPTION_KEY` deve ser uma chave Fernet gerada em base64 URL-safe. Ela precisa ser independente de `JWT_SECRET`; não reutilize client secret nem qualquer senha de usuário.

Gere uma chave localmente com o ambiente Python do projeto:

```bash
cd /opt/LibreChat/mcp-personal-files
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Armazene o resultado no `.env` de produção/secret manager. Se essa chave for perdida, as credenciais OneDrive cifradas já salvas não poderão ser lidas e os usuários precisarão autorizar novamente. Para rotacionar, implemente uma migração que leia com a chave anterior e regrave com a nova; não substitua a chave diretamente enquanto houver tokens existentes.

## 3. Variáveis do serviço Docker

Acrescente ao serviço `librechat-personal-files` no Compose, mantendo `STORAGE_PROVIDERS=local` até o registro Azure estar pronto:

```yaml
environment:
  STORAGE_PROVIDERS: ${STORAGE_PROVIDERS:-local}
  AZURE_TENANT_ID: ${AZURE_TENANT_ID:-}
  AZURE_CLIENT_ID: ${AZURE_CLIENT_ID:-}
  AZURE_CLIENT_SECRET: ${AZURE_CLIENT_SECRET:-}
  AZURE_SCOPES: ${AZURE_SCOPES:-Files.ReadWrite.AppFolder User.Read offline_access}
  AZURE_REDIRECT_URI: ${AZURE_REDIRECT_URI:-}
  ONEDRIVE_TOKEN_ENCRYPTION_KEY: ${ONEDRIVE_TOKEN_ENCRYPTION_KEY:-}
  ONEDRIVE_MAX_FILE_SIZE_MB: ${ONEDRIVE_MAX_FILE_SIZE_MB:-100}
  ONEDRIVE_VIEWER_BASE_URL: ${ONEDRIVE_VIEWER_BASE_URL:-}
```

Configure no `.env`:

```dotenv
STORAGE_PROVIDERS=local,onedrive
AZURE_TENANT_ID=<tenant-guid>
AZURE_CLIENT_ID=<app-client-id>
AZURE_CLIENT_SECRET=<secret armazenado com segurança>
AZURE_SCOPES=Files.ReadWrite.AppFolder User.Read offline_access
AZURE_REDIRECT_URI=https://gpt.example.com/auth/azure/callback
ONEDRIVE_TOKEN_ENCRYPTION_KEY=<chave Fernet independente>
ONEDRIVE_MAX_FILE_SIZE_MB=100
ONEDRIVE_VIEWER_BASE_URL=https://gpt.example.com/onedrive/view
```

Requisitos de validação: redirect URI HTTPS absoluto, escopos contendo `Files.ReadWrite.AppFolder` e `offline_access`, chave Fernet válida e diferente de `JWT_SECRET`, limite positivo. `ONEDRIVE_VIEWER_BASE_URL` é HTTPS e representa o prefixo público da rota de visualização.

O serviço já monta `./data/private:/data/private`; OAuth transactions e registros auxiliares são mantidos em `/data/private/_system`, e cada usuário tem seu arquivo cifrado dentro da própria raiz.

## 4. Rotas Nginx

No server block HTTPS que atende o LibreChat, adicione os proxies antes de regras genéricas. Preserve os headers forwarded para o FastAPI:

```nginx
location ^~ /auth/azure/ {
    proxy_pass http://librechat-personal-files:8080/auth/azure/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location ^~ /onedrive/view/ {
    proxy_pass http://librechat-personal-files:8080/onedrive/view/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Only forward an identity header set/validated by the trusted LibreChat proxy.
    proxy_set_header X-User-Id $http_x_user_id;
}
```

**Identidade do viewer:** a rota requer `X-User-Id` validado e token de viewer curto vinculado ao mesmo usuário/arquivo. O header só pode ser encaminhado quando for inserido/validado por uma camada confiável do LibreChat/reverse proxy; não confie em um header arbitrário enviado por clientes externos. Se a implantação não puder garantir essa identidade para navegação do browser, mantenha o gateway de visualização desabilitado até haver autenticação corporativa de browser apropriada. O token sozinho não transforma o arquivo em público.

O callback não requer `X-User-Id`: o state assinado e transação single-use associam o callback ao usuário. `onedrive_login` retorna a URL Microsoft de autorização e o state. O usuário abre a URL Microsoft no Edge autenticado; a URI registrada `/auth/azure/callback` recebe code/state e o servidor valida/consome a transação antes de guardar tokens. A rota `/auth/azure/login` pode ser usada como entry point quando o browser consegue apresentar `X-User-Id` confiável; nunca desative a validação de identidade para contornar um proxy que não propaga identidade.

## 5. Habilitar e validar

1. Confirme `STORAGE_PROVIDERS=local,onedrive` e todas as variáveis requeridas.
2. Valide o Compose sem iniciar serviços com `docker compose config`.
3. Faça build/recrie o serviço MCP e atualize/recarregue Nginx seguindo o procedimento operacional do ambiente.
4. Verifique `/healthz`; configuração Azure inválida deve impedir o servidor de anunciar tools OneDrive.
5. Em um chat com o MCP, confirme `onedrive_login`, abra a URL retornada no Edge corporativo e conclua consentimento.
6. Confirme `onedrive_auth_status`, depois valide `onedrive_write_file`, `onedrive_read_file`, `onedrive_list_files` e `onedrive_get_file_info` dentro do AppFolder.
7. Teste um upload de mais de 4 MiB e um arquivo acima do limite configurado (o segundo deve ser recusado antes do envio).
8. Verifique organização: `onedrive_publish_file` deve retornar link com scope `organization`; não deve existir fallback para `anonymous`.
9. Teste o gateway HTML autenticado com token válido, identidade correspondente e expiração/reuso; teste sem `X-User-Id` e com identidade de outro usuário.
10. Teste logout e confirme que `onedrive_auth_status` retorna desconectado.

## 6. Desativar/rollback

Para ocultar todas as ferramentas OneDrive sem afetar o armazenamento local, defina `STORAGE_PROVIDERS=local` e recrie somente o serviço MCP. Isso não apaga os documentos do AppFolder. Tokens locais cifrados podem ser removidos pelo fluxo `onedrive_logout`; não apague o diretório de dados inteiro para desativar o provedor.

## Variáveis implementadas

| Variável | Obrigatória | Descrição |
|---|---:|---|
| `STORAGE_PROVIDERS` | Não | Lista separada por vírgulas; default `local`; opções `local`, `onedrive`. |
| `AZURE_TENANT_ID` | Sim quando habilitado | ID do tenant Entra single-tenant. |
| `AZURE_CLIENT_ID` | Sim quando habilitado | ID da aplicação registrada. |
| `AZURE_CLIENT_SECRET` | Sim quando habilitado | Secret da aplicação confidencial usada no code exchange. |
| `AZURE_SCOPES` | Sim quando habilitado | Deve conter `Files.ReadWrite.AppFolder` e `offline_access`; default inclui `User.Read`. |
| `AZURE_REDIRECT_URI` | Sim quando habilitado | Callback HTTPS registrado no Entra. |
| `ONEDRIVE_TOKEN_ENCRYPTION_KEY` | Sim quando habilitado | Chave Fernet base64 independente do `JWT_SECRET`. |
| `ONEDRIVE_MAX_FILE_SIZE_MB` | Sim quando habilitado | Tamanho máximo por arquivo; default `100`. |
| `ONEDRIVE_VIEWER_BASE_URL` | Sim quando habilitado | Prefixo HTTPS para links temporários do gateway HTML autenticado. |
