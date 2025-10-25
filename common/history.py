# common/history.py
from __future__ import annotations

import logging

import logging

from collections import defaultdict, deque
from typing import Deque, Dict, Literal, List, Tuple, Optional
from common.acl import AclMessage

Direction = Literal["IN", "OUT"]

# Pamięć procesowa: agent_name -> deque[(Direction, AclMessage, peer_jid)]
def _default_limit() -> int:
    import os
logger = logging.getLogger("common.history")
    try:
        return max(1, int(os.getenv("ACL_HISTORY_LIMIT", "20")))
    except Exception:
        return 20

_STORE: Dict[str, Deque[Tuple[Direction, AclMessage, str]]] = defaultdict(
    lambda: deque(maxlen=_default_limit())
)

__all__ = [
    "record",
    "recent",
    "recent_thread",
    "format_for_prompt",
    "clear",
    "stats",
]


def record(agent_name: str, direction: Direction, acl: AclMessage, peer_jid: str) -> None:
    \"\"\"Dodaj wpis historii dla danego agenta.\"\"\"
    try:
        logger.info(json.dumps({
            "kind": "ACL_HISTORY",
            "agent": agent_name,
            "direction": direction,
            "peer": peer_jid,
            "acl": json.loads(acl.model_dump_json())
        }, ensure_ascii=False))
    except Exception:
        pass
    _STORE[agent_name].append((direction, acl, peer_jid))


def recent(agent_name: str, limit: int | None = None) -> List[Tuple[Direction, AclMessage, str]]:
    """Zwróć listę ostatnich wpisów (IN/OUT) dla agenta."""
    buf = _STORE.get(agent_name)
    if not buf:
        return []
    data = list(buf)
    return data if limit is None else data[-limit:]


def recent_thread(
    agent_name: str, conversation_id: str, limit: int | None = None
) -> List[Tuple[Direction, AclMessage, str]]:
    """Zwróć wpisy ograniczone do jednego conversation_id."""
    items = [
        (d, a, p)
        for (d, a, p) in _STORE.get(agent_name, [])
        if (a.conversation_id or "") == conversation_id
    ]
    return items if limit is None else items[-limit:]


def _row_from_entry(entry: Tuple[Direction, AclMessage, str]) -> dict:
    d, acl, peer = entry
    # „Bezpieczny” dump do promptu
    return {
        "direction": d,
        "performative": acl.performative,
        "conversation_id": acl.conversation_id,
        "sender": acl.sender or "",
        "receiver": acl.receiver or "",
        "peer": peer,
        "protocol": acl.protocol or "",
        "ontology": acl.ontology,
        "language": acl.language,
        "reply_by": acl.reply_by or None,
        "payload": dict(acl.payload or {}),
    }


def format_for_prompt(
    agent_name: str,
    limit: int | None = None,
    conversation_id: Optional[str] = None,
) -> str:
    """
    Zwraca historię w postaci JSON string (czytelny dla modelu).
    - limit: ograniczenie liczby wpisów (od końca)
    - conversation_id: gdy podane, zwraca tylko wątki z danym CID
    """
    entries = (
        recent_thread(agent_name, conversation_id, limit)
        if conversation_id
        else recent(agent_name, limit)
    )
    rows = [_row_from_entry(e) for e in entries]
    import json
    return json.dumps(rows, ensure_ascii=False, indent=2)


def clear(agent_name: str) -> None:
    """Wyczyść historię jednego agenta."""
    _STORE.pop(agent_name, None)


def stats() -> dict:
    """Proste statystyki pamięci: ile wpisów per agent i globalny limit."""
    return {
        "agents": {k: len(v) for k, v in _STORE.items()},
        "default_limit": _default_limit(),
    }