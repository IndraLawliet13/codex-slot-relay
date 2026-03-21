# Using codex-slot-relay with codex-utils

This repository is intended to pair directly with:
- `https://github.com/IndraLawliet13/codex-utils`

## Default local pairing

The two repos are aligned around these local defaults:

- relay base URL: `http://127.0.0.1:8787/v1`
- relay API key: `relay-dev-token`
- default model: `gpt-5.4`

That means the easiest local workflow is:

1. run `codex-slot-relay serve`
2. use `CodexClient()` from `codex-utils`
3. call `.ask()`, `.chat_stream_text()`, or `.responses_text()` immediately

## Example

```python
from codex_utils import CodexClient

client = CodexClient()
print(client.ask("Balas satu kata saja: halo"))
```

## If you want custom config

```bash
export CODEX_BASE_URL="http://127.0.0.1:8787/v1"
export CODEX_API_KEY="relay-dev-token"
export CODEX_MODEL="gpt-5.4"
```

## Why keep the repos separate

- `codex-slot-relay` focuses on backend routing and slot control
- `codex-utils` focuses on client ergonomics and request helpers

Keeping them separate makes the system easier to reason about and easier to reuse in other projects.
