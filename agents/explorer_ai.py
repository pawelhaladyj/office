# agents/explorer_ai.py
import os
import sys
import asyncio
import logging

from spade.behaviour import OneShotBehaviour

from common.base import BaseACLAgent
from common.llm import suggest
from common.fipa import build_message, new_conv_id, iso_in


class ExplorerAgent(BaseACLAgent):
    async def setup(self):
        await super().setup()
        # Jednorazowe uruchomienie inicjujące rozmowę, gdy rejestr się zapełni
        self.add_behaviour(self.BootstrapOnce())

    class BootstrapOnce(OneShotBehaviour):
        async def run(self):
            # Poczekaj krótko, aż inni agenci zarejestrują się w rejestrze procesu
            for _ in range(8):
                if len(self.agent.agents()) > 1:
                    break
                await asyncio.sleep(0.25)

            # Treść od użytkownika (CLI) lub z ENV, klasyczny fallback
            try:
                user_text = sys.argv[1]
            except IndexError:
                user_text = os.getenv("EXPLORER_TEXT", "Zamów 6 kanapek, budżet 60.")

            items = int(os.getenv("EXPLORER_ITEMS", "6"))
            budget = int(os.getenv("EXPLORER_BUDGET", "60"))

            # Wybierz adresata po charakterze; domyślnie koordynator
            # Opis kierujemy do persony „koordynacja, nadzór, przydział zadań…”
            target_alias = self.agent.choose_agent_by_character(
                "koordynacja, nadzór, przydzielanie zadań, podejmowanie decyzji"
            ) or "coordinator"
            to_jid = self.agent.resolve(target_alias)

            # CID i reply-by (z zapasem), klasycznie i czytelnie
            cid = new_conv_id("req")
            rb = iso_in(12)

            # Komunikat FIPA-ACL (JSON body + metadane) zgodny z common/acl.py
            msg = build_message(
                performative="REQUEST",
                conversation_id=cid,
                reply_by=rb,
                payload={
                    "type": "ORDER_REQUEST",
                    "items": items,
                    "budget": budget,
                    "user_note": suggest(user_text),
                    "from": "explorer",
                },
                # ontology/protocol/language domyślne w build_message
            )

            await self.agent.send_acl(to_jid, msg)
            logging.info("[explorer] REQUEST (%s) -> %s: %s", cid, to_jid, user_text)

    async def handle_acl(self, acl, sender: str):
        # Explorer w tym wariancie tylko inicjuje; logujemy ewentualne odpowiedzi
        logging.info("[explorer] recv perf=%s cid=%s from=%s", acl.performative, acl.conversation_id, sender)
        # Tu można dodać dalsze sterowanie (np. decyzje doradcze) w kolejnych iteracjach projektu.
