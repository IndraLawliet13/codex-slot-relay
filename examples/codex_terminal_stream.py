#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.getenv("CODEX_BASE_URL", "http://127.0.0.1:8787/v1").rstrip("/")
API_KEY = os.getenv("CODEX_API_KEY", "relay-dev-token")
MODEL = os.getenv("CODEX_MODEL", "gpt-5.4")
USER_AGENT = os.getenv("CODEX_USER_AGENT", "Mozilla/5.0")
TIMEOUT = int(os.getenv("CODEX_TIMEOUT", "180"))
SYSTEM_PROMPT = os.getenv("CODEX_SYSTEM_PROMPT", "")


def stream_chat(messages):
    payload = {
        "model": MODEL,
        "stream": True,
        "messages": messages,
    }
    req = urllib.request.Request(
        BASE_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    assistant_text = []
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    assistant_text.append(piece)
                    print(piece, end="", flush=True)
        print()
        return "".join(assistant_text).strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"\nHTTP_ERROR: {e.code}")
        print(body)
        return ""
    except Exception as e:
        print(f"\nERROR: {e}")
        return ""


def build_messages(history, user_prompt):
    messages = []
    if SYSTEM_PROMPT.strip():
        messages.append({"role": "system", "content": SYSTEM_PROMPT.strip()})
    messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})
    return messages


def run_once(prompt):
    history = []
    reply = stream_chat(build_messages(history, prompt))
    return 0 if reply else 1


def run_repl():
    history = []
    print("Codex terminal stream ready")
    print("Commands: /reset, /exit")
    while True:
        try:
            prompt = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt.lower() in {"/exit", "exit", "quit", "/quit"}:
            return 0
        if prompt.lower() == "/reset":
            history = []
            print("history reset")
            continue

        print("ai> ", end="", flush=True)
        reply = stream_chat(build_messages(history, prompt))
        if reply:
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": reply})


def main():
    if len(sys.argv) > 1:
        return run_once(" ".join(sys.argv[1:]).strip())
    return run_repl()


if __name__ == "__main__":
    raise SystemExit(main())
