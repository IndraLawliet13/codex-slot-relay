# QUICKSTART

## Prerequisites

Current safe mode expects:

- Python 3.10+
- `openclaw` installed and available in `PATH`
- ability to complete Codex login interactively on this machine

## Install

```bash
git clone https://github.com/IndraLawliet13/codex-slot-relay.git
cd codex-slot-relay
pip install .
```

## Minimal local workflow

### 1. Initialize runtime
```bash
codex-slot-relay init
```

This prepares a local runtime under:
- `.codex-slot-relay-runtime/`

### 2. Login a slot
```bash
codex-slot-relay slot-login --slot 1 --label your-email@example.com
```

This stores the authenticated slot inside the relay runtime instead of depending on the main OpenClaw slot store.

### 3. Inspect slot state
```bash
codex-slot-relay slot-list
codex-slot-relay refresh-usage
```

### 4. Start the relay
```bash
codex-slot-relay serve
```

### 5. Smoke test the API
```bash
curl -sS -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer relay-dev-token' \
  -H 'Content-Type: application/json' \
  --data '{"model":"relay-selftest","messages":[{"role":"user","content":"hello"}]}'
```

## Legacy bridge option

If you already have saved Codex slots in a main OpenClaw state and want to import them:

```bash
codex-slot-relay slot-import-main
```

This is intentionally treated as a compatibility bridge, not the preferred future path.

## Pairing with codex-utils

If `codex-utils` is installed with its local defaults, you can immediately use:

```python
from codex_utils import CodexClient

client = CodexClient()
print(client.ask("Balas satu kata saja: halo"))
```

As long as the relay is running on:
- `http://127.0.0.1:8787/v1`
- token `relay-dev-token`
