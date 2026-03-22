# ARCHITECTURE

## Public shape

The relay exposes an OpenAI-compatible HTTP surface while managing multiple Codex slots behind the scenes.

### API surface

- `GET /healthz`
- `GET /readyz`
- `GET /admin/slots`
- `GET /v1/models`
- `POST /admin/refresh-usage`
- `POST /v1/chat/completions`
- `POST /v1/responses`

## Backend split

This project now separates subsystem backends explicitly via config:

- `auth.backend`
- `usage.backend`
- `runner.backend`

Current state:
- `auth.backend`: `openclaw`
- `usage.backend`: `openclaw`
- `runner.backend`: `codex-direct` (default), `openclaw` (legacy fallback)

## Runner path (Step 4 first implementation)

`codex-direct` runner flow:
1. Select eligible slot from relay-managed runtime state
2. Read slot auth from slot-local `auth-profiles.json`
3. Resolve Codex base URL from slot `models.json` (fallback `https://chatgpt.com/backend-api`)
4. Call `.../codex/responses` directly via HTTP/SSE
5. Adapt output to public API contract:
   - `/v1/responses`: native Responses shape
   - `/v1/chat/completions`: compatibility translation layer over Responses events

Legacy `openclaw` runner flow remains available for fallback and comparison.

## Slot sources

### Preferred path
Relay-managed local slots:
- `slot-login`
- `slot-list`
- `slot-enable`
- `slot-disable`
- `slot-remove`

### Transitional bridge path
Import from existing main OpenClaw slot store:
- `slot-import-main`
- `sync-slots`

## Selection behavior

At runtime, the relay prefers slots that are:
- enabled
- not busy
- not in cooldown
- above usage thresholds when possible

If no fully healthy slot exists, the relay can still fall back to the best eligible slot.

## Why the API shape matters

`codex-utils` and other clients can stay focused on the OpenAI-compatible surface while internals evolve.

That API stability is the core contract between this repo and client integrations.
