
# common/audit.py
from __future__ import annotations
import os, json, time, logging
from pathlib import Path
from typing import Any, Dict, Optional

# --- Logger ---
logger = logging.getLogger("common.audit")

# --- Storage ---
AUDIT_DIR = Path(os.getenv("AUDIT_DIR", "out/ai_audit")).resolve()

def _safe_mkdir(p: Path) -> None:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def _ts() -> int:
    return int(time.time())

# --- Helpers ---
_REDACT_KEYS = {"authorization", "api_key", "apikey", "token", "password", "secret", "bearer"}

def _redact(obj: Any) -> Any:
    try:
        if isinstance(obj, dict):
            return {k: ("***" if k.lower() in _REDACT_KEYS else _redact(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact(v) for v in obj]
    except Exception:
        pass
    return obj

def setup_logging(default_level: str = "INFO") -> None:
    """Ensure at least one handler; don't override app config if present."""
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        h.setFormatter(fmt)
        root.addHandler(h)
        try:
            level = getattr(logging, os.getenv("LOG_LEVEL", default_level).upper(), logging.INFO)
        except Exception:
            level = logging.INFO
        root.setLevel(level)

# --- File save (backwards compatible) ---
def save(agent_name: str, conversation_id: str, stage: str, payload: Dict[str, Any]) -> None:
    """
    Save a pretty JSON file with a stage of processing under out/ai_audit/<agent>.
    Keeps existing behaviour; add structured log line too.
    """
    base = AUDIT_DIR / agent_name
    _safe_mkdir(base)
    fname = f"{_ts()}-{conversation_id}-{stage}.json"
    path = base / fname
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        # Keep going even if disk is not writable
        pass
    # structured log
    try:
        logger.info(json.dumps({
            "kind": "AUDIT_STAGE",
            "agent": agent_name,
            "conversation_id": conversation_id,
            "stage": stage,
            "payload": payload
        }, ensure_ascii=False))
    except Exception:
        pass

# --- ACL logging ---
def log_acl(direction: str, acl_obj: Any, *, agent: Optional[str] = None, peer: Optional[str] = None,
            transport: Optional[str] = None, note: Optional[str] = None) -> None:
    """
    Log any AclMessage (or dict-like) in full.
    direction: 'IN' | 'OUT' | 'PARSED' | 'SPADE_IN' | 'SPADE_OUT' (free-form allowed)
    """
    try:
        if hasattr(acl_obj, "model_dump_json"):
            acl_json = json.loads(acl_obj.model_dump_json())
        elif hasattr(acl_obj, "model_dump"):
            acl_json = acl_obj.model_dump()
        elif isinstance(acl_obj, dict):
            acl_json = acl_obj
        else:
            acl_json = {"_repr": str(acl_obj)}
        logger.info(json.dumps({
            "kind": "ACL_LOG",
            "direction": direction,
            "agent": agent,
            "peer": peer,
            "transport": transport,
            "note": note,
            "acl": acl_json
        }, ensure_ascii=False))
    except Exception:
        pass

# --- AI I/O logging ---
def log_ai_request(agent: Optional[str], conversation_id: Optional[str], provider: str, model: str, body: Dict[str, Any],
                   *, endpoint: Optional[str] = None, headers: Optional[Dict[str, Any]] = None) -> None:
    try:
        logger.info(json.dumps({
            "kind": "AI_REQUEST",
            "agent": agent,
            "conversation_id": conversation_id,
            "provider": provider,
            "model": model,
            "endpoint": endpoint,
            "headers": _redact(headers or {}),
            "body": body,
        }, ensure_ascii=False))
    except Exception:
        pass

def log_ai_response(agent: Optional[str], conversation_id: Optional[str], provider: str, model: str,
                    status: int, data: Any) -> None:
    try:
        logger.info(json.dumps({
            "kind": "AI_RESPONSE",
            "agent": agent,
            "conversation_id": conversation_id,
            "provider": provider,
            "model": model,
            "status": status,
            "data": data,
        }, ensure_ascii=False))
    except Exception:
        pass
