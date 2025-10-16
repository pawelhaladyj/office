from pydantic import BaseModel, Field
from typing import Dict, Any
from spade.message import Message

class AclMessage(BaseModel):
    performative: str = Field(..., pattern="REQUEST|INFORM|AGREE|CONFIRM|FAILURE|REFUSE")
    conversation_id: str
    ontology: str = "demo.catering"
    language: str = "json"
    payload: Dict[str, Any] = {}

    def to_spade(self, to_jid: str, sender: str) -> Message:
        msg = Message(to=to_jid)
        msg.body = self.model_dump_json()
        msg.sender = sender
        # FIPA-meta po staremu – w SPADE jako metadata:
        msg.metadata = {
            "performative": self.performative,
            "ontology": self.ontology,
            "language": self.language,
            "conversation-id": self.conversation_id,
        }
        return msg
