import asyncio
import logging
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template
from common.fipa import is_fipa_request, perf, conv_id


class ProviderAgent(Agent):
    async def setup(self):
        # Reaguj tylko na rozmowy FIPA-Request
        self.add_behaviour(self.FipaResponder(), Template(metadata={"protocol": "fipa-request"}))

    class FipaResponder(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=5)
            if not msg:
                return
            if not is_fipa_request(msg) or perf(msg) != "REQUEST":
                return

            cid = conv_id(msg)
            logging.info("[provider] REQUEST (%s) od %s: %s", cid, str(msg.sender), msg.body)

            # Kryterium: obsługujemy tylko prośby zawierające słowo "bułek"
            if "bułek" not in (msg.body or ""):
                refuse = msg.make_reply()
                refuse.set_metadata("performative", "REFUSE")
                md = msg.metadata or {}
                refuse.set_metadata("conversation_id", cid)
                refuse.set_metadata("protocol", md.get("protocol", "fipa-request"))
                refuse.set_metadata("ontology", md.get("ontology", "office.demo"))
                refuse.set_metadata("language", md.get("language", "text"))
                refuse.body = "nie obsługuję tego zapytania"
                await self.send(refuse)
                return

            # AGREE
            agree = msg.make_reply()
            agree.set_metadata("performative", "AGREE")
            md = msg.metadata or {}
            agree.set_metadata("conversation_id", cid)
            agree.set_metadata("protocol", md.get("protocol", "fipa-request"))
            agree.set_metadata("ontology", md.get("ontology", "office.demo"))
            agree.set_metadata("language", md.get("language", "text"))
            await self.send(agree)

            # „Realizacja” — stałe opóźnienie dla powtarzalności dema
            await asyncio.sleep(0.5)

            # INFORM z wynikiem
            inform = msg.make_reply()
            inform.set_metadata("performative", "INFORM")
            inform.set_metadata("conversation_id", cid)
            inform.set_metadata("protocol", md.get("protocol", "fipa-request"))
            inform.set_metadata("ontology", md.get("ontology", "office.demo"))
            inform.set_metadata("language", md.get("language", "text"))
            inform.body = "zamówienie zrealizowane: 6 bułek świeżych"
            await self.send(inform)
