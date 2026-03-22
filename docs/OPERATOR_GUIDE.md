# OPERATOR GUIDE

Day-to-day operator workflow for `codex-slot-relay`.

## 1) Initialize runtime

```bash
codex-slot-relay init
```

## 2) Add account into slot

### Option A (default): native OAuth login

```bash
codex-slot-relay slot-login --slot 1 --label your-email@example.com
```

Flow:
- command prints OAuth URL
- open URL in browser
- finish auth
- paste callback URL/code back into terminal

### Option B: import existing auth file

```bash
codex-slot-relay slot-auth-import-file \
  --slot 2 \
  --label imported@example.com \
  --auth-file /path/to/auth-profiles.json
```

### Option C: copy auth from OpenClaw profile (bridge)

```bash
codex-slot-relay slot-auth-copy-profile \
  --slot 3 \
  --label copied@example.com \
  --source-profile codex-slot-relay
```

Optional source agent override:

```bash
codex-slot-relay slot-auth-copy-profile \
  --slot 3 \
  --label copied@example.com \
  --source-profile codex-slot-relay \
  --source-agent relay
```

## 3) Inspect slots

```bash
codex-slot-relay slot-list
codex-slot-relay dependency-map
```

## 4) Refresh usage

Default backend: `codex-api`

```bash
codex-slot-relay refresh-usage
codex-slot-relay refresh-usage --slot slot-2
```

### Local-cache workflow

If you set `usage.backend=local-cache`, keep usage snapshots updated manually:

```bash
codex-slot-relay slot-usage-set \
  --slot slot-2 \
  --usage5h '95% left · resets 3h' \
  --usageWeek '84% left · resets 6d 23h'

codex-slot-relay slot-usage-copy-main --slot slot-2
```

## 5) Slot lifecycle controls

Disable temporarily:

```bash
codex-slot-relay slot-disable --slot slot-4
```

Enable again:

```bash
codex-slot-relay slot-enable --slot slot-4
```

Remove slot (destructive for relay-local slot state):

```bash
codex-slot-relay slot-remove --slot slot-5
```

## 6) Start relay

```bash
codex-slot-relay serve
```

## 7) Smoke-test API

```bash
curl -sS http://127.0.0.1:8787/healthz
curl -sS -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer relay-dev-token' \
  -H 'Content-Type: application/json' \
  --data '{"model":"relay-selftest","messages":[{"role":"user","content":"hello"}]}'
```

## Optional migration command

Import all slots from main OpenClaw slot store:

```bash
codex-slot-relay slot-import-main
```
