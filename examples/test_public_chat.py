#!/usr/bin/env python3
import json
import os
import sys
import urllib.request

BASE_URL = os.getenv("CODEX_BASE_URL", "http://127.0.0.1:8787/v1")
API_KEY = os.getenv("CODEX_API_KEY", "relay-dev-token")
MODEL = os.getenv("CODEX_MODEL", "relay-selftest")
PROMPT = " ".join(sys.argv[1:]).strip() or "hello"

payload = {
    "model": MODEL,
    "messages": [
        {"role": "user", "content": PROMPT}
    ]
}

req = urllib.request.Request(
    BASE_URL.rstrip("/") + "/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    method="POST",
)

with urllib.request.urlopen(req, timeout=60) as resp:
    body = resp.read().decode("utf-8")
    print(body)
