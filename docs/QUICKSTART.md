# QUICKSTART

## Prerequisites

Standalone-first default needs:

- Python 3.10+
- ability to complete Codex OAuth login interactively (browser + paste callback/code)

Optional only for bridge workflows:

- `openclaw` in `PATH` (if using `slot-import-main` or OpenClaw backends)

## Install

```bash
git clone https://github.com/IndraLawliet13/codex-slot-relay.git
cd codex-slot-relay
pip install .
```

## Minimal local workflow

### 1) Initialize runtime

```bash
codex-slot-relay init
```

Runtime root:
- `.codex-slot-relay-runtime/`

### 2) Add first slot (native OAuth)

```bash
codex-slot-relay slot-login --slot 1 --label your-email@example.com
```

### 3) Verify slot and usage

```bash
codex-slot-relay slot-list
codex-slot-relay refresh-usage
```

### 4) Start relay

```bash
codex-slot-relay serve
```

### 5) Smoke-test API

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer relay-dev-token' \
  -H 'Content-Type: application/json' \
  --data '{"model":"relay-selftest","messages":[{"role":"user","content":"hello"}]}'
```

## Optional bridge workflows

Import saved slots from main OpenClaw slot metadata:

```bash
codex-slot-relay slot-import-main
```

Import auth file directly into slot:

```bash
codex-slot-relay slot-auth-import-file \
  --slot 2 \
  --label imported@example.com \
  --auth-file /path/to/auth-profiles.json
```

Copy auth from OpenClaw profile:

```bash
codex-slot-relay slot-auth-copy-profile \
  --slot 3 \
  --label copied@example.com \
  --source-profile codex-slot-relay
```

## Pairing with codex-utils

With default local settings in `codex-utils`:

```python
from codex_utils import CodexClient

client = CodexClient()
print(client.ask("Balas satu kata saja: halo"))
```

As long as relay is running on:
- `http://127.0.0.1:8787/v1`
- token `relay-dev-token`
