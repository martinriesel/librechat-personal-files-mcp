# Personal Files MCP

This context describes the user's personal file stores and the identity and authorization boundaries around them.

## Storage and identity

**User**:
The LibreChat identity identified by the validated `X-User-Id` request header; storage and cloud credentials are scoped to this identity.
_Avoid_: account, tenant user (unless referring to the Microsoft Entra identity)

**Local Storage**:
The per-user filesystem rooted at `/data/private/<userId>` and served by the existing local tools.
_Avoid_: default provider (unless discussing configuration; local remains enabled by default)

**OneDrive AppFolder**:
The application-owned OneDrive folder addressed through Microsoft Graph's `approot`; the OneDrive tools may operate only within this boundary.
_Avoid_: user's OneDrive, whole drive

**Storage Provider**:
A configured backend whose tools are exposed by the MCP server. The default configuration exposes Local Storage only; OneDrive tools require explicit enablement and valid configuration.
_Avoid_: backend (when referring to which tool families are available)

## OneDrive authorization and knowledge

**OneDrive Connection**:
A user's delegated Microsoft authorization, represented by encrypted tokens associated with the validated LibreChat user identity.
_Avoid_: session (the MCP is stateless)

**OAuth Transaction**:
A short-lived, single-use authorization attempt bound to a user, PKCE challenge, and signed state, consumed by the callback.
_Avoid_: OAuth session

**Remote Document Index**:
The `memory-index.json` stored in the OneDrive AppFolder and authoritative for OneDrive document metadata and indexing status.
_Avoid_: local index (when referring to OneDrive documents)

**Knowledge Origin**:
The storage source associated with a RAG document, distinguishing local files from OneDrive AppFolder files.
_Avoid_: path alone (as a globally unique document identity)

**Organizational Sharing Link**:
A OneDrive-generated link explicitly created by a publish operation and scoped to the configured organization; it is distinct from authenticated gateway access.
_Avoid_: public link, anonymous link

**Authenticated Viewer**:
The OneDrive HTML gateway that serves a file only after validating a short-lived access token or equivalent user-bound authorization; viewing does not publish the file.
_Avoid_: public viewer
