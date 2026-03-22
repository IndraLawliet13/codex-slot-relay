#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import platform
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
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

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

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_CODEX_INSTRUCTIONS = "You are a stateless API task runner. Return only the assistant reply content."
DEFAULT_CODEX_USAGE_URL = f"{DEFAULT_CODEX_BASE_URL}/wham/usage"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
PROFILE_CLAIM_PATH = "https://api.openai.com/profile"
OPENAI_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
OPENAI_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
OPENAI_CODEX_SCOPE = "openid profile email offline_access"
OPENAI_CODEX_ORIGINATOR = "pi"
AUTH_REFRESH_LEEWAY_SECONDS = 300
OAUTH_CALLBACK_TIMEOUT_SECONDS = 600


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
        "auth": {
            "backend": "native"
        },
        "usage": {
            "backend": "codex-api"
        },
        "runner": {
            "backend": "codex-direct",
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


SECTION_DEFAULT_BACKENDS: Dict[str, str] = {
    "auth": "native",
    "usage": "codex-api",
    "runner": "codex-direct",
}

SECTION_SUPPORTED_BACKENDS: Dict[str, Tuple[str, ...]] = {
    "auth": ("native", "openclaw"),
    "usage": ("codex-api", "local-cache", "openclaw"),
    "runner": ("codex-direct", "openclaw"),
}

SECTION_USED_BY: Dict[str, Tuple[str, ...]] = {
    "auth": ("slot-login", "slot-auth-import-file", "slot-auth-copy-profile"),
    "usage": ("refresh-usage", "slot-usage-set", "slot-usage-copy-main", "slot-login"),
    "runner": ("test-runner", "serve"),
}

OPENCLAW_INDEPENDENT_BACKENDS: Dict[str, Tuple[str, ...]] = {
    "auth": ("native",),
    "usage": ("codex-api", "local-cache"),
    "runner": ("codex-direct",),
}


def get_backend_name(runtime_root: Path, section: str, default: Optional[str] = None) -> str:
    cfg = load_json(runtime_config_path(runtime_root), {})
    fallback = default or SECTION_DEFAULT_BACKENDS.get(section, "openclaw")
    return str(cfg.get(section, {}).get("backend", fallback) or fallback)


def assert_supported_backend(runtime_root: Path, section: str, supported: Optional[Iterable[str]] = None, *, purpose: str) -> str:
    backend = get_backend_name(runtime_root, section)
    allowed = tuple(supported or SECTION_SUPPORTED_BACKENDS.get(section, (backend,)))
    if backend not in allowed:
        supported_text = ", ".join(sorted(set(allowed)))
        raise RelayError(f"{purpose} backend '{backend}' belum didukung. backend yang tersedia saat ini: {supported_text}")
    return backend


def dependency_map(runtime_root: Path) -> Dict[str, Any]:
    mapping: Dict[str, Any] = {}
    for section in ("auth", "usage", "runner"):
        backend = get_backend_name(runtime_root, section)
        supported = list(SECTION_SUPPORTED_BACKENDS.get(section, (backend,)))
        status = "OpenClaw-independent" if backend in OPENCLAW_INDEPENDENT_BACKENDS.get(section, ()) else "OpenClaw-dependent"
        mapping[section] = {
            "configuredBackend": backend,
            "supportedBackends": supported,
            "status": status,
            "usedBy": list(SECTION_USED_BY.get(section, ())),
        }
    mapping["stateControlPlane"] = {
        "backend": "local-runtime",
        "status": "OpenClaw-independent",
        "usedBy": ["init", "slot-list", "slot-enable", "slot-disable", "slot-remove"],
    }
    return mapping


def setup_runtime(runtime_root: Path, profile: str, force: bool = False) -> None:
    ensure_dir(runtime_root / "config")
    ensure_dir(runtime_root / "state" / "slots")
    ensure_dir(runtime_root / "logs")
    ensure_dir(runtime_root / "run")
    workspace = ensure_minimal_workspace(runtime_root)

    cfg_path = runtime_config_path(runtime_root)
    if force or not cfg_path.exists():
        save_json(cfg_path, default_runtime_config(runtime_root, profile))

    cfg = load_json(cfg_path, default_runtime_config(runtime_root, profile))
    openclaw_needed = any(str(cfg.get(section, {}).get("backend", "")) == "openclaw" for section in ("auth", "usage", "runner"))
    if openclaw_needed or MAIN_OPENCLAW_CONFIG.exists():
        profile_dir = Path.home() / f".openclaw-{profile}"
        ensure_dir(profile_dir)
        profile_cfg_path = profile_dir / "openclaw.json"
        save_json(profile_cfg_path, make_profile_config(runtime_root, profile, workspace=workspace))


def coerce_epoch_ms(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        dt = parse_iso_or_none(raw)
        if dt is not None:
            return int(dt.timestamp() * 1000)
    return None


def extract_email_from_access_token(token: str) -> Optional[str]:
    payload = decode_jwt_payload(token)
    profile = payload.get(PROFILE_CLAIM_PATH)
    if isinstance(profile, dict):
        email = str(profile.get("email") or "").strip()
        if email:
            return email
    return None


def extract_expires_ms_from_access_token(token: str) -> Optional[int]:
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp * 1000)
    return None


def build_codex_auth_profile(credentials: Dict[str, Any], profile_id: str = "openai-codex:default") -> Dict[str, Any]:
    access = str(credentials.get("access") or credentials.get("access_token") or "").strip()
    refresh = str(credentials.get("refresh") or credentials.get("refresh_token") or "").strip()
    if not access:
        raise RelayError("codex credentials missing access token")
    if not refresh:
        raise RelayError("codex credentials missing refresh token")
    account_id = str(credentials.get("accountId") or credentials.get("account_id") or "").strip()
    if not account_id:
        account_id = extract_account_id_from_access_token(access)
    expires = coerce_epoch_ms(credentials.get("expires"))
    if expires is None:
        expires_in = credentials.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires = int(time.time() * 1000 + float(expires_in) * 1000)
    if expires is None:
        expires = extract_expires_ms_from_access_token(access)
    email = str(credentials.get("email") or "").strip() or (extract_email_from_access_token(access) or "")
    profile: Dict[str, Any] = {
        "type": "oauth",
        "provider": "openai-codex",
        "access": access,
        "refresh": refresh,
        "expires": expires,
        "accountId": account_id,
    }
    if email:
        profile["email"] = email
    return {
        "version": 1,
        "profiles": {
            profile_id: profile,
        },
    }


def normalize_auth_store_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), dict):
        normalized_profiles: Dict[str, Any] = {}
        for profile_id, profile in raw.get("profiles", {}).items():
            if not isinstance(profile, dict):
                continue
            if str(profile.get("provider") or "") != "openai-codex":
                continue
            normalized = build_codex_auth_profile(profile, profile_id=str(profile_id))
            normalized_profiles.update(normalized["profiles"])
        if normalized_profiles:
            return {"version": int(raw.get("version", 1) or 1), "profiles": normalized_profiles}

    if isinstance(raw, dict) and isinstance(raw.get("oauth"), dict):
        oauth = raw.get("oauth") or {}
        if isinstance(oauth.get("openai-codex"), dict):
            return build_codex_auth_profile(oauth.get("openai-codex") or {}, profile_id="openai-codex:default")

    if isinstance(raw, dict) and (raw.get("provider") == "openai-codex" or raw.get("access") or raw.get("access_token")):
        return build_codex_auth_profile(raw, profile_id="openai-codex:default")

    raise RelayError("auth payload tidak kompatibel untuk provider openai-codex")


def write_auth_store(path: Path, payload: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    save_json(path, normalize_auth_store_payload(payload))


def extract_codex_profile_info(auth_path: Path) -> Dict[str, Any]:
    auth = normalize_auth_store_payload(load_json(auth_path, {}))
    for profile_id, profile in (auth.get("profiles") or {}).items():
        if profile.get("provider") == "openai-codex":
            access = str(profile.get("access") or "").strip()
            account_id = str(profile.get("accountId") or "").strip() or (extract_account_id_from_access_token(access) if access else None)
            email = str(profile.get("email") or "").strip() or (extract_email_from_access_token(access) if access else None)
            expires = coerce_epoch_ms(profile.get("expires"))
            if expires is None and access:
                expires = extract_expires_ms_from_access_token(access)
            return {
                "profileId": profile_id,
                "accountId": account_id,
                "expires": expires,
                "email": email,
            }
    raise RelayError(f"no openai-codex profile found in {auth_path}")


def ensure_slot_models(agent_dir: Path) -> None:
    ensure_dir(agent_dir)
    dest = agent_dir / "models.json"
    if dest.exists():
        current = load_json(dest, {})
        providers = current.get("providers") if isinstance(current.get("providers"), dict) else {}
        if "openai-codex" not in providers:
            providers["openai-codex"] = {
                "baseUrl": DEFAULT_CODEX_BASE_URL,
                "api": "openai-codex-responses",
                "models": [],
            }
            current["providers"] = providers
            save_json(dest, current)
        return
    if SOURCE_MODELS.exists():
        shutil.copy2(SOURCE_MODELS, dest)
        ensure_slot_models(agent_dir)
        return
    save_json(dest, {
        "providers": {
            "openai-codex": {
                "baseUrl": DEFAULT_CODEX_BASE_URL,
                "api": "openai-codex-responses",
                "models": [],
            }
        }
    })


def relay_profile_agent_dir(profile: str, runtime_root: Optional[Path] = None, source_agent: Optional[str] = None) -> Path:
    agent_id = source_agent or "relay"
    if runtime_root and not source_agent:
        try:
            cfg = load_json(runtime_config_path(runtime_root), {})
            agent_id = str(cfg.get("runner", {}).get("agentId", "relay"))
        except Exception:
            agent_id = "relay"
    return Path.home() / f".openclaw-{profile}" / "agents" / agent_id / "agent"


def copy_profile_auth_into_slot(runtime_root: Path, profile: str, slot_id: str, *, source_agent: Optional[str] = None) -> Optional[Path]:
    slot_id = normalize_slot_id(slot_id)
    source_agent_dir = relay_profile_agent_dir(profile, runtime_root=runtime_root, source_agent=source_agent)
    source_auth = source_agent_dir / "auth-profiles.json"
    if not source_auth.exists():
        return None
    target_agent = slot_agent_dir(runtime_root, slot_id)
    ensure_dir(target_agent)
    write_auth_store(target_agent / "auth-profiles.json", load_json(source_auth, {}))
    source_models = source_agent_dir / "models.json"
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
    resolved_label = label or info.get("email") or slot_id
    return {
        "id": slot_id,
        "sourceSlot": source_slot,
        "enabled": enabled,
        "label": resolved_label,
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
            "emailLabel": resolved_label,
            "accountId": info.get("accountId"),
            "profileId": info.get("profileId"),
            "expires": info.get("expires"),
            "email": info.get("email"),
            "savedAt": utc_now_iso(),
            "usageFingerprint": usage_fingerprint(usage),
        },
        "providerModels": provider_models,
    }


def save_slot_record(runtime_root: Path, record: Dict[str, Any]) -> Dict[str, Any]:
    store = SlotStore(runtime_root)
    slots = store.load_slots()
    replaced = False
    for idx, item in enumerate(slots):
        if item.get("id") == record.get("id"):
            slots[idx] = record
            replaced = True
            break
    if not replaced:
        slots.append(record)
    store.save_slots(sorted(slots, key=lambda item: item.get("id", "")))
    return record


def get_slot_by_id(runtime_root: Path, slot_id: str) -> Dict[str, Any]:
    target_slot = normalize_slot_id(slot_id)
    for item in SlotStore(runtime_root).load_slots():
        if item.get("id") == target_slot:
            return item
    raise RelayError(f"slot not found: {target_slot}")


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
        write_auth_store(dest_auth, load_json(src_auth, {}))
        if SOURCE_MODELS.exists():
            shutil.copy2(SOURCE_MODELS, agent_dir / "models.json")
        else:
            ensure_slot_models(agent_dir)

        auth_info = extract_codex_profile_info(dest_auth)
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
                "emailLabel": info.get("emailLabel") or auth_info.get("email") or slot_id,
                "accountId": info.get("accountId") or auth_info.get("accountId"),
                "profileId": info.get("profileId") or auth_info.get("profileId"),
                "expires": info.get("expires") or auth_info.get("expires"),
                "email": auth_info.get("email"),
                "savedAt": info.get("savedAt") or utc_now_iso(),
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


def fetch_slot_usage_openclaw(agent_dir: str, profile: str) -> Dict[str, Any]:
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


def format_duration_compact(total_seconds: Any) -> str:
    seconds = max(0, int(float(total_seconds or 0)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: List[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts[:2])


def build_usage_line(left_pct: int, reset_after_seconds: Any) -> str:
    return f"{max(0, min(100, int(left_pct)))}% left · resets {format_duration_compact(reset_after_seconds)}"


def parse_codex_usage_data(data: Dict[str, Any]) -> Dict[str, Any]:
    rate_limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
    primary = rate_limit.get("primary_window") if isinstance(rate_limit.get("primary_window"), dict) else {}
    secondary = rate_limit.get("secondary_window") if isinstance(rate_limit.get("secondary_window"), dict) else {}

    primary_left = max(0, 100 - int(primary.get("used_percent", 0) or 0)) if primary else -1
    secondary_left = max(0, 100 - int(secondary.get("used_percent", 0) or 0)) if secondary else primary_left

    usage5h = build_usage_line(primary_left, primary.get("reset_after_seconds", 0)) if primary else ""
    usage_week = build_usage_line(secondary_left, secondary.get("reset_after_seconds", primary.get("reset_after_seconds", 0) if primary else 0)) if secondary_left >= 0 else ""

    return {
        "usage5h": usage5h,
        "usageWeek": usage_week,
        "fivePct": primary_left,
        "weekPct": secondary_left,
        "checkedAt": utc_now_iso(),
    }


def fetch_codex_usage_json(auth: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {auth['accessToken']}",
        "User-Agent": "CodexBar",
        "Accept": "application/json",
    }
    account_id = str(auth.get("accountId") or "").strip()
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = urllib.request.Request(DEFAULT_CODEX_USAGE_URL, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_slot_usage_codex_api(slot: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    try:
        auth = load_slot_codex_auth(slot)
        data = fetch_codex_usage_json(auth, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code in {401, 403}:
            auth = refresh_slot_codex_auth(slot, force=True)
            data = fetch_codex_usage_json(auth, timeout=timeout)
        else:
            raise RelayError(body or f"codex usage http {exc.code}")
    return parse_codex_usage_data(data)


def blank_usage() -> Dict[str, Any]:
    return {
        "usage5h": "",
        "usageWeek": "",
        "fivePct": -1,
        "weekPct": -1,
        "checkedAt": utc_now_iso(),
    }


def resolve_slot_usage(runtime_root: Path, profile: str, slot: Dict[str, Any]) -> Dict[str, Any]:
    backend = assert_supported_backend(runtime_root, "usage", purpose="usage")
    if backend == "openclaw":
        return fetch_slot_usage_openclaw(slot["agentDir"], profile)
    if backend == "codex-api":
        return fetch_slot_usage_codex_api(slot)
    if backend == "local-cache":
        usage = json.loads(json.dumps(slot.get("usage") or blank_usage()))
        usage.setdefault("checkedAt", utc_now_iso())
        return usage
    raise RelayError(f"usage backend '{backend}' belum didukung")


def refresh_usage(runtime_root: Path, profile: str, slot_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    assert_supported_backend(runtime_root, "usage", purpose="usage")
    store = SlotStore(runtime_root)
    slots = store.load_slots()
    changed: List[Dict[str, Any]] = []
    slot_filter = normalize_slot_id(slot_filter) if slot_filter else None
    for slot in slots:
        if slot_filter and slot["id"] != slot_filter:
            continue
        usage = resolve_slot_usage(runtime_root, profile, slot)
        slot["usage"] = usage
        slot.setdefault("sourceMeta", {})["usageFingerprint"] = usage_fingerprint(usage)
        changed.append({"id": slot["id"], **usage})
    store.save_slots(slots)
    return changed


def build_provisional_slot(runtime_root: Path, slot_id: str, prev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    prev = prev or {}
    slot_id = normalize_slot_id(slot_id)
    agent_dir = slot_agent_dir(runtime_root, slot_id)
    return {
        "id": slot_id,
        "agentDir": str(agent_dir),
        "authFile": str(agent_dir / "auth-profiles.json"),
        "modelDefault": prev.get("modelDefault", "gpt-5.4"),
        "usage": prev.get("usage") or blank_usage(),
    }


def finalize_slot_registration(runtime_root: Path, profile: str, slot_id: str, label: str, *, source_slot: Optional[str] = None) -> Dict[str, Any]:
    target_slot = normalize_slot_id(slot_id)
    existing_slots = SlotStore(runtime_root).load_slots()
    prev = next((item for item in existing_slots if item.get("id") == target_slot), {})
    provisional = build_provisional_slot(runtime_root, target_slot, prev=prev)
    try:
        usage = resolve_slot_usage(runtime_root, profile, provisional)
    except Exception:
        usage = prev.get("usage") or blank_usage()
    record = upsert_slot_record(
        runtime_root,
        target_slot,
        label,
        usage,
        enabled=prev.get("enabled", True),
        model_default=prev.get("modelDefault", "gpt-5.4"),
        runtime_meta=prev.get("runtime"),
        source_slot=source_slot if source_slot is not None else prev.get("sourceSlot"),
    )
    return save_slot_record(runtime_root, record)


def login_slot(runtime_root: Path, profile: str, slot_id: str, label: str) -> Dict[str, Any]:
    backend = assert_supported_backend(runtime_root, "auth", purpose="auth")
    setup_runtime(runtime_root, profile)
    slot_id = normalize_slot_id(slot_id)
    agent_dir = slot_agent_dir(runtime_root, slot_id)
    ensure_dir(agent_dir)
    ensure_slot_models(agent_dir)
    auth_file = agent_dir / "auth-profiles.json"

    if backend == "openclaw":
        env = {
            "OPENCLAW_AGENT_DIR": str(agent_dir),
            "OPENCLAW_HIDE_BANNER": "1",
            "OPENCLAW_SUPPRESS_NOTES": "1",
        }
        command = ["openclaw", "--profile", profile, "models", "auth", "login", "--provider", "openai-codex"]
        code = run_interactive_subprocess(command, env=env, cwd=str(minimal_workspace_path(runtime_root)))
        if code != 0:
            raise RelayError(f"slot login gagal untuk {slot_id} (exit {code})")
        if not auth_file.exists():
            copied = copy_profile_auth_into_slot(runtime_root, profile, slot_id)
            if copied:
                auth_file = copied
        if not auth_file.exists():
            raise RelayError(f"auth file tidak ditemukan setelah login: {auth_file}")
    elif backend == "native":
        credentials = login_openai_codex_native()
        write_auth_store(auth_file, build_codex_auth_profile(credentials))
    else:
        raise RelayError(f"auth backend '{backend}' belum didukung")

    return finalize_slot_registration(runtime_root, profile, slot_id, label)


def slot_auth_import_file(runtime_root: Path, profile: str, slot_id: str, label: str, auth_file: Path) -> Dict[str, Any]:
    setup_runtime(runtime_root, profile)
    slot_id = normalize_slot_id(slot_id)
    agent_dir = slot_agent_dir(runtime_root, slot_id)
    ensure_dir(agent_dir)
    ensure_slot_models(agent_dir)
    if not auth_file.exists():
        raise RelayError(f"auth file tidak ditemukan: {auth_file}")
    write_auth_store(agent_dir / "auth-profiles.json", load_json(auth_file, {}))
    return finalize_slot_registration(runtime_root, profile, slot_id, label)


def slot_auth_copy_profile(runtime_root: Path, profile: str, slot_id: str, label: str, source_profile: str, *, source_agent: Optional[str] = None) -> Dict[str, Any]:
    setup_runtime(runtime_root, profile)
    copied = copy_profile_auth_into_slot(runtime_root, source_profile, slot_id, source_agent=source_agent)
    if not copied:
        raise RelayError(f"auth file tidak ditemukan di profile sumber: {source_profile}")
    return finalize_slot_registration(runtime_root, profile, slot_id, label)


def slot_usage_set(runtime_root: Path, slot_id: str, usage5h: str, usageWeek: str) -> Dict[str, Any]:
    slot = get_slot_by_id(runtime_root, slot_id)
    usage = {
        "usage5h": usage5h,
        "usageWeek": usageWeek,
        "fivePct": parse_pct(usage5h),
        "weekPct": parse_pct(usageWeek),
        "checkedAt": utc_now_iso(),
    }
    slot["usage"] = usage
    slot.setdefault("sourceMeta", {})["usageFingerprint"] = usage_fingerprint(usage)
    return save_slot_record(runtime_root, slot)


def slot_usage_copy_main(runtime_root: Path, slot_id: str) -> Dict[str, Any]:
    slot = get_slot_by_id(runtime_root, slot_id)
    source_slots = load_source_slots().get("slots", {})
    fallback_lookup = ""
    normalized = normalize_slot_id(slot_id)
    if "-" in normalized:
        fallback_lookup = normalized.split("-", 1)[1]
    lookup = str(slot.get("sourceSlot") or fallback_lookup)
    info = source_slots.get(lookup)
    if not isinstance(info, dict):
        raise RelayError(f"source usage metadata tidak ditemukan untuk slot {lookup}")
    usage = {
        "usage5h": info.get("usage5h", ""),
        "usageWeek": info.get("usageWeek", ""),
        "fivePct": parse_pct(info.get("usage5h", "")),
        "weekPct": parse_pct(info.get("usageWeek", "")),
        "checkedAt": info.get("liveCheckedAt") or slot.get("usage", {}).get("checkedAt") or utc_now_iso(),
    }
    slot["usage"] = usage
    slot["sourceSlot"] = lookup
    slot.setdefault("sourceMeta", {})["usageFingerprint"] = usage_fingerprint(usage)
    return save_slot_record(runtime_root, slot)


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


def build_openai_oauth_auth_url(verifier: str, state: str, originator: str = OPENAI_CODEX_ORIGINATOR) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode("utf-8").rstrip("=")
    params = {
        "response_type": "code",
        "client_id": OPENAI_CODEX_CLIENT_ID,
        "redirect_uri": OPENAI_CODEX_REDIRECT_URI,
        "scope": OPENAI_CODEX_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    }
    return f"{OPENAI_CODEX_AUTHORIZE_URL}?{urlencode(params)}"


def parse_authorization_input(raw_input: str) -> Dict[str, Optional[str]]:
    value = str(raw_input or "").strip()
    if not value:
        return {"code": None, "state": None}

    try:
        parsed = urlparse(value)
        query = parse_qs(parsed.query or "")
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]
        if code:
            return {"code": str(code), "state": str(state) if state else None}
    except Exception:
        pass

    if "code=" in value:
        query = parse_qs(value)
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]
        return {"code": str(code) if code else None, "state": str(state) if state else None}

    if "#" in value and "http" not in value:
        code, _, state = value.partition("#")
        return {"code": code.strip() or None, "state": state.strip() or None}

    return {"code": value, "state": None}


def post_openai_oauth_token(payload: Dict[str, str], timeout: int = 60) -> Dict[str, Any]:
    request = urllib.request.Request(
        OPENAI_CODEX_TOKEN_URL,
        data=urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RelayError(f"oauth token request failed ({exc.code}): {body[:500]}")


def exchange_openai_authorization_code(code: str, verifier: str) -> Dict[str, Any]:
    data = post_openai_oauth_token({
        "grant_type": "authorization_code",
        "client_id": OPENAI_CODEX_CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": OPENAI_CODEX_REDIRECT_URI,
    })
    if not data.get("access_token") or not data.get("refresh_token") or not isinstance(data.get("expires_in"), (int, float)):
        raise RelayError("oauth token response missing required fields")
    return {
        "access": str(data.get("access_token")),
        "refresh": str(data.get("refresh_token")),
        "expires": int(time.time() * 1000 + float(data.get("expires_in")) * 1000),
        "accountId": extract_account_id_from_access_token(str(data.get("access_token"))),
        "email": extract_email_from_access_token(str(data.get("access_token"))),
    }


def refresh_openai_codex_token(refresh_token: str) -> Dict[str, Any]:
    if not str(refresh_token or "").strip():
        raise RelayError("refresh token kosong")
    data = post_openai_oauth_token({
        "grant_type": "refresh_token",
        "refresh_token": str(refresh_token),
        "client_id": OPENAI_CODEX_CLIENT_ID,
    })
    if not data.get("access_token") or not data.get("refresh_token") or not isinstance(data.get("expires_in"), (int, float)):
        raise RelayError("oauth refresh response missing required fields")
    access = str(data.get("access_token"))
    return {
        "access": access,
        "refresh": str(data.get("refresh_token")),
        "expires": int(time.time() * 1000 + float(data.get("expires_in")) * 1000),
        "accountId": extract_account_id_from_access_token(access),
        "email": extract_email_from_access_token(access),
    }


def login_openai_codex_native() -> Dict[str, Any]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8").rstrip("=")
    state = uuid.uuid4().hex
    auth_url = build_openai_oauth_auth_url(verifier, state)

    print("\nOpenAI Codex OAuth login (native)")
    print("Buka URL berikut lalu login. Setelah redirect, paste URL callback atau code ke terminal.")
    print(auth_url)

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    user_input = input("Paste authorization code / redirect URL: ").strip()
    parsed = parse_authorization_input(user_input)
    code = parsed.get("code")
    incoming_state = parsed.get("state")
    if incoming_state and incoming_state != state:
        raise RelayError("oauth state mismatch")
    if not code:
        raise RelayError("authorization code kosong")

    return exchange_openai_authorization_code(str(code), verifier)


def refresh_slot_codex_auth(slot: Dict[str, Any], *, force: bool = False, leeway_seconds: int = AUTH_REFRESH_LEEWAY_SECONDS) -> Dict[str, Any]:
    auth_file = Path(slot.get("authFile") or Path(slot.get("agentDir", "")) / "auth-profiles.json")
    auth_data = normalize_auth_store_payload(load_json(auth_file, {}))
    preferred_profile = slot.get("sourceMeta", {}).get("profileId") if isinstance(slot.get("sourceMeta"), dict) else None
    profile_id, profile = pick_openai_codex_profile(auth_data, preferred_id=preferred_profile)

    expires_ms = coerce_epoch_ms(profile.get("expires"))
    now_ms = int(time.time() * 1000)
    should_refresh = force
    if not should_refresh:
        if expires_ms is None:
            should_refresh = True
        else:
            should_refresh = now_ms + int(leeway_seconds * 1000) >= int(expires_ms)

    if not should_refresh:
        return {
            "profileId": profile_id,
            "accessToken": str(profile.get("access") or "").strip(),
            "refreshToken": str(profile.get("refresh") or "").strip(),
            "accountId": str(profile.get("accountId") or "").strip() or extract_account_id_from_access_token(str(profile.get("access") or "")),
            "expires": expires_ms,
            "email": str(profile.get("email") or "").strip() or extract_email_from_access_token(str(profile.get("access") or "")),
        }

    refreshed = refresh_openai_codex_token(str(profile.get("refresh") or ""))
    merged = dict(profile)
    merged.update({
        "provider": "openai-codex",
        "type": "oauth",
        "access": refreshed["access"],
        "refresh": refreshed["refresh"],
        "expires": refreshed["expires"],
        "accountId": refreshed["accountId"],
    })
    if refreshed.get("email"):
        merged["email"] = refreshed.get("email")

    auth_data.setdefault("profiles", {})
    auth_data["profiles"][profile_id] = merged
    write_auth_store(auth_file, auth_data)
    if isinstance(slot.get("sourceMeta"), dict):
        slot["sourceMeta"]["expires"] = refreshed["expires"]
        slot["sourceMeta"]["accountId"] = refreshed["accountId"]
        if refreshed.get("email"):
            slot["sourceMeta"]["email"] = refreshed.get("email")

    return {
        "profileId": profile_id,
        "accessToken": refreshed["access"],
        "refreshToken": refreshed["refresh"],
        "accountId": refreshed["accountId"],
        "expires": refreshed["expires"],
        "email": refreshed.get("email"),
    }


def normalize_model_for_codex(model: str) -> str:
    value = (model or "").strip()
    if not value:
        return "gpt-5.4"
    if "/" in value:
        value = value.rsplit("/", 1)[1]
    return value


def resolve_codex_responses_url(base_url: str) -> str:
    raw = (base_url or "").strip() or DEFAULT_CODEX_BASE_URL
    normalized = raw.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise RelayError("invalid codex access token format")
    body = parts[1]
    pad = "=" * ((4 - len(body) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(body + pad)
        payload = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise RelayError(f"failed decoding codex access token payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise RelayError("invalid codex access token payload")
    return payload


def extract_account_id_from_access_token(token: str) -> str:
    payload = decode_jwt_payload(token)
    claim = payload.get(JWT_CLAIM_PATH)
    if isinstance(claim, dict):
        account_id = str(claim.get("chatgpt_account_id") or "").strip()
        if account_id:
            return account_id
    fallback = str(payload.get("chatgpt_account_id") or "").strip()
    if fallback:
        return fallback
    raise RelayError("chatgpt_account_id not found in codex token")


def pick_openai_codex_profile(auth: Dict[str, Any], preferred_id: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    profiles = auth.get("profiles") if isinstance(auth.get("profiles"), dict) else {}
    if preferred_id and preferred_id in profiles:
        preferred = profiles.get(preferred_id)
        if isinstance(preferred, dict) and preferred.get("provider") == "openai-codex":
            return preferred_id, preferred
    for profile_id, profile in profiles.items():
        if isinstance(profile, dict) and profile.get("provider") == "openai-codex":
            return str(profile_id), profile
    raise RelayError("no openai-codex profile found in slot auth file")


def load_slot_codex_auth(slot: Dict[str, Any], *, refresh_if_needed: bool = True) -> Dict[str, Any]:
    auth_file = Path(slot.get("authFile") or Path(slot.get("agentDir", "")) / "auth-profiles.json")
    auth_data = normalize_auth_store_payload(load_json(auth_file, {}))
    preferred_profile = slot.get("sourceMeta", {}).get("profileId") if isinstance(slot.get("sourceMeta"), dict) else None
    profile_id, profile = pick_openai_codex_profile(auth_data, preferred_id=preferred_profile)
    access_token = str(profile.get("access") or "").strip()
    if not access_token:
        raise RelayError(f"missing codex access token in {auth_file}")

    expires_ms = coerce_epoch_ms(profile.get("expires"))
    if refresh_if_needed:
        now_ms = int(time.time() * 1000)
        if expires_ms is None or now_ms + AUTH_REFRESH_LEEWAY_SECONDS * 1000 >= int(expires_ms):
            return refresh_slot_codex_auth(slot, force=True)

    account_id = str(profile.get("accountId") or "").strip()
    if not account_id:
        account_id = extract_account_id_from_access_token(access_token)

    email = str(profile.get("email") or "").strip() or extract_email_from_access_token(access_token)
    return {
        "profileId": profile_id,
        "accessToken": access_token,
        "refreshToken": str(profile.get("refresh") or "").strip(),
        "accountId": account_id,
        "expires": expires_ms,
        "email": email,
    }


def get_slot_codex_base_url(slot: Dict[str, Any]) -> str:
    agent_dir = Path(slot.get("agentDir") or "")
    models_path = agent_dir / "models.json"
    models = load_json(models_path, {})
    providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    codex = providers.get("openai-codex") if isinstance(providers.get("openai-codex"), dict) else {}
    base_url = str(codex.get("baseUrl") or "").strip()
    return base_url or DEFAULT_CODEX_BASE_URL


def build_codex_headers(auth: Dict[str, Any], stream: bool, session_key: Optional[str] = None) -> Dict[str, str]:
    user_agent = f"pi ({platform.system().lower()} {platform.release()}; {platform.machine()})"
    headers = {
        "Authorization": f"Bearer {auth['accessToken']}",
        "chatgpt-account-id": str(auth.get("accountId") or ""),
        "OpenAI-Beta": "responses=experimental",
        "originator": "pi",
        "User-Agent": user_agent,
        "Accept": "text/event-stream" if stream else "application/json",
        "Content-Type": "application/json",
    }
    if session_key:
        headers["session_id"] = session_key
    return headers


def content_to_codex_blocks(content: Any, role: str) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    block_type = "output_text" if role == "assistant" else "input_text"

    def push_text(text: str) -> None:
        value = str(text or "")
        if value.strip():
            blocks.append({"type": block_type, "text": value})

    if isinstance(content, str):
        push_text(content)
        return blocks

    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                push_text(item)
                continue
            if not isinstance(item, dict):
                push_text(json.dumps(item, ensure_ascii=False))
                continue
            item_type = str(item.get("type") or "").lower()
            if item_type in {"text", "input_text", "output_text"}:
                push_text(str(item.get("text") or ""))
            elif item_type in {"image_url", "input_image"}:
                push_text("[image omitted in relay]")
            elif item_type == "input_file":
                push_text("[file omitted in relay]")
            elif isinstance(item.get("text"), str):
                push_text(str(item.get("text") or ""))
            else:
                push_text(json.dumps(item, ensure_ascii=False))
        return blocks

    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            push_text(str(content.get("text") or ""))
        else:
            push_text(json.dumps(content, ensure_ascii=False))
        return blocks

    push_text(json.dumps(content, ensure_ascii=False))
    return blocks


def build_codex_messages_from_openai_messages(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    instructions_parts: List[str] = []
    input_items: List[Dict[str, Any]] = []

    for message in messages:
        role = str(message.get("role") or "user").lower().strip() or "user"
        if role in {"system", "developer"}:
            text = flatten_content(message.get("content", "")).strip()
            if text:
                instructions_parts.append(text)
            continue

        if role == "tool":
            role = "user"
            text = flatten_content(message.get("content", "")).strip()
            content_value: Any = f"[tool]\n{text}" if text else "[tool]"
            blocks = content_to_codex_blocks(content_value, role="user")
            input_items.append({
                "type": "message",
                "role": "user",
                "content": blocks,
            })
            continue

        codex_role = "assistant" if role == "assistant" else "user"
        blocks = content_to_codex_blocks(message.get("content", ""), role=codex_role)
        if not blocks:
            continue
        input_items.append({
            "type": "message",
            "role": codex_role,
            "content": blocks,
        })

    instructions = "\n\n".join(part for part in instructions_parts if part).strip()
    return instructions, input_items


def ensure_reasoning_include(include: Any) -> List[str]:
    values: List[str] = []
    if isinstance(include, list):
        for item in include:
            text = str(item or "").strip()
            if text:
                values.append(text)
    if "reasoning.encrypted_content" not in values:
        values.append("reasoning.encrypted_content")
    return values


def translate_chat_completions_to_codex_payload(body: Dict[str, Any], model_hint: str, session_key: Optional[str]) -> Dict[str, Any]:
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise RelayError("messages is required")
    if body.get("tools"):
        raise RelayError("tools belum didukung pada runner codex-direct untuk /v1/chat/completions")

    instructions, input_items = build_codex_messages_from_openai_messages(messages)
    if not input_items:
        raise RelayError("missing user/assistant content in messages")

    payload: Dict[str, Any] = {
        "model": normalize_model_for_codex(str(body.get("model") or model_hint or "gpt-5.4")),
        "instructions": instructions or DEFAULT_CODEX_INSTRUCTIONS,
        "store": False,
        "stream": True,
        "input": input_items,
        "text": {"verbosity": "medium"},
        "include": ensure_reasoning_include(body.get("include")),
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    temperature = body.get("temperature")
    if isinstance(temperature, (int, float)):
        payload["temperature"] = float(temperature)
    if session_key:
        payload["prompt_cache_key"] = session_key
    return payload


def translate_responses_to_codex_payload(body: Dict[str, Any], model_hint: str, session_key: Optional[str]) -> Dict[str, Any]:
    messages = normalize_responses_input_to_messages(body)
    has_user = any(
        (str(msg.get("role") or "user").lower() in {"user", "tool"})
        and flatten_content(msg.get("content", "")).strip()
        for msg in messages
    )
    if not has_user:
        raise RelayError("Missing user message in `input`.")

    instructions, input_items = build_codex_messages_from_openai_messages(messages)
    if not input_items:
        raise RelayError("Missing user message in `input`.")

    text_cfg = body.get("text") if isinstance(body.get("text"), dict) else {"verbosity": "medium"}
    payload: Dict[str, Any] = {
        "model": normalize_model_for_codex(str(body.get("model") or model_hint or "gpt-5.4")),
        "instructions": instructions or str(body.get("instructions") or "").strip() or DEFAULT_CODEX_INSTRUCTIONS,
        "store": False,
        "stream": True,
        "input": input_items,
        "text": text_cfg,
        "include": ensure_reasoning_include(body.get("include")),
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    for key in ("temperature", "max_output_tokens", "reasoning", "tools", "tool_choice", "parallel_tool_calls", "metadata"):
        if key in body:
            payload[key] = body[key]

    cache_key = body.get("prompt_cache_key")
    if isinstance(cache_key, str) and cache_key.strip():
        payload["prompt_cache_key"] = cache_key.strip()
    elif session_key:
        payload["prompt_cache_key"] = session_key

    return payload


def iter_sse_events(resp) -> Iterable[Tuple[Optional[str], str]]:
    event_name: Optional[str] = None
    data_lines: List[str] = []
    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip() or None
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


def extract_text_from_codex_response_obj(response_obj: Dict[str, Any]) -> str:
    parts: List[str] = []
    output = response_obj.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "").lower() == "output_text":
                text = str(block.get("text") or "")
                if text:
                    parts.append(text)
    return "".join(parts).strip()


def collect_codex_stream_result(resp) -> Dict[str, Any]:
    collected: List[Dict[str, Any]] = []
    text_parts: List[str] = []
    response_created: Optional[Dict[str, Any]] = None
    response_completed: Optional[Dict[str, Any]] = None

    for _event_name, data in iter_sse_events(resp):
        if data.strip() == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        collected.append(obj)
        event_type = str(obj.get("type") or "")
        if event_type == "response.output_text.delta":
            delta = str(obj.get("delta") or "")
            if delta:
                text_parts.append(delta)
        elif event_type == "response.output_text.done" and not text_parts:
            done_text = str(obj.get("text") or "")
            if done_text:
                text_parts.append(done_text)
        elif event_type in {"response.created", "response.in_progress"}:
            response_obj = obj.get("response")
            if isinstance(response_obj, dict):
                response_created = response_obj
        elif event_type in {"response.completed", "response.done"}:
            response_obj = obj.get("response")
            if isinstance(response_obj, dict):
                response_completed = response_obj

    response_obj = response_completed or response_created or {}
    text = "".join(text_parts).strip()
    if not text and isinstance(response_obj, dict):
        text = extract_text_from_codex_response_obj(response_obj)

    return {
        "events": collected,
        "response": response_obj if isinstance(response_obj, dict) else {},
        "text": text,
    }


def open_codex_stream_request(
    slot: Dict[str, Any],
    payload: Dict[str, Any],
    timeout: int,
    session_key: Optional[str] = None,
    *,
    raise_retryable: bool = False,
):
    url = resolve_codex_responses_url(get_slot_codex_base_url(slot))
    auth = load_slot_codex_auth(slot, refresh_if_needed=True)

    for attempt in range(2):
        headers = build_codex_headers(auth, stream=True, session_key=session_key)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            error_body = exc.read()
            if exc.code in {401, 403} and attempt == 0:
                try:
                    auth = refresh_slot_codex_auth(slot, force=True)
                    continue
                except Exception:
                    pass
            if raise_retryable and should_retry_upstream_status(exc.code):
                raise RetryableUpstreamError(exc.code, error_body, dict(exc.headers))
            detail = error_body.decode("utf-8", errors="replace").strip()
            message = detail or f"codex upstream http {exc.code}"
            raise RelayError(message)
    raise RelayError("codex upstream auth retry exhausted")


def run_slot_prompt_codex_direct(slot: Dict[str, Any], prompt: str, timeout: int, session_key: Optional[str] = None) -> Dict[str, Any]:
    payload = {
        "model": normalize_model_for_codex(str(slot.get("modelDefault") or "gpt-5.4")),
        "instructions": DEFAULT_CODEX_INSTRUCTIONS,
        "store": False,
        "stream": True,
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        }],
        "text": {"verbosity": "medium"},
        "include": ["reasoning.encrypted_content"],
    }
    with open_codex_stream_request(slot, payload, timeout, session_key=session_key, raise_retryable=False) as resp:
        result = collect_codex_stream_result(resp)

    response_obj = result.get("response") if isinstance(result.get("response"), dict) else {}
    text = str(result.get("text") or "").strip()
    if not text:
        raise RelayError("no text payload returned from codex-direct")
    session_id = str(response_obj.get("id") or f"resp_{uuid.uuid4().hex}")
    return {
        "sessionId": session_id,
        "text": text,
        "raw": response_obj,
    }


def finalize_responses_payload_from_codex(result: Dict[str, Any], fallback_model: str) -> Dict[str, Any]:
    response_obj = result.get("response") if isinstance(result.get("response"), dict) else {}
    text = str(result.get("text") or "")

    if response_obj:
        payload = json.loads(json.dumps(response_obj))
        payload.setdefault("id", f"resp_{uuid.uuid4().hex}")
        payload.setdefault("object", "response")
        payload.setdefault("created_at", int(time.time()))
        payload.setdefault("status", "completed")
        payload.setdefault("model", normalize_model_for_codex(str(payload.get("model") or fallback_model or "gpt-5.4")))
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            payload["usage"] = create_empty_responses_usage()
        output = payload.get("output")
        if not isinstance(output, list) or not output:
            payload["output"] = [
                create_assistant_output_item(f"msg_{uuid.uuid4().hex}", text, status="completed")
            ]
        return payload

    return create_response_resource(
        f"resp_{uuid.uuid4().hex}",
        normalize_model_for_codex(str(fallback_model or "gpt-5.4")),
        "completed",
        [create_assistant_output_item(f"msg_{uuid.uuid4().hex}", text, status="completed")],
    )


def stream_codex_chat_chunks(handler: "RelayHandler", resp, model_hint: str) -> None:
    completion_id = f"chatcmpl-relay-{uuid.uuid4().hex[:12]}"
    model = normalize_model_for_codex(model_hint)
    role_sent = False
    delta_sent = False

    handler._set_sse_headers()
    for _event_name, data in iter_sse_events(resp):
        if data.strip() == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        event_type = str(obj.get("type") or "")
        if event_type in {"response.created", "response.in_progress", "response.completed", "response.done"}:
            response_obj = obj.get("response")
            if isinstance(response_obj, dict):
                upstream_id = str(response_obj.get("id") or "").strip()
                if upstream_id:
                    completion_id = upstream_id
                upstream_model = str(response_obj.get("model") or "").strip()
                if upstream_model:
                    model = normalize_model_for_codex(upstream_model)

        if event_type == "response.output_text.delta":
            delta = str(obj.get("delta") or "")
            if not delta:
                continue
            if not role_sent:
                handler._write_sse_data(build_chat_completion_chunk(completion_id, model, {"role": "assistant"}, None))
                role_sent = True
            handler._write_sse_data(build_chat_completion_chunk(completion_id, model, {"content": delta}, None))
            delta_sent = True
        elif event_type == "response.output_text.done" and not delta_sent:
            done_text = str(obj.get("text") or "")
            if done_text:
                if not role_sent:
                    handler._write_sse_data(build_chat_completion_chunk(completion_id, model, {"role": "assistant"}, None))
                    role_sent = True
                handler._write_sse_data(build_chat_completion_chunk(completion_id, model, {"content": done_text}, None))
        elif event_type in {"response.completed", "response.done"}:
            break

    if not role_sent:
        handler._write_sse_data(build_chat_completion_chunk(completion_id, model, {"role": "assistant"}, None))
    handler._write_sse_data(build_chat_completion_chunk(completion_id, model, {}, "stop"))
    handler._write_done()
    handler.close_connection = True


def stream_via_slot_codex_direct(handler: "RelayHandler", path: str, body: Dict[str, Any], slot: Dict[str, Any], payload_override: Optional[Dict[str, Any]] = None) -> bool:
    timeout = int(handler.server.relay_config.get("requestTimeoutSeconds", 180)) + 30
    session_key = handler.headers.get("x-openclaw-session-key")
    model_hint = str(body.get("model") or slot.get("modelDefault") or "gpt-5.4")

    if payload_override is not None:
        payload = json.loads(json.dumps(payload_override))
    else:
        if path == "/v1/chat/completions":
            payload = translate_chat_completions_to_codex_payload(body, model_hint=model_hint, session_key=session_key)
        else:
            payload = translate_responses_to_codex_payload(body, model_hint=model_hint, session_key=session_key)

    payload["model"] = normalize_model_for_codex(str(body.get("model") or payload.get("model") or model_hint))
    payload["stream"] = True

    with open_codex_stream_request(slot, payload, timeout, session_key=session_key, raise_retryable=True) as resp:
        client_stream = bool(body.get("stream"))
        if client_stream:
            if path == "/v1/chat/completions":
                stream_codex_chat_chunks(handler, resp, payload["model"])
                return True

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
            return True

        result = collect_codex_stream_result(resp)
        if path == "/v1/chat/completions":
            response_obj = result.get("response") if isinstance(result.get("response"), dict) else {}
            effective_model = normalize_model_for_codex(str(response_obj.get("model") or payload["model"] or model_hint))
            handler._send_json(200, build_chat_completion_payload(effective_model, str(result.get("text") or ""), {}))
            return True

        handler._send_json(200, finalize_responses_payload_from_codex(result, payload["model"]))
        return True


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
    assert_supported_backend(runtime_root, "runner", {"openclaw"}, purpose="runner")
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


def stream_via_slot_gateway(handler: "RelayHandler", path: str, body: Dict[str, Any], slot: Dict[str, Any]) -> bool:
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
        return True
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
        "belum didukung",
        "unsupported",
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


def get_runner_backend_from_config(config: Dict[str, Any]) -> str:
    return str(config.get("runner", {}).get("backend", "codex-direct") or "codex-direct")


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
    runner_backend = get_runner_backend_from_config(server.relay_config)

    slots = choose_slots(server.relay_config, server.slot_store.load_slots(), False, server.runtime_root)
    if not slots:
        raise RelayError("tidak ada slot Codex yang eligible")

    tried: List[str] = []
    last_error = None
    for slot in slots[:2]:
        tried.append(slot["id"])
        try:
            BUSY_TRACKER.acquire(slot["id"])
            if runner_backend == "codex-direct":
                result = run_slot_prompt_codex_direct(slot, prompt, timeout)
            elif runner_backend == "openclaw":
                result = run_slot_prompt(slot, prompt, server.relay_config["profile"], timeout, thinking, runner_agent, server.runtime_root)
            else:
                raise RelayError(f"runner backend '{runner_backend}' belum didukung")

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

        runner_backend = get_runner_backend_from_config(self.server.relay_config)
        if runner_backend not in {"openclaw", "codex-direct"}:
            raise RelayError(f"runner backend '{runner_backend}' belum didukung")

        payload_override: Optional[Dict[str, Any]] = None
        if runner_backend == "codex-direct":
            model_hint = str(body.get("model") or "gpt-5.4")
            session_key = self.headers.get("x-openclaw-session-key")
            if path == "/v1/chat/completions":
                payload_override = translate_chat_completions_to_codex_payload(body, model_hint=model_hint, session_key=session_key)
            else:
                payload_override = translate_responses_to_codex_payload(body, model_hint=model_hint, session_key=session_key)

        last_retry: Optional[RetryableUpstreamError] = None
        last_error = None
        for slot in slots[:2]:
            try:
                BUSY_TRACKER.acquire(slot["id"])
                if runner_backend == "codex-direct":
                    proxied_ok = stream_via_slot_codex_direct(self, path, body, slot, payload_override=payload_override)
                else:
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


def cmd_slot_auth_import_file(args: argparse.Namespace) -> int:
    slot = slot_auth_import_file(args.runtime_root, args.profile, args.slot, args.label, Path(args.auth_file))
    print(json.dumps({"slot": slot}, indent=2, ensure_ascii=False))
    return 0


def cmd_slot_auth_copy_profile(args: argparse.Namespace) -> int:
    slot = slot_auth_copy_profile(
        args.runtime_root,
        args.profile,
        args.slot,
        args.label,
        args.source_profile,
        source_agent=args.source_agent,
    )
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


def cmd_slot_usage_set(args: argparse.Namespace) -> int:
    slot = slot_usage_set(args.runtime_root, args.slot, args.usage5h, args.usageWeek)
    print(json.dumps({"slot": slot}, indent=2, ensure_ascii=False))
    return 0


def cmd_slot_usage_copy_main(args: argparse.Namespace) -> int:
    slot = slot_usage_copy_main(args.runtime_root, args.slot)
    print(json.dumps({"slot": slot}, indent=2, ensure_ascii=False))
    return 0


def cmd_refresh_usage(args: argparse.Namespace) -> int:
    changed = refresh_usage(args.runtime_root, args.profile, slot_filter=args.slot)
    print(json.dumps({"updated": changed}, indent=2, ensure_ascii=False))
    return 0


def cmd_dependency_map(args: argparse.Namespace) -> int:
    setup_runtime(args.runtime_root, args.profile)
    print(json.dumps({"dependencyMap": dependency_map(args.runtime_root)}, indent=2, ensure_ascii=False))
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

    timeout = int(config.get("requestTimeoutSeconds", 180))
    runner_backend = get_runner_backend_from_config(config)
    if runner_backend == "codex-direct":
        result = run_slot_prompt_codex_direct(
            slot=slot,
            prompt=args.prompt,
            timeout=timeout,
        )
    elif runner_backend == "openclaw":
        result = run_slot_prompt(
            slot=slot,
            prompt=args.prompt,
            profile=config["profile"],
            timeout=timeout,
            thinking=str(config.get("thinking", "off")),
            agent_id=str(config.get("runner", {}).get("agentId", "relay")),
            runtime_root=args.runtime_root,
        )
    else:
        raise SystemExit(f"runner backend belum didukung: {runner_backend}")

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

    p_slot_auth_import = sub.add_parser("slot-auth-import-file", help="Import a compatible auth-profiles.json into one slot")
    p_slot_auth_import.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_auth_import.add_argument("--label", required=True, help="Human label such as email/account name")
    p_slot_auth_import.add_argument("--auth-file", required=True, help="Path to source auth-profiles.json")
    p_slot_auth_import.set_defaults(func=cmd_slot_auth_import_file)

    p_slot_auth_copy = sub.add_parser("slot-auth-copy-profile", help="Copy Codex auth from another OpenClaw profile/agent into one slot")
    p_slot_auth_copy.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_auth_copy.add_argument("--label", required=True, help="Human label such as email/account name")
    p_slot_auth_copy.add_argument("--source-profile", required=True, help="OpenClaw profile name, e.g. codex-slot-relay")
    p_slot_auth_copy.add_argument("--source-agent", help="Optional source agent id (defaults to relay)")
    p_slot_auth_copy.set_defaults(func=cmd_slot_auth_copy_profile)

    p_slot_enable = sub.add_parser("slot-enable", help="Enable one relay-managed slot for selection")
    p_slot_enable.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_enable.set_defaults(func=cmd_slot_enable)

    p_slot_disable = sub.add_parser("slot-disable", help="Disable one relay-managed slot so it will not be selected")
    p_slot_disable.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_disable.set_defaults(func=cmd_slot_disable)

    p_slot_remove = sub.add_parser("slot-remove", help="Remove one relay-managed slot and delete its relay-local auth/runtime state")
    p_slot_remove.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_remove.set_defaults(func=cmd_slot_remove)

    p_slot_usage_set = sub.add_parser("slot-usage-set", help="Set slot usage snapshot manually (for local-cache workflow)")
    p_slot_usage_set.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_usage_set.add_argument("--usage5h", required=True, help="Primary window usage label")
    p_slot_usage_set.add_argument("--usageWeek", required=True, help="Secondary window usage label")
    p_slot_usage_set.set_defaults(func=cmd_slot_usage_set)

    p_slot_usage_copy = sub.add_parser("slot-usage-copy-main", help="Copy usage snapshot for one slot from main slot metadata")
    p_slot_usage_copy.add_argument("--slot", required=True, help="Slot id or number, e.g. 2 or slot-2")
    p_slot_usage_copy.set_defaults(func=cmd_slot_usage_copy_main)

    p_refresh = sub.add_parser("refresh-usage", help="Refresh usage cache from all or one relay-managed slot")
    p_refresh.add_argument("--slot")
    p_refresh.set_defaults(func=cmd_refresh_usage)

    p_dep = sub.add_parser("dependency-map", help="Show which relay subsystems still depend on which backend")
    p_dep.set_defaults(func=cmd_dependency_map)

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
