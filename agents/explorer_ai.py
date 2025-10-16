import sys
from agents.base import BaseACLAgent
from common.acl import AclMessage
from ai.llm import suggest

class ExplorerAgent(BaseACLAgent):
    async def on_start(self):
        # jednorazowe wysłanie zamówienia z wejścia CLI (albo domyślne)
        try:
            text = sys.argv[1]
        except IndexError:
            text = "Zamów 6 kanapek, budżet 60."
        cid = "conv-1"
        req = AclMessage(performative="REQUEST", conversation_id=cid,
                         payload={"type":"ORDER_REQUEST","items":6,"budget":60,
                                  "user_note": suggest(text)})
        await self.send_acl(self.coordinator_jid, req)

    async def setup(self):
        await super().setup()
        self.coordinator_jid = self.config.get("coordinator")

    async def handle_acl(self, acl: AclMessage, sender: str):
        pass  # Explorer tutaj tylko inicjuje
