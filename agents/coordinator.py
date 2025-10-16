import os, uuid
from agents.base import BaseACLAgent
from common.acl import AclMessage

JID_PROVIDER = os.getenv("JID_PROVIDER")
JID_REPORTER = os.getenv("JID_REPORTER")

class CoordinatorAgent(BaseACLAgent):
    async def handle_acl(self, acl: AclMessage, sender: str):
        if acl.performative == "REQUEST" and acl.payload.get("type") == "ORDER_REQUEST":
            cid = acl.conversation_id or str(uuid.uuid4())
            # prosty RFP do dostawcy
            rfp = AclMessage(performative="REQUEST", conversation_id=cid,
                             payload={"type":"RFP","items":acl.payload.get("items",6),
                                      "budget":acl.payload.get("budget",60)})
            await self.send_acl(JID_PROVIDER, rfp)
        elif acl.performative == "INFORM" and acl.payload.get("type") == "OFFER":
            # przyjmij pierwszą ofertę i potwierdź
            cid = acl.conversation_id
            accept = AclMessage(performative="AGREE", conversation_id=cid,
                                payload={"type":"ORDER_ACCEPT"})
            await self.send_acl(JID_PROVIDER, accept)
            # log do reportera
            log = AclMessage(performative="INFORM", conversation_id=cid,
                             payload={"type":"AUDIT","offer":acl.payload})
            await self.send_acl(JID_REPORTER, log)
