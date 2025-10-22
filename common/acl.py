# common/acl.py
from typing import Dict, Any, Optional
import json

from pydantic import BaseModel, Field, field_validator
from spade.message import Message

ALLOWED_PERFORMATIVES = {"REQUEST", "AGREE", "REFUSE", "INFORM", "FAILURE", "CANCEL"}

class AclMessage(BaseModel):
    """
    Kanoniczna reprezentacja komunikatu FIPA-ACL:
    - spójne metadane: performative, protocol, conversation_id, ontology, language, reply_by
    - ładunek aplikacyjny: payload (dict)
    - nadawca/odbiorca: sender/receiver (opcjonalne, ale uzupełniane przy transporcie)
    """
    performative: str
    conversation_id: str
    protocol: str = "fipa-request"
    ontology: str = "office.demo"
    language: str = "json"
    reply_by: Optional[str] = None        # ISO8601 UTC, opcjonalnie
    sender: Optional[str] = None          # <— DODANE
    receiver: Optional[str] = None        # <— DODANE
    payload: Dict[str, Any] = Field(default_factory=dict)

    # --- Normalizacja / walidacja pól ---
    @field_validator("performative")
    @classmethod
    def _perf_upper_and_allowed(cls, v: str) -> str:
        v_up = (v or "").upper()
        if v_up not in ALLOWED_PERFORMATIVES:
            raise ValueError(f"unsupported performative '{v}'")
        return v_up

    @field_validator("conversation_id")
    @classmethod
    def _conv_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("conversation_id is required")
        return v

    # --- Transport SPADE <-> model ---
    def to_spade(self, to_jid: str, sender_jid) -> Message:
        """
        Zamień AclMessage na SPADE Message z JSON w body i metadanymi FIPA.
        W body gwarantujemy obecność sender/receiver (nie mutujemy self).
        """
        body: Dict[str, Any] = self.model_dump()
        body.setdefault("sender", str(sender_jid))
        body.setdefault("receiver", str(to_jid))

        msg = Message(to=str(to_jid))
        msg.body = json.dumps(body, ensure_ascii=False)

        # SPADE wymaga czystego stringa:
        msg.sender = str(sender_jid)

        proto = getattr(self, "protocol", None) or "fipa-request"
        rb = getattr(self, "reply_by", None)
        md = {
            "performative": self.performative,
            "protocol": proto,
            "conversation_id": self.conversation_id,
            "ontology": self.ontology,
            "language": self.language,
        }
        if rb:
            md["reply_by"] = rb
        msg.metadata = md
        return msg

    @classmethod
    def from_spade(cls, msg: Message) -> "AclMessage":
        """
        Odtwarza AclMessage z wiadomości SPADE.
        Priorytet:
        1) body jako JSON (uzupełniamy brakujące pola metadanymi i nadawcą/odbiorcą),
        2) metadane + body jako tekst w payload["text"].
        Akceptuje conversation_id lub (legacy) conversation-id.
        """
        md = msg.metadata or {}
        conv = md.get("conversation_id") or md.get("conversation-id")
        perf = (md.get("performative") or "").upper()
        proto = md.get("protocol") or "fipa-request"
        onto = md.get("ontology") or "office.demo"
        lang = md.get("language") or "json"
        rby = md.get("reply_by")
        snd = str(msg.sender) if msg.sender else None
        rcv = str(msg.to) if msg.to else None

        # 1) Spróbuj body jako JSON i dopełnij brakujące pola
        if msg.body:
            try:
                obj = json.loads(msg.body)
                if not isinstance(obj, dict):
                    raise ValueError("body JSON is not an object")

                # Dopełnij wymagane pola jeśli nie ma ich w body
                obj.setdefault("performative", perf)
                obj.setdefault("conversation_id", conv)
                obj.setdefault("protocol", proto)
                obj.setdefault("ontology", onto)
                obj.setdefault("language", lang)
                obj.setdefault("reply_by", rby)
                obj.setdefault("sender", snd)
                obj.setdefault("receiver", rcv)
                if "payload" not in obj or not isinstance(obj["payload"], dict):
                    obj["payload"] = {"text": msg.body}

                return cls.model_validate(obj)
            except Exception:
                # lecimy do trybu 2)
                pass

        # 2) Fallback: metadane + surowe body jako payload.text
        payload: Dict[str, Any] = {}
        if msg.body:
            payload["text"] = msg.body

        return cls(
            performative=perf,
            conversation_id=conv,
            protocol=proto,
            ontology=onto,
            language=lang,
            reply_by=rby,
            sender=snd,
            receiver=rcv,
            payload=payload,
        )
