# Changelog

## v0.2.0

Standalone-first milestone release.

### Added
- native auth backend (`auth.backend = native`)
- native OAuth login for Codex slots
- native refresh-token handling for slot-local auth
- auth import/copy commands:
  - `slot-auth-import-file`
  - `slot-auth-copy-profile`
- native usage backend (`usage.backend = codex-api`)
- local usage-management commands:
  - `slot-usage-set`
  - `slot-usage-copy-main`
- direct runner backend (`runner.backend = codex-direct`)
- `/v1/chat/completions` translation over the Codex Responses backend
- production-ready systemd example and showcase docs polish

### Changed
- default runtime posture is now standalone-first instead of OpenClaw-first
- dependency map now reports configured backend, supported backends, and effective independence status
- quickstart/docs now reflect the native-first workflow and `/healthz` sanity checks

### Notes
- `chat/completions` tool/function calling is still limited on the `codex-direct` path
- `responses` API is the better path for advanced tool/function workflows
