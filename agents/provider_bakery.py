import os, random
from agents.base import BaseACLAgent
from common.acl import AclMessage
from ai.llm import suggest

class ProviderAgent(BaseACLAgent):
    async def handle_acl(self, acl: AclMessage, sender: str):
        if acl.performative=="REQUEST" and acl.payload.get("type")=="RFP":
            price = min(acl.payload.get("budget",60), random.randint(45,70))
            offer = AclMessage(performative="INFORM", conversation_id=acl.conversation_id,
                               payload={"type":"OFFER","vendor":"Bakery","price":price,
                                        "note": suggest(f"Budget {acl.payload.get('budget')}, items {acl.payload.get('items')}")})
            await self.send_acl(str(sender), offer)
        elif acl.performative=="AGREE" and acl.payload.get("type")=="ORDER_ACCEPT":
            done = AclMessage(performative="CONFIRM", conversation_id=acl.conversation_id,
                              payload={"type":"ORDER_CONFIRMED","eta":"30min"})
            await self.send_acl(str(sender), done)
