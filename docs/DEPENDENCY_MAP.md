# DEPENDENCY MAP

Tracks which subsystems depend on OpenClaw and which are independent.

## Current default posture

Default runtime config is OpenClaw-independent:

- `auth.backend = native`
- `usage.backend = codex-api`
- `runner.backend = codex-direct`

## Subsystem breakdown

## 1) Auth subsystem

Used by:
- `slot-login`
- `slot-auth-import-file`
- `slot-auth-copy-profile`

Supported backends:
- `native` (independent)
- `openclaw` (legacy)

Status on default backend: **OpenClaw-independent**

## 2) Usage subsystem

Used by:
- `refresh-usage`
- `slot-usage-set`
- `slot-usage-copy-main`
- part of `slot-login`

Supported backends:
- `codex-api` (independent)
- `local-cache` (independent)
- `openclaw` (legacy)

Status on default backend: **OpenClaw-independent**

## 3) Runner subsystem

Used by:
- `test-runner`
- `serve`
- live `/v1/chat/completions`
- live `/v1/responses`

Supported backends:
- `codex-direct` (independent)
- `openclaw` (legacy)

Status on default backend: **OpenClaw-independent**

## 4) State/control plane

Always local-runtime and independent:

- `init`
- `slot-list`
- `slot-enable`
- `slot-disable`
- `slot-remove`

## CLI visibility

Check effective dependency split:

```bash
codex-slot-relay dependency-map
```

Output includes:
- configured backend
- supported backends
- effective independence status
- command consumers per subsystem
