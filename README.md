# codex-slot-relay

![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![API](https://img.shields.io/badge/API-OpenAI--compatible-black)
![Transport](https://img.shields.io/badge/Streaming-SSE-success)
![License](https://img.shields.io/badge/License-MIT-blue.svg)

OpenAI-compatible relay with slot-aware routing for Codex-backed requests, using local runtime state and no external database.

Designed to pair with [`codex-utils`](https://github.com/IndraLawliet13/codex-utils):

- `codex-utils` -> client helpers
- `codex-slot-relay` -> backend relay + multi-account slot management

## Why this repo exists

Goal: a maintainable multi-account Codex backend you can run independently.

Target flow:

`User -> codex-utils -> codex-slot-relay`

With built-in slot selection and automatic rolling to the best available account based on usage/health.

## Highlights

- OpenAI-compatible endpoints
  - `GET /v1/models`
  - `POST /v1/chat/completions`
  - `POST /v1/responses`
- SSE streaming for both API styles
- slot-aware routing based on usage + cooldown + health
- relay-managed slot lifecycle
  - native login (`slot-login`)
  - auth import/copy (`slot-auth-import-file`, `slot-auth-copy-profile`)
  - usage management (`refresh-usage`, `slot-usage-set`, `slot-usage-copy-main`)
  - enable/disable/remove slot
- optional legacy bridge to OpenClaw when needed

## Current architecture

Default runtime is standalone-first:

- `auth.backend = native`
- `usage.backend = codex-api`
- `runner.backend = codex-direct`

This means the default path does not require OpenClaw for auth, usage refresh, or runner execution.

Legacy fallback backends remain available for migration/compatibility:

- `auth.backend = openclaw`
- `usage.backend = openclaw`
- `runner.backend = openclaw`

## Quick start

### 1) Clone and install

```bash
git clone https://github.com/IndraLawliet13/codex-slot-relay.git
cd codex-slot-relay
pip install .
```

### 2) Initialize runtime

```bash
codex-slot-relay init
```

### 3) Add account to slot (native OAuth)

```bash
codex-slot-relay slot-login --slot 1 --label your-email@example.com
```

The command prints an OAuth URL. Open it, complete login, then paste callback URL/code back to terminal.

### 4) Refresh usage and inspect slots

```bash
codex-slot-relay refresh-usage
codex-slot-relay slot-list
```

### 5) Start relay

```bash
codex-slot-relay serve
```

Quick sanity check:

```bash
curl -sS http://127.0.0.1:8787/healthz
```

Default local API target:

- base URL: `http://127.0.0.1:8787/v1`
- API key: `relay-dev-token`

## Example smoke test

Use `relay-selftest` for plumbing-only verification, or `gpt-5.4` for a real slot-backed request.

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer relay-dev-token' \
  -H 'Content-Type: application/json' \
  --data '{"model":"relay-selftest","messages":[{"role":"user","content":"hello"}]}'
```

## Working with codex-utils

Defaults are aligned for local pairing:

- `CODEX_BASE_URL=http://127.0.0.1:8787/v1`
- `CODEX_API_KEY=relay-dev-token`

So after relay is up, `CodexClient()` can work without extra config.

## CLI overview

Main commands:

- `codex-slot-relay init`
- `codex-slot-relay setup` (legacy convenience bootstrap)
- `codex-slot-relay sync-slots` (legacy bridge import)
- `codex-slot-relay slot-import-main` (clearer alias for `sync-slots`)
- `codex-slot-relay slot-login --slot 2 --label account@example.com`
- `codex-slot-relay slot-auth-import-file --slot 2 --label imported@example.com --auth-file /path/to/auth-profiles.json`
- `codex-slot-relay slot-auth-copy-profile --slot 3 --label copied@example.com --source-profile codex-slot-relay`
- `codex-slot-relay slot-list`
- `codex-slot-relay slot-enable --slot 2`
- `codex-slot-relay slot-disable --slot 2`
- `codex-slot-relay slot-remove --slot 2`
- `codex-slot-relay slot-usage-set --slot slot-2 --usage5h '95% left · resets 3h' --usageWeek '84% left · resets 6d 23h'`
- `codex-slot-relay slot-usage-copy-main --slot slot-2`
- `codex-slot-relay refresh-usage`
- `codex-slot-relay health`
- `codex-slot-relay dependency-map`
- `codex-slot-relay test-runner --slot slot-2 --prompt "Reply with exactly pong"`
- `codex-slot-relay serve`

## Documentation

- `docs/QUICKSTART.md`
- `docs/ARCHITECTURE.md`
- `docs/CODEX_UTILS.md`
- `docs/OPERATOR_GUIDE.md`
- `docs/DEPENDENCY_MAP.md`

## Notes

- Relay is stateless by design.
- `/v1/chat/completions` on `codex-direct` is translated over Codex Responses API.
- `tools` on `chat/completions` path is intentionally limited in current version.

## License

MIT
