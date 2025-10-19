# agents/provider_bakery.py
import os
import re
import asyncio
import logging

from spade.behaviour import CyclicBehaviour
from spade.template import Template

from common.base import BaseACLAgent
from common.fipa import is_fipa_request, perf, conv_id


class ProviderAgent(BaseACLAgent):
    async def setup(self):
        await super().setup()

        # Konfiguracja „po staremu” z ENV (bez hardcodu)
        self.item_keyword = os.getenv("PROVIDER_ITEM", "bułek")          # co dostarczamy
        try:
            self.default_qty = int(os.getenv("PROVIDER_DEFAULT_QTY", "6"))
        except Exception:
            self.default_qty = 6
        try:
            self.delay = float(os.getenv("PROVIDER_DELAY", "0.5"))       # sekundy
        except Exception:
            self.delay = 0.5

        # Reaguj na FIPA-Request (stary styl: tekst w body + metadata)
        self.add_behaviour(self.FipaResponder(), Template(metadata={"protocol": "fipa-request"}))

    class FipaResponder(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=5)
            if not msg:
                return
            if not is_fipa_request(msg) or perf(msg) != "REQUEST":
                return

            md = msg.metadata or {}
            cid = conv_id(msg)
            body = msg.body or ""
            logging.info("[provider] REQUEST (%s) od %s: %s", cid, str(msg.sender), body)

            # Proste kryterium: obsługujemy tylko jeśli treść dotyczy naszego asortymentu
            if self.agent.item_keyword not in body:
                refuse = msg.make_reply()
                refuse.set_metadata("performative", "REFUSE")
                refuse.set_metadata("conversation_id", md.get("conversation_id", cid))
                refuse.set_metadata("protocol", md.get("protocol", "fipa-request"))
                refuse.set_metadata("ontology", md.get("ontology", "office.demo"))
                refuse.set_metadata("language", md.get("language", "text"))
                refuse.body = "nie obsługuję tego zapytania"
                await self.send(refuse)
                return

            # AGREE
            agree = msg.make_reply()
            agree.set_metadata("performative", "AGREE")
            agree.set_metadata("conversation_id", md.get("conversation_id", cid))
            agree.set_metadata("protocol", md.get("protocol", "fipa-request"))
            agree.set_metadata("ontology", md.get("ontology", "office.demo"))
            agree.set_metadata("language", md.get("language", "text"))
            await self.send(agree)

            # „Realizacja” – opóźnienie kontrolowane ENV (powtarzalne demo)
            await asyncio.sleep(self.agent.delay)

            # Wyciągnij ilość z tekstu (pierwsza liczba), fallback: default_qty
            m = re.search(r"(\d+)", body)
            qty = int(m.group(1)) if m else self.agent.default_qty

            # INFORM z wynikiem
            inform = msg.make_reply()
            inform.set_metadata("performative", "INFORM")
            inform.set_metadata("conversation_id", md.get("conversation_id", cid))
            inform.set_metadata("protocol", md.get("protocol", "fipa-request"))
            inform.set_metadata("ontology", md.get("ontology", "office.demo"))
            inform.set_metadata("language", md.get("language", "text"))
            inform.body = f"zamówienie zrealizowane: {qty} {self.agent.item_keyword} świeżych"
            await self.send(inform)
