# ARCHITECTURE

## Public shape

`codex-slot-relay` exposes an OpenAI-compatible HTTP surface while managing multiple Codex accounts as slots.

### API surface

- `GET /healthz`
- `GET /readyz`
- `GET /admin/slots`
- `GET /v1/models`
- `POST /admin/refresh-usage`
- `POST /v1/chat/completions`
- `POST /v1/responses`

## Backend split

Backends are explicit per subsystem:

- `auth.backend`
- `usage.backend`
- `runner.backend`

### Default (standalone-first)

- `auth.backend = native`
- `usage.backend = codex-api`
- `runner.backend = codex-direct`

### Legacy fallback (optional)

- `auth.backend = openclaw`
- `usage.backend = openclaw`
- `runner.backend = openclaw`

## Auth subsystem

### Native auth flow

`slot-login` native path:
1. Build OAuth authorize URL with PKCE
2. User completes login in browser
3. User pastes callback URL/code into terminal
4. Relay exchanges code at `https://auth.openai.com/oauth/token`
5. Relay stores slot-local OAuth profile (`access`, `refresh`, `expires`, `accountId`)

### Native token lifecycle

Before Codex calls, relay checks token expiry and refreshes using refresh token when needed.

## Usage subsystem

### `codex-api` backend

Usage refresh path:
- `GET https://chatgpt.com/backend-api/wham/usage`
- headers:
  - `Authorization: Bearer <access>`
  - `User-Agent: CodexBar`
  - `Accept: application/json`
  - optional `ChatGPT-Account-Id`

### `local-cache` backend

No network call. Uses slot-local usage snapshot; update via:
- `slot-usage-set`
- `slot-usage-copy-main`

## Runner subsystem

### `codex-direct` backend

1. Select eligible slot from runtime state
2. Load slot auth from slot-local `auth-profiles.json`
3. Refresh token when near expiry
4. Call `.../codex/responses` directly (`chatgpt.com/backend-api`)
5. Adapt to public API contract:
   - `/v1/responses`: native Responses shape
   - `/v1/chat/completions`: compatibility translation over Responses stream

`runner.backend=openclaw` remains as optional legacy path.

## Slot sources

### Preferred

Relay-managed local slots:
- native login
- auth import file
- auth copy profile

### Bridge path

Import from existing OpenClaw slot metadata:
- `slot-import-main`
- `sync-slots`

## Selection behavior

At runtime the relay prefers slots that are:
- enabled
- not busy
- not in cooldown
- above configured usage thresholds when possible

If no fully healthy slot exists, relay can still fall back to best available slot.

## Why this shape matters

Client side (`codex-utils`) can stay stable on the OpenAI-compatible API while backend internals evolve.
