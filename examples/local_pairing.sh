#!/usr/bin/env bash
set -euo pipefail

# Minimal local pairing helper for codex-slot-relay + codex-utils.

: "${SLOT_ID:=1}"
: "${SLOT_LABEL:=your-email@example.com}"

codex-slot-relay init
codex-slot-relay slot-login --slot "$SLOT_ID" --label "$SLOT_LABEL"
codex-slot-relay refresh-usage --slot "slot-$SLOT_ID"

echo
echo 'Start the relay in another terminal with:'
echo '  codex-slot-relay serve'
echo
echo 'Then use codex-utils with:'
echo '  export CODEX_BASE_URL=http://127.0.0.1:8787/v1'
echo '  export CODEX_API_KEY=relay-dev-token'
