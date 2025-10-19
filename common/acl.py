# common/acl.py
from typing import Dict, Any, Optional

from pydantic import BaseModel, Field, field_validator
from spade.message import Message

ALLOWED_PERFORMATIVES = {"REQUEST", "AGREE", "REFUSE", "INFORM", "FAILURE", "CANCEL"}

class AclMessage(BaseModel):
    """
    Kanoniczna reprezentacja komunikatu FIPA-ACL:
    - spójne metadane: performative, protocol, conversation_id, ontology, language, reply_by
    - ładunek aplikacyjny: payload (dict)
    """
    performative: str
    conversation_id: str
    protocol: str = "fipa-request"
    ontology: str = "office.demo"
    language: str = "json"
    reply_by: Optional[str] = None        # ISO8601 UTC, opcjonalnie
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
    def to_spade(self, to_jid: str, sender_jid: str) -> Message:
        """
        Konwersja do wiadomości SPADE:
        - body: cały AclMessage jako JSON (model_dump_json)
        - metadata: spójne klucze z podkreślnikiem
        """
        msg = Message(to=to_jid)
        msg.body = self.model_dump_json()
        msg.sender = sender_jid
        msg.metadata = {
            "performative": self.performative,
            "protocol": self.protocol,
            "conversation_id": self.conversation_id,
            "ontology": self.ontology,
            "language": self.language,
        }
        if self.reply_by:
            msg.metadata["reply_by"] = self.reply_by
        return msg

    @classmethod
    def from_spade(cls, msg: Message) -> "AclMessage":
        """
        Odtwarza AclMessage z wiadomości SPADE.
        Priorytet:
        1) body jako JSON zgodny z AclMessage
        2) metadane + body jako tekst w payload["text"]
        Akceptuje conversation_id lub (legacy) conversation-id.
        """
        # 1) Spróbuj JSON z body
        if msg.body:
            try:
                return cls.model_validate_json(msg.body)
            except Exception:
                pass

        md = msg.metadata or {}
        conv = md.get("conversation_id") or md.get("conversation-id")
        perf = (md.get("performative") or "").upper()
        proto = md.get("protocol") or "fipa-request"
        onto = md.get("ontology") or "office.demo"
        lang = md.get("language") or "json"
        rby = md.get("reply_by")

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
            payload=payload,
        )
