# codex-slot-relay

![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)
![API](https://img.shields.io/badge/API-OpenAI--compatible-black)
![Transport](https://img.shields.io/badge/Streaming-SSE-success)
![License](https://img.shields.io/badge/License-MIT-blue.svg)

Stateless OpenAI-compatible relay with slot-aware routing for Codex-backed requests.

This project is designed as a practical backend pair for [`codex-utils`](https://github.com/IndraLawliet13/codex-utils). Clone both, start the relay locally, and the client helpers can talk to it immediately with matching defaults.

## Why this repo exists

`codex-slot-relay` is the backend service side of a small two-repo stack:

- **`codex-slot-relay`** -> relay backend / control plane / HTTP API
- **`codex-utils`** -> lightweight Python client helpers for that API

The relay exposes OpenAI-style endpoints while managing multiple Codex slots behind the scenes.

## Highlights

- OpenAI-compatible endpoints
  - `GET /v1/models`
  - `POST /v1/chat/completions`
  - `POST /v1/responses`
- SSE streaming support for both API styles
- slot-aware routing based on cached usage and health
- relay-managed slot lifecycle
  - `slot-login`
  - `slot-list`
  - `slot-enable`
  - `slot-disable`
  - `slot-remove`
- legacy import bridge from a main OpenClaw slot store when needed
- mock/self-test models for plumbing validation

## Current safe-mode architecture

This repository is intentionally in a **safe transitional mode**:

- slot management is becoming self-contained inside the relay runtime
- request execution is still **OpenClaw-backed** underneath
- the public value today is the relay/control-plane design plus compatible HTTP surface

That means users can already clone and use it, while the future “ambitious mode” can replace the execution adapter later without breaking the public API shape.

## Quick start

### 1. Clone and install
```bash
git clone https://github.com/IndraLawliet13/codex-slot-relay.git
cd codex-slot-relay
pip install .
```

### 2. Initialize local runtime
```bash
codex-slot-relay init
```

### 3. Login one slot directly into the relay runtime
```bash
codex-slot-relay slot-login --slot 1 --label your-email@example.com
```

### 4. Check managed slots
```bash
codex-slot-relay slot-list
codex-slot-relay refresh-usage
```

### 5. Start the relay
```bash
codex-slot-relay serve
```

Default local API target:
- base URL: `http://127.0.0.1:8787/v1`
- API key: `relay-dev-token`

## Example local smoke test

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer relay-dev-token' \
  -H 'Content-Type: application/json' \
  --data '{"model":"relay-selftest","messages":[{"role":"user","content":"hello"}]}'
```

## Working with codex-utils

If you also clone `codex-utils`, the defaults are aligned for local pairing:

- `CODEX_BASE_URL=http://127.0.0.1:8787/v1`
- `CODEX_API_KEY=relay-dev-token`

So after the relay is running locally, a simple `CodexClient()` can work without extra configuration.

See:
- `docs/CODEX_UTILS.md`
- `examples/quickstart_codex_utils.py`

## CLI overview

Main commands:

- `codex-slot-relay init`
- `codex-slot-relay slot-login --slot 2 --label account@example.com`
- `codex-slot-relay slot-list`
- `codex-slot-relay slot-enable --slot 2`
- `codex-slot-relay slot-disable --slot 2`
- `codex-slot-relay slot-remove --slot 2`
- `codex-slot-relay refresh-usage`
- `codex-slot-relay slot-import-main`
- `codex-slot-relay dependency-map`
- `codex-slot-relay test-runner --slot slot-2 --prompt "Reply with exactly pong"`
- `codex-slot-relay serve`

## Documentation

- `docs/QUICKSTART.md`
- `docs/ARCHITECTURE.md`
- `docs/CODEX_UTILS.md`

## Notes

- This relay is **stateless by design**.
- Slot execution is currently **OpenClaw-backed** in safe mode.
- The HTTP API surface is intended to remain stable even as the internal execution adapter evolves.

## License

MIT
