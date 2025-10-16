import json, asyncio
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template
from common.acl import AclMessage

class InboxBehaviour(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if not msg: return
        try:
            acl = AclMessage.model_validate_json(msg.body)
            await self.agent.handle_acl(acl, str(msg.sender))
        except Exception as e:
            print(f"[{self.agent.name}] parse error: {e}")

class BaseACLAgent(Agent):
    async def setup(self):
        self.add_behaviour(InboxBehaviour(), Template())  # prosty szablon
        print(f"[{self.name}] up")

    async def handle_acl(self, acl: AclMessage, sender: str):
        pass  # nadpisywane w klasach pochodnych

    async def send_acl(self, to_jid: str, acl: AclMessage):
        await self.send(acl.to_spade(to_jid, self.jid))
