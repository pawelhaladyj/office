# common/audit.py
from __future__ import annotations
import os, json, time
from pathlib import Path
from typing import Any, Dict

AUDIT_DIR = Path(os.getenv("AUDIT_DIR", "out/ai_audit"))

def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _ts() -> int:
    return int(time.time())

def save(agent_name: str, conversation_id: str, stage: strS, payload: Dict[str, Any]) -> None:
    """
    Zapisz plik JSON z etapem przetwarzania:
    stage: 'incoming' | 'prompt' | 'raw_response' | 'validated' | 'error'
    """
    base = AUDIT_DIR / agent_name
    _safe_mkdir(base)
    fname = f"{_ts()}-{conversation_id}-{stage}.json"
    path = base / fname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
