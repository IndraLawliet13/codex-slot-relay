# OPERATOR GUIDE

This guide focuses on the most common day-to-day operator tasks.

## Initialize a fresh local runtime

```bash
codex-slot-relay init
```

## Add a new account directly into the relay

```bash
codex-slot-relay slot-login --slot 1 --label your-email@example.com
```

This uses the current auth backend and then stores the resulting auth in the relay-local slot directory.

## Import an existing account from the main slot store

```bash
codex-slot-relay slot-import-main
```

Use this when you already have working Codex slots in a main OpenClaw state and want a fast migration path.

## Inspect slot state

```bash
codex-slot-relay slot-list
codex-slot-relay dependency-map
```

## Refresh usage

```bash
codex-slot-relay refresh-usage
codex-slot-relay refresh-usage --slot slot-2
```

If you want the relay to avoid OpenClaw for usage refresh, switch `usage.backend` to `local-cache` and then manage/update slot usage snapshots locally:

```bash
codex-slot-relay slot-usage-copy-main --slot slot-2
codex-slot-relay slot-usage-set --slot slot-2 --usage5h '95% left · resets 3h' --usageWeek '84% left · resets 6d 23h'
```

## Temporarily disable one slot

```bash
codex-slot-relay slot-disable --slot slot-4
```

Re-enable later:

```bash
codex-slot-relay slot-enable --slot slot-4
```

## Remove a slot

```bash
codex-slot-relay slot-remove --slot slot-5
```

This is destructive for the relay-local runtime of that slot.

## Start the relay

```bash
codex-slot-relay serve
```

## Smoke-test the API

```bash
curl -sS http://127.0.0.1:8787/healthz
curl -sS -X POST http://127.0.0.1:8787/v1/chat/completions \
  -H 'Authorization: Bearer relay-dev-token' \
  -H 'Content-Type: application/json' \
  --data '{"model":"relay-selftest","messages":[{"role":"user","content":"hello"}]}'
```
