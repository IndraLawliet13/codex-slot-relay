#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = os.getenv("CODEX_BASE_URL", "http://127.0.0.1:8787/v1")
API_KEY = os.getenv("CODEX_API_KEY", "relay-dev-token")
MODEL = os.getenv("CODEX_MODEL", "gpt-5.4")
USER_AGENT = os.getenv("CODEX_USER_AGENT", "Mozilla/5.0")
PROMPT = " ".join(sys.argv[1:]).strip() or "Balas tepat satu kata saja: halo"

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
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read().decode("utf-8")
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        print("MODEL:", data.get("model"))
        print("REPLY:", content)
        print("RAW_JSON:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
except urllib.error.HTTPError as e:
    err_body = e.read().decode("utf-8", errors="replace")
    print(f"HTTP_ERROR: {e.code}")
    print(err_body)
    raise
