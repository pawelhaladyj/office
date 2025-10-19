import os
import asyncio
import logging
from spade.agent import Agent
from spade.behaviour import OneShotBehaviour, CyclicBehaviour
from spade.template import Template
from spade.message import Message
from common.fipa import acl_msg, iso_in, new_conv_id, perf


class CoordinatorAgent(Agent):
    def __init__(self, jid, password):
        super().__init__(jid, password)
        self.provider_jid = os.getenv("JID_PROVIDER") or "provider_office@xmpp.pawelhaladyj.pl"
        self.reporter_jid = os.getenv("JID_REPORTER") or "reporter_office@xmpp.pawelhaladyj.pl"

    async def setup(self):
        self.add_behaviour(self.KickOff())

    class KickOff(OneShotBehaviour):
        async def run(self):
            # Daj innym agentom sekundę na podpięcie behawiorów
            await asyncio.sleep(1.0)

            # Ustal rozmowę
            conv = new_conv_id("order")
            tpl = Template(metadata={"conversation_id": conv})

            # Podłącz behawior ODBIERAJĄCY odpowiedzi ZANIM wyślesz REQUEST
            waiter = self.agent.WaitReplies(conv, self.agent.reporter_jid)
            self.agent.add_behaviour(waiter, tpl)

            # Wyślij REQUEST do Provider
            req = acl_msg(
                to=self.agent.provider_jid,
                performative="REQUEST",
                content="poproszę 6 bułek",
                reply_by=iso_in(10),
                conv_id=conv,
            )
            await self.send(req)

            # AUDYT do Reportera
            audit = Message(to=self.agent.reporter_jid)
            audit.body = f"AUDIT: wysłano REQUEST -> {self.agent.provider_jid} ({conv})"
            md = req.metadata or {}
            audit.set_metadata("protocol", md.get("protocol", "fipa-request"))
            audit.set_metadata("conversation_id", md.get("conversation_id", conv))
            audit.set_metadata("ontology", md.get("ontology", "office.demo"))
            audit.set_metadata("language", md.get("language", "text"))
            audit.set_metadata("performative", "INFORM")
            if "reply_by" in md:
                audit.set_metadata("reply_by", md["reply_by"])
            await self.send(audit)

    class WaitReplies(CyclicBehaviour):
        def __init__(self, conv_id: str, reporter_jid: str):
            super().__init__()
            self.conv_id = conv_id
            self.reporter_jid = reporter_jid
            self.got_agree = False
            self.deadline = asyncio.get_event_loop().time() + 30.0  # 30s na całą rozmowę

        async def run(self):
            # Czekaj na dowolną wiadomość dopasowaną przez Template (conversation_id)
            msg = await self.receive(timeout=15)
            now = asyncio.get_event_loop().time()

            if not msg:
                # Timeout częściowy – jeśli minął całkowity deadline, kończymy
                if now >= self.deadline:
                    logging.warning("[coordinator] timeout na odpowiedzi (%s)", self.conv_id)
                    self.kill()
                return

            p = perf(msg)
            if p == "AGREE":
                self.got_agree = True
                logging.info("[coordinator] Provider AGREE (%s)", self.conv_id)
                return

            if p == "REFUSE":
                logging.info("[coordinator] Provider REFUSE (%s): %s", self.conv_id, msg.body or "")
                self.kill()
                return

            if p in ("INFORM", "FAILURE"):
                if p == "FAILURE":
                    logging.error("[coordinator] FAILURE (%s): %s", self.conv_id, msg.body or "")
                else:
                    logging.info("[coordinator] INFORM (%s): %s", self.conv_id, msg.body or "")

                # AUDYT końcowy do Reportera
                fin = Message(to=self.reporter_jid)
                fin.body = f"AUDIT: wynik zamówienia ({self.conv_id}): {msg.body or ''}"
                md2 = msg.metadata or {}
                fin.set_metadata("protocol", md2.get("protocol", "fipa-request"))
                fin.set_metadata("conversation_id", md2.get("conversation_id", self.conv_id))
                fin.set_metadata("ontology", md2.get("ontology", "office.demo"))
                fin.set_metadata("language", md2.get("language", "text"))
                fin.set_metadata("performative", "INFORM")
                await self.send(fin)

                # Kończymy po otrzymaniu wyniku
                self.kill()
                return

            # Inne performatywy ignorujemy (nie powinny wystąpić w tym protokole)
            return
