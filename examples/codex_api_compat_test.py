#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = os.getenv("CODEX_BASE_URL", "http://127.0.0.1:8787/v1").rstrip("/")
API_KEY = os.getenv("CODEX_API_KEY", "relay-dev-token")
MODEL = os.getenv("CODEX_MODEL", "gpt-5.4")
USER_AGENT = os.getenv("CODEX_USER_AGENT", "Mozilla/5.0")
MODE = os.getenv("CODEX_MODE", "chat-stream")
PROMPT = " ".join(sys.argv[1:]).strip() or "Balas singkat satu kata saja: halo"
TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "180"))

if MODE == "chat":
    path = "/chat/completions"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": PROMPT}
        ]
    }
elif MODE == "chat-stream":
    path = "/chat/completions"
    payload = {
        "model": MODEL,
        "stream": True,
        "messages": [
            {"role": "user", "content": PROMPT}
        ]
    }
elif MODE == "responses":
    path = "/responses"
    payload = {
        "model": MODEL,
        "input": PROMPT
    }
elif MODE == "responses-stream":
    path = "/responses"
    payload = {
        "model": MODEL,
        "stream": True,
        "input": PROMPT
    }
else:
    raise SystemExit("MODE harus salah satu dari: chat | chat-stream | responses | responses-stream")

req = urllib.request.Request(
    BASE_URL + path,
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        "User-Agent": USER_AGENT,
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if payload.get("stream"):
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if line:
                    print(line)
        else:
            body = resp.read().decode("utf-8")
            print(body)
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    print(f"HTTP_ERROR: {e.code}")
    print(body)
    raise
