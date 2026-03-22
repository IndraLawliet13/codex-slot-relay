# DEPENDENCY MAP

This document tracks which subsystems in `codex-slot-relay` still rely on OpenClaw and which are already separated.

## Already OpenClaw-independent

Control-plane runtime state is owned by this repository:

- `init`
- `slot-list`
- `slot-enable`
- `slot-disable`
- `slot-remove`
- relay-local slot state files under `.codex-slot-relay-runtime/` (or custom runtime root)

## OpenClaw-dependent sections

### 1. Auth backend
Current backend: `openclaw`

Used by:
- `slot-login`

Why:
- interactive OAuth login flow is still delegated to OpenClaw.

### 2. Usage backend
Current backend: `openclaw`

Used by:
- `refresh-usage`
- part of `slot-login`

Why:
- usage/quota refresh currently calls `openclaw status --usage`.

## Runner backend (Step 4)

Current supported backends:
- `openclaw` (legacy adapter)
- `codex-direct` (new direct runner)

Default: `codex-direct`

Used by:
- `test-runner`
- `serve`
- live `/v1/chat/completions` and `/v1/responses`

What `codex-direct` means:
- runner execution no longer needs per-request OpenClaw gateway/agent runtime
- relay reads slot auth directly from slot `auth-profiles.json`
- relay calls `https://chatgpt.com/backend-api/codex/responses` directly
- `/v1/chat/completions` is served through translation over Responses events

## CLI visibility

Check the active backend split:

```bash
codex-slot-relay dependency-map
```

## Next ambitious-mode targets

Likely next improvements:
1. native auth refresh flow (reduce `auth.backend=openclaw` coupling)
2. native usage introspection (reduce `usage.backend=openclaw` coupling)
3. broader parity for advanced tools/function-calling translation in `codex-direct`
