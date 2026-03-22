# DEPENDENCY MAP

This document explains exactly which parts of `codex-slot-relay` are already independent and which parts still depend on OpenClaw in the current safe-to-ambitious transition.

## Already OpenClaw-independent

These control-plane features are owned directly by this repository and stored in the relay runtime:

- `init`
- `slot-list`
- `slot-enable`
- `slot-disable`
- `slot-remove`
- relay-local slot state files under `.codex-slot-relay-runtime/` or your chosen runtime root

These commands manage relay-owned metadata and slot directories without needing the main OpenClaw slot store.

## Still OpenClaw-dependent today

### 1. Auth backend
Current backend can be:
- `openclaw`
- `file-import`
- `profile-copy`

Used by:
- `slot-login`
- `slot-auth-import-file`
- `slot-auth-copy-profile`

Current status:
- if `auth.backend=openclaw`, interactive Codex/OAuth login is still delegated to OpenClaw
- if you use `slot-auth-import-file`, auth onboarding can happen by importing a compatible `auth-profiles.json` directly into the relay-local slot state
- if you use `slot-auth-copy-profile`, auth onboarding can happen by copying auth from another OpenClaw profile/agent directory without running the relay login command itself

So auth is no longer limited to one OpenClaw-only onboarding path, even though a fully native no-OpenClaw OAuth flow is still a future step.

### 2. Usage backend
Current backend: `openclaw`

Used by:
- `refresh-usage`
- part of `slot-login`

Why it still depends on OpenClaw:
- usage/quota introspection currently comes from `openclaw status --usage`

### 3. Runner backend
Current backend: `openclaw`

Used by:
- `test-runner`
- `serve`
- the live `/v1/chat/completions` and `/v1/responses` request path

Why it still depends on OpenClaw:
- request execution still runs through an OpenClaw-backed gateway/runtime adapter in safe mode

## What changed in the first ambitious-mode step

The relay now makes these dependencies explicit in config and code:

- `auth.backend`
- `usage.backend`
- `runner.backend`

Today the supported value is still:
- `openclaw`

But the important change is architectural:
- the dependency is now **explicit and localized** instead of being hidden implicitly everywhere
- future backends can replace each section one by one without changing the public HTTP API

## CLI visibility

You can inspect the current dependency split with:

```bash
codex-slot-relay dependency-map
```

## Next ambitious-mode targets

A likely future order is:

1. replace `usage.backend`
2. replace `auth.backend`
3. replace `runner.backend`

That order keeps the HTTP API stable while gradually reducing OpenClaw reliance.
