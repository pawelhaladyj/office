# agents/human.py
import sys
import os
import json
import asyncio
import logging
from typing import Optional

from spade.behaviour import CyclicBehaviour, OneShotBehaviour
from spade.template import Template
from spade.message import Message

from common.base import BaseACLAgent
from common.acl import AclMessage
from common.fipa import (
    build_message,
    acl_msg,
    new_conv_id,
    iso_in,
)

HELP_TEXT = """
[human] komendy:
  help                              - pokaż pomoc
  registry                          - pokaż zarejestrowanych agentów (alias, klasa, character, jid)
  who                               - pokaż mapowanie ostatnich nadawców per conversation_id
  say <tekst...>                    - REQUEST JSON do najlepszego adresata (wybór po 'character')
  json <to> <PERF> <tekst...>       - wyślij JSON FIPA-ACL (to=alias albo JID)
  classic <to> <PERF> <tekst...>    - wyślij klasyczny SPADE+FIPA (to=alias albo JID)
  reply <CID> <PERF> <tekst...>     - odpowiedz ostatniemu nadawcy w wątku CID (JSON lub classic wg historii)
  quit                              - zakończ pętlę wejścia (agent nadal działa)
"""

class HumanAgent(BaseACLAgent):
    def __init__(self, jid: str, password: str):
        super().__init__(jid, password)
        # tryb per CID: "json" | "classic"
        self._last_mode_by_cid = {}

    async def setup(self):
        await super().setup()
        # Most klasyczny: odbiór nie-JSON (żeby nie ginęły stare komunikaty)
        self.add_behaviour(self.ClassicInbox(), Template(metadata={"language": "text"}))
        # Pętla wejścia z klawiatury (interaktywna obsługa)
        self.add_behaviour(self.ConsoleLoop())

    # ========== ODBIÓR NOWY: JSON FIPA-ACL ==========
    async def handle_acl(self, acl: AclMessage, sender: str):
        cid = acl.conversation_id or "(brak-cid)"
        self._last_mode_by_cid[cid] = "json"

        pretty = json.dumps(acl.model_dump(), ensure_ascii=False, indent=2)
        print(f"\n[human] << JSON from={sender} cid={cid} perf={acl.performative}\n{pretty}\n")

    # ========== ODBIÓR STARY: SPADE+FIPA ==========
    class ClassicInbox(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=1)
            if not msg:
                return
            # Niech Base nie próbuje parsować tego jako JSON
            cid = (msg.metadata or {}).get("conversation_id") or (msg.metadata or {}).get("conversation-id") or "(brak-cid)"
            self.agent._last_mode_by_cid[cid] = "classic"
            print(f"\n[human] << CLASSIC from={msg.sender} cid={cid} perf={(msg.metadata or {}).get('performative','')}\n{msg.body}\n")

    # ========== WYSYŁKA POMOCNICZA ==========
    async def _send_json(self, to_alias_or_jid: str, performative: str, text: str, cid: Optional[str] = None):
        to_jid = self.resolve(to_alias_or_jid)
        cid = cid or new_conv_id("human")
        msg = build_message(
            performative=performative.upper(),
            conversation_id=cid,
            reply_by=iso_in(20),
            payload={"text": text, "from": "human"},
        )
        await self.send_acl(to_jid, msg)
        self._last_mode_by_cid[cid] = "json"
        print(f"[human] >> JSON to={to_jid} cid={cid} perf={performative.upper()}  text={text}")
        return cid

    async def _send_classic(self, to_alias_or_jid: str, performative: str, text: str, cid: Optional[str] = None):
        to_jid = self.resolve(to_alias_or_jid)
        cid = cid or new_conv_id("human")
        msg = acl_msg(
            to=to_jid,
            performative=performative.upper(),
            content=text,
            conv_id=cid,
            reply_by=iso_in(20),
        )
        await self.send(msg)
        self._last_mode_by_cid[cid] = "classic"
        print(f"[human] >> CLASSIC to={to_jid} cid={cid} perf={performative.upper()}  text={text}")
        return cid

    # ========== PĘTLA KONSOLI ==========
    class ConsoleLoop(CyclicBehaviour):
        async def on_start(self):
            # Delikatna pauza, żeby rejestr się zapełnił
            for _ in range(8):
                if len(self.agent.agents()) > 1:
                    break
                await asyncio.sleep(0.25)
            print(HELP_TEXT.strip(), flush=True)

        async def run(self):
            loop = asyncio.get_running_loop()
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception:
                await asyncio.sleep(0.1)
                return

            if not line:
                await asyncio.sleep(0.1)
                return

            line = line.strip()
            if not line:
                return

            parts = line.split()
            cmd = parts[0].lower()

            if cmd in ("help", "?"):
                print(HELP_TEXT.strip())
                return

            if cmd == "registry":
                snap = self.agent.agents()
                if not snap:
                    print("[human] (rejestr pusty)")
                    return
                print("[human] rejestr agentów:")
                for alias, info in sorted(snap.items()):
                    print(f"  - {alias:12s} | {info.get('class','?'):16s} | {info.get('character','-')}\n    JID: {info.get('jid','?')}")
                return

            if cmd == "who":
                mp = self.agent._last_sender_by_cid
                if not mp:
                    print("[human] brak znanych rozmów (CID)")
                    return
                print("[human] ostatni nadawcy per CID:")
                for cid, j in mp.items():
                    print(f"  {cid}: {j}")
                return

            if cmd == "say":
                # say <tekst...>  -> wybór adresata po charakterze i REQUEST JSON
                if len(parts) < 2:
                    print("[human] użycie: say <tekst…>")
                    return
                text = line[len("say "):].strip()
                target_alias = self.agent.choose_agent_by_character(text) or "coordinator"
                await self.agent._send_json(target_alias, "REQUEST", text)
                return

            if cmd == "json":
                # json <to> <PERF> <tekst...>
                if len(parts) < 4:
                    print("[human] użycie: json <to> <PERF> <tekst…>")
                    return
                to = parts[1]
                perf = parts[2]
                text = " ".join(parts[3:])
                await self.agent._send_json(to, perf, text)
                return

            if cmd == "classic":
                # classic <to> <PERF> <tekst...>
                if len(parts) < 4:
                    print("[human] użycie: classic <to> <PERF> <tekst…>")
                    return
                to = parts[1]
                perf = parts[2]
                text = " ".join(parts[3:])
                await self.agent._send_classic(to, perf, text)
                return

            if cmd == "reply":
                # reply <CID> <PERF> <tekst...>  -> do ostatniego nadawcy w CID
                if len(parts) < 4:
                    print("[human] użycie: reply <CID> <PERF> <tekst…>")
                    return
                cid = parts[1]
                perf = parts[2]
                text = " ".join(parts[3:])
                to = self.agent.last_sender_for(cid)
                if not to:
                    print(f"[human] nie znam nadawcy dla CID={cid}")
                    return
                mode = self.agent._last_mode_by_cid.get(cid, "json")
                if mode == "classic":
                    await self.agent._send_classic(to, perf, text, cid=cid)
                else:
                    await self.agent._send_json(to, perf, text, cid=cid)
                return

            if cmd == "quit":
                print("[human] zakończono pętlę wejścia (agent nadal aktywny).")
                self.kill()
                return

            print(f"[human] nieznana komenda: {cmd}. Wpisz 'help'.")

