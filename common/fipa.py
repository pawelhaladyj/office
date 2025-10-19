# common/fipa.py
# Wspólne narzędzia FIPA-ACL: czas/ID, reguły przejść, budowa odpowiedzi
# + funkcje kompatybilności (acl_msg, perf, conv_id, is_fipa_request).

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from uuid import uuid4

from spade.message import Message
from common.acl import AclMessage, ALLOWED_PERFORMATIVES


# ---------- Czas i identyfikatory ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _to_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")

def iso_in(seconds: int) -> str:
    return _to_iso_utc(now_utc() + timedelta(seconds=seconds))

def new_conv_id(prefix: str = "conv") -> str:
    return f"{prefix}-{uuid4().hex[:8]}"

def ensure_reply_by(value: Optional[str], *, min_seconds: int = 5, default_seconds: int = 30) -> Optional[str]:
    if value is None:
        return iso_in(default_seconds)
    try:
        if value.endswith("Z"):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(value)
    except Exception:
        return iso_in(default_seconds)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    min_dt = now_utc() + timedelta(seconds=min_seconds)
    if dt < min_dt:
        dt = min_dt
    return _to_iso_utc(dt)


# ---------- Reguły przejść (prosty kanon) ----------

_REQUEST_REPLIES = {"AGREE", "REFUSE"}
_AFTER_AGREE = {"INFORM", "FAILURE"}

def is_valid_transition(incoming_perf: Optional[str], outgoing_perf: str) -> bool:
    out_up = outgoing_perf.upper()
    if incoming_perf is None:
        return out_up in ALLOWED_PERFORMATIVES
    inc_up = incoming_perf.upper()
    if inc_up == "REQUEST":
        return out_up in _REQUEST_REPLIES
    if inc_up == "AGREE":
        return out_up in _AFTER_AGREE
    return out_up in ALLOWED_PERFORMATIVES


# ---------- Budowa wiadomości (nowy styl: AclMessage) ----------

def build_message(
    *,
    performative: str,
    conversation_id: Optional[str] = None,
    protocol: str = "fipa-request",
    ontology: str = "office.demo",
    language: str = "json",
    reply_by: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    text: Optional[str] = None,
) -> AclMessage:
    if conversation_id is None:
        conversation_id = new_conv_id()
    data: Dict[str, Any] = dict(payload or {})
    if text is not None:
        data.setdefault("text", text)
    rb = ensure_reply_by(reply_by) if reply_by is not None else None
    return AclMessage(
        performative=performative.upper(),
        conversation_id=conversation_id,
        protocol=protocol,
        ontology=ontology,
        language=language,
        reply_by=rb,
        payload=data,
    )

def make_reply(
    incoming: AclMessage,
    *,
    performative: str,
    payload: Optional[Dict[str, Any]] = None,
    text: Optional[str] = None,
    reply_by: Optional[str] = None,
    protocol: Optional[str] = None,
    ontology: Optional[str] = None,
    language: Optional[str] = None,
    strict_transition: bool = False,
) -> AclMessage:
    perf_up = performative.upper()
    if perf_up not in ALLOWED_PERFORMATIVES:
        raise ValueError(f"unsupported performative '{performative}'")
    if strict_transition and not is_valid_transition(incoming.performative, perf_up):
        raise ValueError(f"invalid FIPA transition: {incoming.performative} -> {perf_up}")
    data: Dict[str, Any] = dict(payload or {})
    if text is not None:
        data.setdefault("text", text)
    rb = ensure_reply_by(reply_by) if reply_by is not None else None
    return AclMessage(
        performative=perf_up,
        conversation_id=incoming.conversation_id,
        protocol=protocol or incoming.protocol or "fipa-request",
        ontology=ontology or incoming.ontology or "office.demo",
        language=language or incoming.language or "json",
        reply_by=rb,
        payload=data,
    )


# ---------- Funkcje kompatybilności „po staremu” ----------
# (używane przez obecne pliki agentów; teraz importuj z common.fipa)

ACL_PROTOCOL_REQUEST = "fipa-request"

def acl_msg(
    to: str,
    performative: str,
    content: str = "",
    protocol: str = ACL_PROTOCOL_REQUEST,
    conv_id: str | None = None,
    reply_by: str | None = None,
    ontology: str = "office.demo",
    language: str = "text",
) -> Message:
    """
    Tworzy prosty SPADE Message (body = tekst) z metadanymi FIPA
    kompatybilnymi ze starszym kodem.
    """
    m = Message(to=to)
    m.body = content
    m.set_metadata("performative", performative.upper())
    m.set_metadata("protocol", protocol)
    m.set_metadata("conversation_id", conv_id or new_conv_id())
    m.set_metadata("ontology", ontology)
    m.set_metadata("language", language)
    if reply_by:
        m.set_metadata("reply_by", ensure_reply_by(reply_by))
    return m

def perf(msg: Message) -> str:
    return (msg.metadata or {}).get("performative", "").upper()

def conv_id(msg: Message) -> str:
    md = msg.metadata or {}
    return md.get("conversation_id") or md.get("conversation-id", "")

def protocol_of(msg: Message) -> str:
    return (msg.metadata or {}).get("protocol", "")

def is_fipa_request(msg: Message) -> bool:
    md = msg.metadata or {}
    return md.get("protocol") == ACL_PROTOCOL_REQUEST and (md.get("performative", "").upper() in ALLOWED_PERFORMATIVES)

# --- Alias kompatybilności dla starszego kodu ---
def protocol(msg):
    return protocol_of(msg)