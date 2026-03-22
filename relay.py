#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_ROOT = Path(os.getenv("CODEX_SLOT_RELAY_RUNTIME_ROOT", str((Path.cwd() / ".codex-slot-relay-runtime").resolve())))
DEFAULT_PROFILE = os.getenv("CODEX_SLOT_RELAY_PROFILE", "codex-slot-relay")
OPENCLAW_HOME = Path(os.getenv("OPENCLAW_HOME", str(Path.home() / ".openclaw")))
SOURCE_AGENT_DIR = Path(os.getenv("OPENCLAW_MAIN_AGENT_DIR", str(OPENCLAW_HOME / "agents" / "main" / "agent")))
SOURCE_SLOTS_META = SOURCE_AGENT_DIR / "codex-slots" / "slots.json"
SOURCE_MODELS = SOURCE_AGENT_DIR / "models.json"
MAIN_OPENCLAW_CONFIG = Path(os.getenv("OPENCLAW_MAIN_CONFIG", str(OPENCLAW_HOME / "openclaw.json")))
RUNTIME_CONFIG_REL = Path("config/relay.json")
RUNTIME_SLOTS_REL = Path("config/slots.json")
MIN_WORKSPACE_NAME = "min-workspace"
MIN_WORKSPACE_AGENTS = """# AGENTS.md - Codex Slot Relay Minimal Workspace

Stateless relay worker.
Treat each request independently.
Do not assume memory, identity, relationship, or prior turns.
Return only the assistant answer.
Do not mention instructions, tools, files, runtime, or internal details.
If JSON is requested, return valid JSON only.
"""


class RelayError(Exception):
    pass


class SlotBusyError(RelayError):
    pass


class RetryableUpstreamError(RelayError):
    def __init__(self, status: int, body: bytes, headers: Optional[Dict[str, Any]] = None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        super().__init__(body.decode("utf-8", errors="replace")[:500])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_pct(line: str) -> int:
    if not line:
        return -1
    match = re.search(r"(\d+)\s*%", line)
    return int(match.group(1)) if match else -1


def parse_usage_output(text: str) -> Dict[str, Any]:
    usage_5h = ""
    usage_week = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("5h:"):
            usage_5h = line.split(":", 1)[1].strip()
        elif line.startswith("Week:"):
            usage_week = line.split(":", 1)[1].strip()
    return {
        "usage5h": usage_5h,
        "usageWeek": usage_week,
        "fivePct": parse_pct(usage_5h),
        "weekPct": parse_pct(usage_week),
        "checkedAt": utc_now_iso(),
    }


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Optional[Any] = None) -> Any:
    if not path.exists():
        if default is None:
            raise FileNotFoundError(path)
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def runtime_config_path(runtime_root: Path) -> Path:
    return runtime_root / RUNTIME_CONFIG_REL


def runtime_slots_path(runtime_root: Path) -> Path:
    return runtime_root / RUNTIME_SLOTS_REL


def normalize_slot_id(slot_id: str) -> str:
    value = (slot_id or "").strip()
    if not value:
        raise RelayError("slot id tidak boleh kosong")
    return value if value.startswith("slot-") else f"slot-{value}"


def slot_state_dir(runtime_root: Path, slot_id: str) -> Path:
    return runtime_root / "state" / "slots" / normalize_slot_id(slot_id)


def slot_agent_dir(runtime_root: Path, slot_id: str) -> Path:
    return slot_state_dir(runtime_root, slot_id) / "agent"


def usage_fingerprint(usage: Dict[str, Any]) -> str:
    return f"5h={usage.get('usage5h', '')} | week={usage.get('usageWeek', '')}".strip()


def minimal_workspace_path(runtime_root: Path) -> Path:
    return runtime_root / MIN_WORKSPACE_NAME


def ensure_minimal_workspace(runtime_root: Path) -> Path:
    workspace = minimal_workspace_path(runtime_root)
    ensure_dir(workspace)
    for child in workspace.iterdir():
        if child.name == "AGENTS.md":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    (workspace / "AGENTS.md").write_text(MIN_WORKSPACE_AGENTS, encoding="utf-8")
    return workspace


def default_runtime_config(runtime_root: Path, profile: str) -> Dict[str, Any]:
    return {
        "listen": "127.0.0.1:8787",
        "authToken": "relay-dev-token",
        "profile": profile,
        "workspace": str(minimal_workspace_path(runtime_root)),
        "runtimeRoot": str(runtime_root),
        "usageTtlSeconds": 300,
        "requestTimeoutSeconds": 180,
        "thinking": "off",
        "selectionPolicy": "best-week-then-5h",
        "thresholds": {
            "min5hPct": 15,
            "minWeekPct": 10
        },
        "cooldown": {
            "genericErrorSeconds": 300,
            "quotaErrorSeconds": 1800
        },
        "runner": {
            "agentId": "relay"
        }
    }


def make_profile_config(runtime_root: Path, profile: str, workspace: Optional[Path] = None) -> Dict[str, Any]:
    main_cfg = load_json(MAIN_OPENCLAW_CONFIG, {})
    gateway_cfg = main_cfg.get("gateway", {})
    workspace = workspace or minimal_workspace_path(runtime_root)
    return {
        "auth": {
            "profiles": {
                "openai-codex:default": {
                    "provider": "openai-codex",
                    "mode": "oauth"
                }
            }
        },
        "agents": {
            "defaults": {
                "model": {
                    "primary": "openai-codex/gpt-5.4",
                    "fallbacks": []
                },
                "workspace": str(workspace)
            },
            "list": [
                {
                    "id": "relay",
                    "default": True,
                    "model": "openai-codex/gpt-5.4",
                    "tools": {
                        "profile": "minimal",
                        "deny": ["session_status"]
                    }
                }
            ]
        },
        "tools": {
            "profile": "minimal",
            "deny": ["session_status"]
        },
        "gateway": gateway_cfg,
        "commands": {
            "native": "auto",
            "nativeSkills": "auto"
        }
    }


def setup_runtime(runtime_root: Path, profile: str, force: bool = False) -> None:
    ensure_dir(runtime_root / "config")
    ensure_dir(runtime_root / "state" / "slots")
    ensure_dir(runtime_root / "logs")
    ensure_dir(runtime_root / "run")
    workspace = ensure_minimal_workspace(runtime_root)

    cfg_path = runtime_config_path(runtime_root)
    if force or not cfg_path.exists():
        save_json(cfg_path, default_runtime_config(runtime_root, profile))

    profile_dir = Path.home() / f".openclaw-{profile}"
    ensure_dir(profile_dir)
    profile_cfg_path = profile_dir / "openclaw.json"
    save_json(profile_cfg_path, make_profile_config(runtime_root, profile, workspace=workspace))


def extract_codex_profile_info(auth_path: Path) -> Dict[str, Any]:
    auth = load_json(auth_path, {})
    for profile_id, profile in (auth.get("profiles") or {}).items():
        if profile.get("provider") == "openai-codex":
            return {
                "profileId": profile_id,
                "accountId": profile.get("accountId"),
                "expires": profile.get("expires"),
            }
    raise RelayError(f"no openai-codex profile found in {auth_path}")


def ensure_slot_models(agent_dir: Path) -> None:
    ensure_dir(agent_dir)
    dest = agent_dir / "models.json"
    if dest.exists():
        return
    if SOURCE_MODELS.exists():
        shutil.copy2(SOURCE_MODELS, dest)
        return
    save_json(dest, {"providers": {}})


def relay_profile_agent_dir(profile: str, runtime_root: Optional[Path] = None) -> Path:
    agent_id = "relay"
    if runtime_root:
        try:
            cfg = load_json(runtime_config_path(runtime_root), {})
            agent_id = str(cfg.get("runner", {}).get("agentId", "relay"))
        except Exception:
            agent_id = "relay"
    return Path.home() / f".openclaw-{profile}" / "agents" / agent_id / "agent"


def copy_profile_auth_into_slot(runtime_root: Path, profile: str, slot_id: str) -> Optional[Path]:
    slot_id = normalize_slot_id(slot_id)
    source_agent = relay_profile_agent_dir(profile, runtime_root=runtime_root)
    source_auth = source_agent / "auth-profiles.json"
    if not source_auth.exists():
        return None
    target_agent = slot_agent_dir(runtime_root, slot_id)
    ensure_dir(target_agent)
    shutil.copy2(source_auth, target_agent / "auth-profiles.json")
    source_models = source_agent / "models.json"
    if source_models.exists():
        shutil.copy2(source_models, target_agent / "models.json")
    else:
        ensure_slot_models(target_agent)
    return target_agent / "auth-profiles.json"


def upsert_slot_record(
    runtime_root: Path,
    slot_id: str,
    label: str,
    usage: Dict[str, Any],
    *,
    enabled: bool = True,
    model_default: str = "gpt-5.4",
    runtime_meta: Optional[Dict[str, Any]] = None,
    source_slot: Optional[str] = None,
) -> Dict[str, Any]:
    slot_id = normalize_slot_id(slot_id)
    agent_dir = slot_agent_dir(runtime_root, slot_id)
    auth_file = agent_dir / "auth-profiles.json"
    ensure_slot_models(agent_dir)
    info = extract_codex_profile_info(auth_file)
    provider_models = load_json(agent_dir / "models.json", {}).get("providers", {})
    return {
        "id": slot_id,
        "sourceSlot": source_slot,
        "enabled": enabled,
        "label": label or slot_id,
        "agentDir": str(agent_dir),
        "authFile": str(auth_file),
        "modelDefault": model_default,
        "usage": usage,
        "runtime": runtime_meta or {
            "cooldownUntil": "",
            "lastUsedAt": "",
            "lastError": "",
            "consecutiveFailures": 0,
        },
        "sourceMeta": {
            "emailLabel": label or slot_id,
            "accountId": info.get("accountId"),
            "profileId": info.get("profileId"),
            "expires": info.get("expires"),
            "savedAt": utc_now_iso(),
            "usageFingerprint": usage_fingerprint(usage),
        },
        "providerModels": provider_models,
    }


class SlotStore:
    def __init__(self, runtime_root: Path):
        self.runtime_root = runtime_root
        self.path = runtime_slots_path(runtime_root)
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            return load_json(self.path, {"version": 1, "slots": []})

    def save(self, data: Dict[str, Any]) -> None:
        with self._lock:
            save_json(self.path, data)

    def load_slots(self) -> List[Dict[str, Any]]:
        return self.load().get("slots", [])

    def save_slots(self, slots: List[Dict[str, Any]]) -> None:
        self.save({"version": 1, "updatedAt": utc_now_iso(), "slots": slots})


class BusyTracker:
    def __init__(self):
        self._lock = threading.RLock()
        self._busy = set()

    def acquire(self, slot_id: str) -> None:
        with self._lock:
            if slot_id in self._busy:
                raise SlotBusyError(f"slot {slot_id} sedang busy")
            self._busy.add(slot_id)

    def release(self, slot_id: str) -> None:
        with self._lock:
            self._busy.discard(slot_id)

    def is_busy(self, slot_id: str) -> bool:
        with self._lock:
            return slot_id in self._busy


BUSY_TRACKER = BusyTracker()


def load_source_slots() -> Dict[str, Any]:
    return load_json(SOURCE_SLOTS_META)


def sync_slots(runtime_root: Path) -> List[Dict[str, Any]]:
    store = SlotStore(runtime_root)
    existing = {slot["id"]: slot for slot in store.load_slots()}
    source = load_source_slots().get("slots", {})
    models_json = load_json(SOURCE_MODELS, {})

    synced: List[Dict[str, Any]] = []
    for source_slot in sorted(source.keys(), key=lambda x: int(x)):
        info = source[source_slot]
        slot_id = f"slot-{source_slot}"
        agent_dir = runtime_root / "state" / "slots" / slot_id / "agent"
        ensure_dir(agent_dir)

        src_auth = Path(info["file"])
        if not src_auth.exists():
            raise FileNotFoundError(f"slot auth missing: {src_auth}")

        dest_auth = agent_dir / "auth-profiles.json"
        shutil.copy2(src_auth, dest_auth)
        shutil.copy2(SOURCE_MODELS, agent_dir / "models.json")

        prev = existing.get(slot_id, {})
        slot = {
            "id": slot_id,
            "sourceSlot": source_slot,
            "enabled": prev.get("enabled", True),
            "label": info.get("emailLabel") or slot_id,
            "agentDir": str(agent_dir),
            "authFile": str(dest_auth),
            "modelDefault": prev.get("modelDefault", "gpt-5.4"),
            "usage": {
                "usage5h": info.get("usage5h", prev.get("usage", {}).get("usage5h", "")),
                "usageWeek": info.get("usageWeek", prev.get("usage", {}).get("usageWeek", "")),
                "fivePct": parse_pct(info.get("usage5h", prev.get("usage", {}).get("usage5h", ""))),
                "weekPct": parse_pct(info.get("usageWeek", prev.get("usage", {}).get("usageWeek", ""))),
                "checkedAt": info.get("liveCheckedAt", prev.get("usage", {}).get("checkedAt", ""))
            },
            "runtime": {
                "cooldownUntil": prev.get("runtime", {}).get("cooldownUntil", ""),
                "lastUsedAt": prev.get("runtime", {}).get("lastUsedAt", ""),
                "lastError": prev.get("runtime", {}).get("lastError", ""),
                "consecutiveFailures": prev.get("runtime", {}).get("consecutiveFailures", 0)
            },
            "sourceMeta": {
                "emailLabel": info.get("emailLabel"),
                "accountId": info.get("accountId"),
                "profileId": info.get("profileId"),
                "expires": info.get("expires"),
                "savedAt": info.get("savedAt"),
                "usageFingerprint": info.get("usageFingerprint", "")
            },
            "providerModels": models_json.get("providers", {})
        }
        synced.append(slot)

    store.save_slots(synced)
    return synced


def run_subprocess(command: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 120, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(command, capture_output=True, text=True, env=merged_env, timeout=timeout, cwd=cwd)


def run_interactive_subprocess(command: List[str], env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> int:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(command, env=merged_env, cwd=cwd)
    return int(proc.returncode or 0)


def fetch_slot_usage(agent_dir: str, profile: str) -> Dict[str, Any]:
    env = {
        "OPENCLAW_AGENT_DIR": agent_dir,
        "OPENCLAW_HIDE_BANNER": "1",
        "OPENCLAW_SUPPRESS_NOTES": "1",
    }
    proc = run_subprocess([
        "openclaw", "--profile", profile, "status", "--usage"
    ], env=env, timeout=60)
    if proc.returncode != 0:
        raise RelayError(f"usage check failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return parse_usage_output(proc.stdout)


def refresh_usage(runtime_root: Path, profile: str, slot_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    store = SlotStore(runtime_root)
    slots = store.load_slots()
    changed: List[Dict[str, Any]] = []
    slot_filter = normalize_slot_id(slot_filter) if slot_filter else None
    for slot in slots:
        if slot_filter and slot["id"] != slot_filter:
            continue
        usage = fetch_slot_usage(slot["agentDir"], profile)
        slot["usage"] = usage
        slot.setdefault("sourceMeta", {})["usageFingerprint"] = usage_fingerprint(usage)
        changed.append({"id": slot["id"], **usage})
    store.save_slots(slots)
    return changed


def blank_usage() -> Dict[str, Any]:
    return {
        "usage5h": "",
        "usageWeek": "",
        "fivePct": -1,
        "weekPct": -1,
        "checkedAt": utc_now_iso(),
    }


def login_slot(runtime_root: Path, profile: str, slot_id: str, label: str) -> Dict[str, Any]:
    setup_runtime(runtime_root, profile)
    slot_id = normalize_slot_id(slot_id)
    agent_dir = slot_agent_dir(runtime_root, slot_id)
    ensure_dir(agent_dir)
    ensure_slot_models(agent_dir)

    env = {
        "OPENCLAW_AGENT_DIR": str(agent_dir),
        "OPENCLAW_HIDE_BANNER": "1",
        "OPENCLAW_SUPPRESS_NOTES": "1",
    }
    command = ["openclaw", "--profile", profile, "models", "auth", "login", "--provider", "openai-codex"]
    code = run_interactive_subprocess(command, env=env, cwd=str(minimal_workspace_path(runtime_root)))
    if code != 0:
        raise RelayError(f"slot login gagal untuk {slot_id} (exit {code})")

    auth_file = agent_dir / "auth-profiles.json"
    if not auth_file.exists():
        copied = copy_profile_auth_into_slot(runtime_root, profile, slot_id)
        if copied:
            auth_file = copied
    if not auth_file.exists():
        raise RelayError(f"auth file tidak ditemukan setelah login: {auth_file}")

    try:
        usage = fetch_slot_usage(str(agent_dir), profile)
    except Exception:
        usage = blank_usage()

    store = SlotStore(runtime_root)
    slots = store.load_slots()
    prev = next((item for item in slots if item["id"] == slot_id), {})
    record = upsert_slot_record(
        runtime_root,
        slot_id,
        label,
        usage,
        enabled=prev.get("enabled", True),
        model_default=prev.get("modelDefault", "gpt-5.4"),
        runtime_meta=prev.get("runtime"),
        source_slot=prev.get("sourceSlot"),
    )
    replaced = False
    for idx, item in enumerate(slots):
        if item["id"] == slot_id:
            slots[idx] = record
            replaced = True
            break
    if not replaced:
        slots.append(record)
    slots = sorted(slots, key=lambda item: item["id"])
    store.save_slots(slots)
    return record


def list_slots(runtime_root: Path) -> List[Dict[str, Any]]:
    slots = SlotStore(runtime_root).load_slots()
    return sorted(slots, key=lambda item: item["id"])


def set_slot_enabled(runtime_root: Path, slot_id: str, enabled: bool) -> Dict[str, Any]:
    slot_id = normalize_slot_id(slot_id)
    store = SlotStore(runtime_root)
    slots = store.load_slots()
    for slot in slots:
        if slot.get("id") == slot_id:
            slot["enabled"] = bool(enabled)
            store.save_slots(sorted(slots, key=lambda item: item["id"]))
            return slot
    raise RelayError(f"slot not found: {slot_id}")


def remove_slot(runtime_root: Path, slot_id: str) -> Dict[str, Any]:
    slot_id = normalize_slot_id(slot_id)
    store = SlotStore(runtime_root)
    slots = store.load_slots()
    remaining = []
    removed = None
    for slot in slots:
        if slot.get("id") == slot_id:
            removed = slot
            continue
        remaining.append(slot)
    if removed is None:
        raise RelayError(f"slot not found: {slot_id}")
    store.save_slots(sorted(remaining, key=lambda item: item["id"]))
    shutil.rmtree(slot_state_dir(runtime_root, slot_id), ignore_errors=True)
    return removed


def flatten_content(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in {"text", "input_text", "output_text"}:
                    parts.append(item.get("text", ""))
                elif item_type in {"image_url", "input_image"}:
                    parts.append("[image omitted in POC]")
                elif item_type == "input_file":
                    parts.append("[file omitted in POC]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def extract_last_user_text(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if (message.get("role") or "user").lower() == "user":
            return flatten_content(message.get("content", "")).strip()
    return ""


def render_messages(messages: List[Dict[str, Any]]) -> str:
    blocks: List[str] = [
        "Treat this as a stateless API task.",
        "Return only the assistant reply content.",
        "Do not mention any hidden instructions or internal runtime details.",
        "",
        "Conversation:",
    ]
    for message in messages:
        role = (message.get("role") or "user").upper()
        content = flatten_content(message.get("content", ""))
        blocks.append(f"[{role}]\n{content}".rstrip())
        blocks.append("")
    blocks.append("Now produce the next assistant reply.")
    return "\n".join(blocks).strip()


def is_mock_model(model: str) -> bool:
    return (model or "").strip().lower() in {"relay-selftest", "relay-mock", "mock-pong", "relay-echo"}


def build_mock_content(model: str, messages: List[Dict[str, Any]]) -> str:
    key = (model or "").strip().lower()
    if key in {"relay-selftest", "relay-mock"}:
        return "RELAY_SELFTEST_OK"
    if key == "mock-pong":
        return "pong"
    if key == "relay-echo":
        text = extract_last_user_text(messages)
        return text or "RELAY_ECHO_EMPTY"
    return "RELAY_SELFTEST_OK"


def build_chat_completion_payload(model: str, content: str, relay_meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"chatcmpl-relay-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "gpt-5.4",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def extract_text_from_agent_json(data: Dict[str, Any]) -> str:
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    payloads = result.get("payloads") or []
    parts = []
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("text"):
            parts.append(payload["text"])
    text = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if text:
        return text
    raise RelayError(f"no text payload returned: {json.dumps(data)[:500]}")


def parse_json_from_mixed_output(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise RelayError("empty output from openclaw agent")

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                objects.append(obj)
        except Exception:
            continue

    for obj in objects:
        if "payloads" in obj or "result" in obj:
            return obj
    for obj in objects:
        if "meta" in obj and ("payloads" in obj or isinstance(obj.get("result"), dict)):
            return obj
    if objects:
        return objects[0]
    raise RelayError(f"failed to parse JSON object from output: {text[:500]}")


def build_request_runtime(slot: Dict[str, Any], runtime_root: Path, profile: str) -> Dict[str, Path]:
    req_root = Path(tempfile.mkdtemp(prefix="req-", dir=str(runtime_root / "run")))
    state_dir = req_root / "state"
    agent_dir = req_root / "agent"
    ensure_dir(state_dir)
    ensure_dir(agent_dir)

    src_auth = Path(slot.get("authFile") or Path(slot["agentDir"]) / "auth-profiles.json")
    src_models = Path(slot["agentDir"]) / "models.json"
    shutil.copy2(src_auth, agent_dir / "auth-profiles.json")
    shutil.copy2(src_models, agent_dir / "models.json")

    config_path = state_dir / "openclaw.json"
    cfg = make_profile_config(runtime_root, profile, workspace=minimal_workspace_path(runtime_root))
    cfg["gateway"] = {
        "bind": "loopback",
        "http": {
            "endpoints": {
                "chatCompletions": {"enabled": True},
                "responses": {"enabled": True},
            }
        },
    }
    save_json(config_path, cfg)

    return {
        "root": req_root,
        "stateDir": state_dir,
        "agentDir": agent_dir,
        "configPath": config_path,
    }


def find_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_gateway_http(port: int, timeout_seconds: int = 20) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise RelayError(f"gateway startup timeout on 127.0.0.1:{port}: {last_error}")


def start_slot_gateway(slot: Dict[str, Any], profile: str, runtime_root: Path) -> Dict[str, Any]:
    request_runtime = build_request_runtime(slot, runtime_root, profile)
    token = f"relay-gateway-{uuid.uuid4().hex}"
    port = find_free_loopback_port()
    log_path = request_runtime["root"] / "gateway.log"
    log_handle = open(log_path, "wb")
    env = {
        **os.environ,
        "OPENCLAW_STATE_DIR": str(request_runtime["stateDir"]),
        "OPENCLAW_CONFIG_PATH": str(request_runtime["configPath"]),
        "OPENCLAW_AGENT_DIR": str(request_runtime["agentDir"]),
        "OPENCLAW_HIDE_BANNER": "1",
        "OPENCLAW_SUPPRESS_NOTES": "1",
    }
    command = [
        "openclaw", "gateway", "run",
        "--allow-unconfigured",
        "--port", str(port),
        "--auth", "token",
        "--token", token,
        "--compact",
    ]
    proc = subprocess.Popen(
        command,
        cwd=str(minimal_workspace_path(runtime_root)),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_for_gateway_http(port)
        return {
            "runtime": request_runtime,
            "token": token,
            "port": port,
            "proc": proc,
            "logPath": log_path,
            "logHandle": log_handle,
        }
    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log_handle.close()
        log_tail = ""
        try:
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except Exception:
            pass
        shutil.rmtree(request_runtime["root"], ignore_errors=True)
        raise RelayError(f"failed to start isolated slot gateway: {log_tail.strip() or 'no log output'}")


def stop_slot_gateway(ctx: Dict[str, Any]) -> None:
    proc = ctx.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    log_handle = ctx.get("logHandle")
    try:
        if log_handle is not None:
            log_handle.close()
    except Exception:
        pass
    runtime = ctx.get("runtime") or {}
    root = runtime.get("root")
    if root:
        shutil.rmtree(root, ignore_errors=True)


def should_retry_upstream_status(status: int) -> bool:
    return status in {429, 500, 502, 503, 504}


def send_raw_http_response(handler: "RelayHandler", status: int, headers: Optional[Dict[str, Any]], body: bytes) -> None:
    handler.send_response(status)
    content_type = None
    if headers:
        content_type = headers.get("Content-Type") or headers.get("content-type")
    handler.send_header("Content-Type", content_type or "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def stream_via_slot_gateway(handler: "RelayHandler", path: str, body: Dict[str, Any], slot: Dict[str, Any]) -> None:
    gateway_ctx = start_slot_gateway(slot, handler.server.relay_config["profile"], handler.server.runtime_root)
    try:
        upstream_headers = {
            "Authorization": f"Bearer {gateway_ctx['token']}",
            "Content-Type": "application/json",
            "Accept": handler.headers.get("Accept", "application/json"),
            "User-Agent": "CodexSlotRelay/0.1",
            "x-openclaw-agent-id": "relay",
        }
        session_key = handler.headers.get("x-openclaw-session-key")
        if session_key:
            upstream_headers["x-openclaw-session-key"] = session_key
        request = urllib.request.Request(
            f"http://127.0.0.1:{gateway_ctx['port']}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers=upstream_headers,
            method="POST",
        )
        timeout = int(handler.server.relay_config.get("requestTimeoutSeconds", 180)) + 30
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                if body.get("stream"):
                    handler.send_response(resp.status)
                    handler.send_header("Content-Type", resp.headers.get("Content-Type", "text/event-stream; charset=utf-8"))
                    handler.send_header("Cache-Control", resp.headers.get("Cache-Control", "no-cache"))
                    handler.send_header("Connection", "close")
                    handler.send_header("X-Accel-Buffering", "no")
                    handler.end_headers()
                    while True:
                        reader = getattr(resp, "read1", None)
                        chunk = reader(4096) if callable(reader) else resp.read(4096)
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
                        handler.wfile.flush()
                    handler.close_connection = True
                else:
                    response_body = resp.read()
                    send_raw_http_response(handler, resp.status, dict(resp.headers), response_body)
        except urllib.error.HTTPError as exc:
            error_body = exc.read()
            if should_retry_upstream_status(exc.code):
                raise RetryableUpstreamError(exc.code, error_body, dict(exc.headers))
            send_raw_http_response(handler, exc.code, dict(exc.headers), error_body)
            return False
    finally:
        stop_slot_gateway(gateway_ctx)


def run_slot_prompt(slot: Dict[str, Any], prompt: str, profile: str, timeout: int, thinking: str, agent_id: str, runtime_root: Path) -> Dict[str, Any]:
    session_id = f"codex-slot-relay-{uuid.uuid4()}"
    request_runtime = build_request_runtime(slot, runtime_root, profile)
    env = {
        "OPENCLAW_STATE_DIR": str(request_runtime["stateDir"]),
        "OPENCLAW_CONFIG_PATH": str(request_runtime["configPath"]),
        "OPENCLAW_AGENT_DIR": str(request_runtime["agentDir"]),
        "OPENCLAW_HIDE_BANNER": "1",
        "OPENCLAW_SUPPRESS_NOTES": "1",
    }
    command = [
        "openclaw",
        "agent",
        "--agent", agent_id,
        "--session-id", session_id,
        "--message", prompt,
        "--json",
        "--timeout", str(timeout),
        "--thinking", thinking,
        "--local",
    ]
    try:
        proc = run_subprocess(command, env=env, timeout=timeout + 15, cwd=str(minimal_workspace_path(runtime_root)))
        if proc.returncode != 0:
            raise RelayError(proc.stderr.strip() or proc.stdout.strip() or f"openclaw agent exited {proc.returncode}")
        data = parse_json_from_mixed_output(proc.stdout)
        text = extract_text_from_agent_json(data)
        return {
            "sessionId": session_id,
            "text": text,
            "raw": data,
        }
    finally:
        shutil.rmtree(request_runtime["root"], ignore_errors=True)


def parse_iso_or_none(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def slot_in_cooldown(slot: Dict[str, Any]) -> bool:
    until = parse_iso_or_none(slot.get("runtime", {}).get("cooldownUntil", ""))
    return bool(until and until > utc_now())


def usage_stale(slot: Dict[str, Any], ttl_seconds: int) -> bool:
    checked = parse_iso_or_none(slot.get("usage", {}).get("checkedAt", ""))
    if not checked:
        return True
    return (utc_now() - checked).total_seconds() > ttl_seconds


def quota_error_text(text: str) -> bool:
    lowered = text.lower()
    phrase_needles = [
        "rate limit", "quota", "exhausted", "credit", "usage limit", "too many requests"
    ]
    if any(needle in lowered for needle in phrase_needles):
        return True
    if re.search(r"(?:http|status|error|code)\s*[:=]?\s*429\b", lowered):
        return True
    if re.fullmatch(r"429", lowered.strip()):
        return True
    return False


def upstream_error_text(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    if quota_error_text(lowered):
        return True
    extra_needles = [
        "unsupported parameter",
        "temporarily unavailable",
        "api error",
        "please try again later",
        "service unavailable",
    ]
    return any(needle in lowered for needle in extra_needles)


def apply_slot_error(slot: Dict[str, Any], config: Dict[str, Any], err_text: str) -> None:
    slot.setdefault("runtime", {})
    slot["runtime"]["lastError"] = err_text[:500]
    slot["runtime"]["consecutiveFailures"] = int(slot["runtime"].get("consecutiveFailures", 0) or 0) + 1
    cooldown_key = "quotaErrorSeconds" if quota_error_text(err_text) else "genericErrorSeconds"
    cooldown_seconds = int(config.get("cooldown", {}).get(cooldown_key, 300))
    slot["runtime"]["cooldownUntil"] = datetime.fromtimestamp(time.time() + cooldown_seconds, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clear_slot_error(slot: Dict[str, Any]) -> None:
    slot.setdefault("runtime", {})
    slot["runtime"]["lastError"] = ""
    slot["runtime"]["consecutiveFailures"] = 0
    slot["runtime"]["cooldownUntil"] = ""
    slot["runtime"]["lastUsedAt"] = utc_now_iso()


def choose_slots(config: Dict[str, Any], slots: List[Dict[str, Any]], refresh_if_stale: bool, runtime_root: Path) -> List[Dict[str, Any]]:
    profile = config["profile"]
    ttl = int(config.get("usageTtlSeconds", 300))
    if refresh_if_stale and any(usage_stale(slot, ttl) for slot in slots if slot.get("enabled", True)):
        refresh_usage(runtime_root, profile)
        slots = SlotStore(runtime_root).load_slots()

    min_5h = int(config.get("thresholds", {}).get("min5hPct", 15))
    min_week = int(config.get("thresholds", {}).get("minWeekPct", 10))

    healthy = []
    fallback = []
    for slot in slots:
        if not slot.get("enabled", True):
            continue
        if BUSY_TRACKER.is_busy(slot["id"]):
            continue
        if slot_in_cooldown(slot):
            continue
        usage = slot.get("usage", {})
        five = int(usage.get("fivePct", -1))
        week = int(usage.get("weekPct", -1))
        if five <= 0 or week <= 0:
            continue
        fallback.append(slot)
        if five >= min_5h and week >= min_week:
            healthy.append(slot)

    candidates = healthy if healthy else fallback
    candidates.sort(
        key=lambda s: (
            -int(s.get("usage", {}).get("weekPct", -1)),
            -int(s.get("usage", {}).get("fivePct", -1)),
            s.get("runtime", {}).get("lastUsedAt", ""),
            s["id"],
        )
    )
    return candidates


def chunk_text_for_sse(text: str, chunk_size: int = 120) -> List[str]:
    text = "" if text is None else str(text)
    if not text:
        return [""]
    chunks: List[str] = []
    cursor = 0
    size = max(16, int(chunk_size or 120))
    while cursor < len(text):
        end = min(len(text), cursor + size)
        if end < len(text):
            split = text.rfind(" ", cursor, end)
            if split > cursor + max(8, size // 3):
                end = split + 1
        chunks.append(text[cursor:end])
        cursor = end
    return chunks or [""]


def build_openai_error_payload(message: str, error_type: str = "invalid_request_error", code: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "message": message,
        "type": error_type,
    }
    if code:
        payload["code"] = code
    return {"error": payload}


def resolve_error_status(message: str) -> Tuple[int, str, Optional[str]]:
    lowered = (message or "").strip().lower()
    if not lowered:
        return 500, "api_error", "api_error"
    if any(needle in lowered for needle in [
        "invalid json body",
        "messages is required",
        "missing user message",
        "input is required",
        "invalid request body",
    ]):
        return 400, "invalid_request_error", None
    if any(needle in lowered for needle in ["unauthorized", "invalid api key"]):
        return 401, "authentication_error", "invalid_api_key"
    if quota_error_text(lowered) or "tidak ada slot codex yang eligible" in lowered:
        return 429, "rate_limit_error", "rate_limit_exceeded"
    return 500, "api_error", "api_error"


def build_chat_completion_chunk(completion_id: str, model: str, delta: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
    choice: Dict[str, Any] = {
        "index": 0,
        "delta": delta,
    }
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model or "gpt-5.4",
        "choices": [choice],
    }


def create_empty_responses_usage() -> Dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def create_response_resource(response_id: str, model: str, status: str, output: List[Dict[str, Any]], usage: Optional[Dict[str, Any]] = None, error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model or "gpt-5.4",
        "output": output,
        "usage": usage or create_empty_responses_usage(),
        "error": error,
    }


def create_assistant_output_item(item_id: str, text: str, status: str = "completed") -> Dict[str, Any]:
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "content": [{
            "type": "output_text",
            "text": text,
        }],
        "status": status,
    }


def normalize_responses_input_to_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions.strip()})

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        if raw_input.strip():
            messages.append({"role": "user", "content": raw_input})
        return messages

    if isinstance(raw_input, dict):
        raw_items = [raw_input]
    elif isinstance(raw_input, list):
        raw_items = raw_input
    else:
        raw_items = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            messages.append({
                "role": item.get("role") or "user",
                "content": item.get("content", ""),
            })
        elif item_type == "function_call_output":
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "content": output,
            })
    return messages


def execute_relay_completion(server: "RelayServer", requested_model: str, prompt: str, source_messages: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, str, Dict[str, Any]]:
    requested_model = requested_model or "gpt-5.4"
    if is_mock_model(requested_model):
        content = build_mock_content(requested_model, source_messages or [])
        return requested_model, content, {
            "mode": "mock",
            "slot": None,
            "upstreamSessionId": None,
            "tried": [],
        }

    timeout = int(server.relay_config.get("requestTimeoutSeconds", 180))
    thinking = str(server.relay_config.get("thinking", "off"))
    runner_agent = str(server.relay_config.get("runner", {}).get("agentId", "relay"))

    slots = choose_slots(server.relay_config, server.slot_store.load_slots(), False, server.runtime_root)
    if not slots:
        raise RelayError("tidak ada slot Codex yang eligible")

    tried: List[str] = []
    last_error = None
    for slot in slots[:2]:
        tried.append(slot["id"])
        try:
            BUSY_TRACKER.acquire(slot["id"])
            result = run_slot_prompt(slot, prompt, server.relay_config["profile"], timeout, thinking, runner_agent, server.runtime_root)
            if upstream_error_text(result.get("text", "")):
                raise RelayError(f"upstream returned error-like text: {result.get('text', '')[:240]}")
            current_slots = server.slot_store.load_slots()
            by_id = {s["id"]: s for s in current_slots}
            if slot["id"] in by_id:
                clear_slot_error(by_id[slot["id"]])
                server.slot_store.save_slots(current_slots)
            return requested_model or slot.get("modelDefault", "gpt-5.4"), result["text"], {
                "mode": "live",
                "slot": slot["id"],
                "upstreamSessionId": result["sessionId"],
                "tried": tried,
            }
        except Exception as exc:
            last_error = str(exc)
            current_slots = server.slot_store.load_slots()
            by_id = {s["id"]: s for s in current_slots}
            if slot["id"] in by_id:
                apply_slot_error(by_id[slot["id"]], server.relay_config, last_error)
                server.slot_store.save_slots(current_slots)
        finally:
            BUSY_TRACKER.release(slot["id"])

    raise RelayError(last_error or "request failed")


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "CodexSlotRelay/0.1"

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_api_error(self, status: int, message: str, error_type: str = "invalid_request_error", code: Optional[str] = None) -> None:
        self._send_json(status, build_openai_error_payload(message, error_type=error_type, code=code))

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RelayError(f"invalid JSON body: {exc}") from exc

    def _check_auth(self) -> bool:
        expected = self.server.relay_config["authToken"]
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {expected}"

    def _set_sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _write_sse_data(self, payload: Any) -> None:
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _write_sse_event(self, payload: Dict[str, Any]) -> None:
        self.wfile.write(f"event: {payload['type']}\n".encode("utf-8"))
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _write_done(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_chat_completion_stream(self, model: str, content: str) -> None:
        completion_id = f"chatcmpl-relay-{uuid.uuid4().hex[:12]}"
        self._set_sse_headers()
        self._write_sse_data(build_chat_completion_chunk(completion_id, model, {"role": "assistant"}, None))
        for chunk in chunk_text_for_sse(content):
            if not chunk:
                continue
            self._write_sse_data(build_chat_completion_chunk(completion_id, model, {"content": chunk}, None))
        self._write_sse_data(build_chat_completion_chunk(completion_id, model, {}, "stop"))
        self._write_done()
        self.close_connection = True

    def _send_responses_stream(self, model: str, content: str, response_id: str, output_item_id: str) -> None:
        self._set_sse_headers()
        initial = create_response_resource(response_id, model, "in_progress", [])
        self._write_sse_event({"type": "response.created", "response": initial})
        self._write_sse_event({"type": "response.in_progress", "response": initial})
        self._write_sse_event({
            "type": "response.output_item.added",
            "output_index": 0,
            "item": create_assistant_output_item(output_item_id, "", status="in_progress"),
        })
        self._write_sse_event({
            "type": "response.content_part.added",
            "item_id": output_item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        })
        for chunk in chunk_text_for_sse(content):
            if not chunk:
                continue
            self._write_sse_event({
                "type": "response.output_text.delta",
                "item_id": output_item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
            })
        self._write_sse_event({
            "type": "response.output_text.done",
            "item_id": output_item_id,
            "output_index": 0,
            "content_index": 0,
            "text": content,
        })
        self._write_sse_event({
            "type": "response.content_part.done",
            "item_id": output_item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": content},
        })
        completed_item = create_assistant_output_item(output_item_id, content, status="completed")
        self._write_sse_event({
            "type": "response.output_item.done",
            "output_index": 0,
            "item": completed_item,
        })
        self._write_sse_event({
            "type": "response.completed",
            "response": create_response_resource(response_id, model, "completed", [completed_item]),
        })
        self._write_done()
        self.close_connection = True

    def _handle_live_proxy(self, path: str, body: Dict[str, Any]) -> None:
        slots = choose_slots(self.server.relay_config, self.server.slot_store.load_slots(), False, self.server.runtime_root)
        if not slots:
            raise RelayError("tidak ada slot Codex yang eligible")

        last_retry: Optional[RetryableUpstreamError] = None
        last_error = None
        for slot in slots[:2]:
            try:
                BUSY_TRACKER.acquire(slot["id"])
                proxied_ok = stream_via_slot_gateway(self, path, body, slot)
                current_slots = self.server.slot_store.load_slots()
                by_id = {s["id"]: s for s in current_slots}
                if proxied_ok and slot["id"] in by_id:
                    clear_slot_error(by_id[slot["id"]])
                    self.server.slot_store.save_slots(current_slots)
                return
            except RetryableUpstreamError as exc:
                last_retry = exc
                last_error = str(exc)
                current_slots = self.server.slot_store.load_slots()
                by_id = {s["id"]: s for s in current_slots}
                if slot["id"] in by_id:
                    apply_slot_error(by_id[slot["id"]], self.server.relay_config, exc.body.decode("utf-8", errors="replace"))
                    self.server.slot_store.save_slots(current_slots)
            except Exception as exc:
                last_error = str(exc)
                current_slots = self.server.slot_store.load_slots()
                by_id = {s["id"]: s for s in current_slots}
                if slot["id"] in by_id:
                    apply_slot_error(by_id[slot["id"]], self.server.relay_config, last_error)
                    self.server.slot_store.save_slots(current_slots)
            finally:
                BUSY_TRACKER.release(slot["id"])

        if last_retry is not None:
            send_raw_http_response(self, last_retry.status, last_retry.headers, last_retry.body)
            return
        raise RelayError(last_error or "request failed")

    def _handle_chat_completions(self, body: Dict[str, Any]) -> None:
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise RelayError("messages is required")
        requested_model = str(body.get("model") or "gpt-5.4")
        if not is_mock_model(requested_model):
            self._handle_live_proxy("/v1/chat/completions", body)
            return
        prompt = render_messages(messages)
        model, content, _relay_meta = execute_relay_completion(self.server, requested_model, prompt, source_messages=messages)
        if body.get("stream"):
            self._send_chat_completion_stream(model, content)
            return
        self._send_json(200, build_chat_completion_payload(model, content, {}))

    def _handle_responses(self, body: Dict[str, Any]) -> None:
        requested_model = str(body.get("model") or "gpt-5.4")
        if not is_mock_model(requested_model):
            self._handle_live_proxy("/v1/responses", body)
            return
        messages = normalize_responses_input_to_messages(body)
        has_user_message = any((msg.get("role") or "user") in {"user", "tool"} and flatten_content(msg.get("content", "")).strip() for msg in messages)
        if not has_user_message:
            raise RelayError("Missing user message in `input`.")
        prompt = render_messages(messages)
        model, content, _relay_meta = execute_relay_completion(self.server, requested_model, prompt, source_messages=messages)
        response_id = f"resp_{uuid.uuid4()}"
        output_item_id = f"msg_{uuid.uuid4()}"
        if body.get("stream"):
            self._send_responses_stream(model, content, response_id, output_item_id)
            return
        self._send_json(200, create_response_resource(
            response_id,
            model,
            "completed",
            [create_assistant_output_item(output_item_id, content, status="completed")],
        ))

    def do_GET(self):
        path = (self.path or "/").split("?", 1)[0]
        if path in {"/healthz", "/readyz"}:
            slots = self.server.slot_store.load_slots()
            self._send_json(200, {
                "ok": True,
                "time": utc_now_iso(),
                "slots": len(slots),
                "busy": sorted(list(BUSY_TRACKER._busy)),
            })
            return

        if path == "/admin/slots":
            if not self._check_auth():
                self._send_api_error(401, "Unauthorized", error_type="authentication_error", code="invalid_api_key")
                return
            slots = self.server.slot_store.load_slots()
            sanitized = []
            for slot in slots:
                sanitized.append({
                    "id": slot["id"],
                    "enabled": slot.get("enabled", True),
                    "label": slot.get("label"),
                    "usage": slot.get("usage", {}),
                    "runtime": slot.get("runtime", {}),
                    "busy": BUSY_TRACKER.is_busy(slot["id"]),
                })
            self._send_json(200, {"slots": sanitized})
            return

        if path == "/v1/models":
            if not self._check_auth():
                self._send_api_error(401, "Unauthorized", error_type="authentication_error", code="invalid_api_key")
                return
            created = int(time.time())
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": "gpt-5.4", "object": "model", "created": created, "owned_by": "openai"},
                    {"id": "relay-selftest", "object": "model", "created": created, "owned_by": "openai"},
                    {"id": "mock-pong", "object": "model", "created": created, "owned_by": "openai"},
                    {"id": "relay-echo", "object": "model", "created": created, "owned_by": "openai"},
                ],
            })
            return

        self._send_api_error(404, "Not found", error_type="invalid_request_error", code="not_found")

    def do_POST(self):
        path = (self.path or "/").split("?", 1)[0]
        if path == "/admin/refresh-usage":
            if not self._check_auth():
                self._send_api_error(401, "Unauthorized", error_type="authentication_error", code="invalid_api_key")
                return
            changed = refresh_usage(self.server.runtime_root, self.server.relay_config["profile"])
            self._send_json(200, {"updated": changed})
            return

        if path not in {"/v1/chat/completions", "/v1/responses"}:
            self._send_api_error(404, "Not found", error_type="invalid_request_error", code="not_found")
            return

        if not self._check_auth():
            self._send_api_error(401, "Unauthorized", error_type="authentication_error", code="invalid_api_key")
            return

        try:
            body = self._read_json()
            if path == "/v1/chat/completions":
                self._handle_chat_completions(body)
            else:
                self._handle_responses(body)
        except RelayError as exc:
            status, error_type, code = resolve_error_status(str(exc))
            self._send_api_error(status, str(exc), error_type=error_type, code=code)
        except BrokenPipeError:
            return
        except Exception as exc:
            self._send_api_error(500, f"unexpected_error: {exc}", error_type="api_error", code="api_error")


class RelayServer(ThreadingHTTPServer):
    def __init__(self, addr: Tuple[str, int], handler_cls, runtime_root: Path, relay_config: Dict[str, Any]):
        super().__init__(addr, handler_cls)
        self.runtime_root = runtime_root
        self.relay_config = relay_config
        self.slot_store = SlotStore(runtime_root)


def serve(runtime_root: Path) -> None:
    config = load_json(runtime_config_path(runtime_root))
    listen = config["listen"]
    host, port_str = listen.rsplit(":", 1)
    server = RelayServer((host, int(port_str)), RelayHandler, runtime_root, config)
    print(f"Codex Slot Relay listening on http://{listen}")
    server.serve_forever()


def cmd_init(args: argparse.Namespace) -> int:
    setup_runtime(args.runtime_root, args.profile, force=args.force)
    print(json.dumps({
        "runtimeRoot": str(args.runtime_root),
        "profile": args.profile,
        "initialized": True,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    setup_runtime(args.runtime_root, args.profile, force=args.force)
    synced = sync_slots(args.runtime_root)
    try:
        changed = refresh_usage(args.runtime_root, args.profile)
    except Exception as exc:
        changed = []
        print(f"WARN refresh_usage failed during setup: {exc}", file=sys.stderr)
    print(json.dumps({
        "runtimeRoot": str(args.runtime_root),
        "profile": args.profile,
        "slotsSynced": [slot["id"] for slot in synced],
        "usageRefreshed": changed,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_sync_slots(args: argparse.Namespace) -> int:
    synced = sync_slots(args.runtime_root)
    print(json.dumps({"slots": synced}, indent=2, ensure_ascii=False))
    return 0


def cmd_slot_list(args: argparse.Namespace) -> int:
    slots = list_slots(args.runtime_root)
    payload = []
    for slot in slots:
        payload.append({
            "id": slot.get("id"),
            "label": slot.get("label"),
            "enabled": slot.get("enabled", True),
            "usage5h": slot.get("usage", {}).get("usage5h", ""),
            "usageWeek": slot.get("usage", {}).get("usageWeek", ""),
            "accountId": slot.get("sourceMeta", {}).get("accountId"),
            "savedAt": slot.get("sourceMeta", {}).get("savedAt"),
            "sourceSlot": slot.get("sourceSlot"),
        })
    print(json.dumps({"slots": payload}, indent=2, ensure_ascii=False))
    return 0


def cmd_slot_login(args: argparse.Namespace) -> int:
    slot = login_slot(args.runtime_root, args.profile, args.slot, args.label)
    print(json.dumps({"slot": slot}, indent=2, ensure_ascii=False))
    return 0


def cmd_slot_enable(args: argparse.Namespace) -> int:
    slot = set_slot_enabled(args.runtime_root, args.slot, True)
    print(json.dumps({"slot": slot}, indent=2, ensure_ascii=False))
    return 0


def cmd_slot_disable(args: argparse.Namespace) -> int:
    slot = set_slot_enabled(args.runtime_root, args.slot, False)
    print(json.dumps({"slot": slot}, indent=2, ensure_ascii=False))
    return 0


def cmd_slot_remove(args: argparse.Namespace) -> int:
    slot = remove_slot(args.runtime_root, args.slot)
    print(json.dumps({"removed": {"id": slot.get("id"), "label": slot.get("label")}}, indent=2, ensure_ascii=False))
    return 0


def cmd_refresh_usage(args: argparse.Namespace) -> int:
    changed = refresh_usage(args.runtime_root, args.profile, slot_filter=args.slot)
    print(json.dumps({"updated": changed}, indent=2, ensure_ascii=False))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    config = load_json(runtime_config_path(args.runtime_root))
    slots = SlotStore(args.runtime_root).load_slots()
    payload = {
        "runtimeRoot": str(args.runtime_root),
        "profile": config["profile"],
        "slots": slots,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_test_runner(args: argparse.Namespace) -> int:
    config = load_json(runtime_config_path(args.runtime_root))
    slots = SlotStore(args.runtime_root).load_slots()
    target_slot = normalize_slot_id(args.slot)
    slot = None
    for item in slots:
        if item["id"] == target_slot:
            slot = item
            break
    if not slot:
        raise SystemExit(f"slot not found: {target_slot}")
    result = run_slot_prompt(
        slot=slot,
        prompt=args.prompt,
        profile=config["profile"],
        timeout=int(config.get("requestTimeoutSeconds", 180)),
        thinking=str(config.get("thinking", "off")),
        agent_id=str(config.get("runner", {}).get("agentId", "relay")),
        runtime_root=args.runtime_root,
    )
    print(json.dumps({
        "slot": slot["id"],
        "sessionId": result["sessionId"],
        "text": result["text"],
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    serve(args.runtime_root)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Slot Relay")
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create runtime dirs and profile config without importing slots")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_setup = sub.add_parser("setup", help="Legacy setup: create runtime dirs, import main slots, refresh usage")
    p_setup.add_argument("--force", action="store_true")
    p_setup.set_defaults(func=cmd_setup)

    p_sync = sub.add_parser("sync-slots", help="Import/copy saved slot auth snapshots from the main OpenClaw slot store")
    p_sync.set_defaults(func=cmd_sync_slots)

    p_import = sub.add_parser("slot-import-main", help="Alias of sync-slots with clearer safe-mode naming")
    p_import.set_defaults(func=cmd_sync_slots)

    p_slot_list = sub.add_parser("slot-list", help="List relay-managed slots from the relay runtime store")
    p_slot_list.set_defaults(func=cmd_slot_list)

    p_slot_login = sub.add_parser("slot-login", help="Login a Codex account directly into one relay-managed slot")
    p_slot_login.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_login.add_argument("--label", required=True, help="Human label such as email/account name")
    p_slot_login.set_defaults(func=cmd_slot_login)

    p_slot_enable = sub.add_parser("slot-enable", help="Enable one relay-managed slot for selection")
    p_slot_enable.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_enable.set_defaults(func=cmd_slot_enable)

    p_slot_disable = sub.add_parser("slot-disable", help="Disable one relay-managed slot so it will not be selected")
    p_slot_disable.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_disable.set_defaults(func=cmd_slot_disable)

    p_slot_remove = sub.add_parser("slot-remove", help="Remove one relay-managed slot and delete its relay-local auth/runtime state")
    p_slot_remove.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_remove.set_defaults(func=cmd_slot_remove)

    p_refresh = sub.add_parser("refresh-usage", help="Refresh usage cache from all or one relay-managed slot")
    p_refresh.add_argument("--slot")
    p_refresh.set_defaults(func=cmd_refresh_usage)

    p_health = sub.add_parser("health", help="Print current relay runtime health JSON")
    p_health.set_defaults(func=cmd_health)

    p_test = sub.add_parser("test-runner", help="Run one stateless prompt through one slot")
    p_test.add_argument("--slot", required=True)
    p_test.add_argument("--prompt", required=True)
    p_test.set_defaults(func=cmd_test_runner)

    p_serve = sub.add_parser("serve", help="Run the local HTTP relay server")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
