# ARCHITECTURE

## Public shape

The relay exposes an OpenAI-compatible HTTP surface while managing multiple Codex slots behind the scenes.

### API surface

- `GET /healthz`
- `GET /readyz`
- `GET /admin/slots`
- `GET /v1/models`
- `POST /admin/refresh-usage`
- `POST /v1/chat/completions`
- `POST /v1/responses`

## Safe mode

Current safe mode separates the project into two concerns:

### 1. Control plane owned by this repository

This repo now owns:
- relay runtime initialization
- relay-managed slot login and storage
- slot enable / disable / remove lifecycle
- cached usage refresh
- slot selection and cooldown behavior
- public HTTP interface

### 2. Execution adapter still backed by OpenClaw

Requests are still executed through an OpenClaw-backed adapter in this version.

This is intentional because it provides a stable stepping stone:
- easier to clone and run now
- easier to showcase now
- easier to replace later with a more ambitious runner

The first ambitious-mode step is already reflected in code/config:
- `auth.backend`
- `usage.backend`
- `runner.backend`

So the remaining OpenClaw dependency is now explicit and localized instead of being hidden implicitly.

## Slot sources

### Preferred path
Relay-managed local slots:
- `slot-login`
- `slot-list`
- `slot-enable`
- `slot-disable`
- `slot-remove`

### Transitional bridge path
Import from an existing main OpenClaw slot store:
- `slot-import-main`
- `sync-slots`

## Selection behavior

At runtime, the relay prefers slots that are:
- enabled
- not busy
- not in cooldown
- above usage thresholds when possible

If no fully healthy slot exists, the relay can still fall back to the best available eligible slot.

## Why the API shape matters

`codex-utils` and any other client can stay focused on the OpenAI-compatible surface while the backend evolves internally.

That API stability is the core contract between this repo and the companion client library.
