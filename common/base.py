# common/base.py
# Fundament dla wszystkich agentów FIPA-ACL.
# Zapewnia: odbiór → autoryzacja → kontekst (20) → decyzja (rola/AI) → realizacja → wysyłka → audyt.
import os
import time
import logging
from typing import Optional, Deque, Dict, Any, List, Tuple
from collections import defaultdict, deque

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template

from common.acl import AclMessage
from common.llm import plan_reply, realize_acl  # AI: plan -> AclMessage (spójny FIPA)
from common.fipa import new_conv_id  # używane przy tworzeniu nowych rozmów (gdyby było potrzebne)


ALLOWED_PERFORMATIVES = {"REQUEST", "AGREE", "REFUSE", "INFORM", "FAILURE", "CANCEL"}


class InboxBehaviour(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if not msg:
            return

        sender = str(msg.sender)
        body = msg.body or ""

        try:
            acl = AclMessage.model_validate_json(body)
        except Exception as e:
            await self.agent.on_parse_error(raw_body=body, sender_jid=sender, exc=e)
            return

        try:
            await self.agent._receive_and_dispatch(acl, sender)
        except Exception as e:
            logging.exception("[%s] pipeline error: %s", self.agent.name, e)


class BaseACLAgent(Agent):
    """
    Abstrakcyjny agent FIPA-ACL.
    Klasy pochodne nadpisują: handle_acl(...), build_system_prompt(...), kb_lookup(...), route_model(...), validate_plan(...).
    """

    # Domyślna „polityka” (spójna we wszystkich agentach)
    DEFAULT_PROTOCOL = "fipa-request"
    DEFAULT_ONTOLOGY = "office.demo"
    DEFAULT_LANGUAGE = "json"

    CONTEXT_SIZE = int(os.getenv("ACL_CONTEXT_SIZE", "20"))           # ostatnie 20 wypowiedzi per conversation_id
    REPLY_TIMEOUT_SEC = int(os.getenv("ACL_REPLY_TIMEOUT_SEC", "30")) # domyślny deadline „reply_by”

    def __init__(self, jid: str, password: str):
        super().__init__(jid, password)
        # stan i konfiguracja wspólna
        self._context_store: Dict[str, Deque[Dict[str, Any]]] = defaultdict(lambda: deque(maxlen=self.CONTEXT_SIZE))
        self.reporter_jid: Optional[str] = os.getenv("JID_REPORTER") or None
        # lista dozwolonych nadawców (opcjonalnie)
        self.operator_allowlist: set[str] = set(
            j.strip() for j in os.getenv("OPERATOR_JIDS", "").split(",") if j.strip()
        )
        # DEV: zezwól na wszystkich, jeśli lista pusta
        self.accept_unknown_senders: bool = (os.getenv("ACL_ALLOW_ALL_SENDERS", "true").lower() == "true")

    # ---------- Cykl życia / rejestracja zachowań ----------
    async def setup(self):
        self.add_behaviour(InboxBehaviour(), Template())  # szablon uniwersalny – filtr robimy w handle_acl/validate
        logging.info("[%s] up", self.name)

    # ---------- GŁÓWNY PIPELINE (nie nadpisywać w dzieciach) ----------
    async def _receive_and_dispatch(self, acl: AclMessage, sender_jid: str) -> None:
        # 1) Autoryzacja
        if not self.allow_sender(sender_jid):
            await self.on_unauthorized(sender_jid=sender_jid, acl=acl)
            return

        # 2) Kontekst (wejście)
        self._push_context(direction="in", peer=sender_jid, acl=acl)

        # 3) Decyzja: rola (handle_acl) lub AI (plan_reply)
        out_acl: Optional[AclMessage] = await self.handle_acl(acl, sender_jid)

        if out_acl is None:
            # AI plan → realizacja → gotowy AclMessage
            system_prompt = await self.build_system_prompt()
            context_lines = self.get_context_lines(acl.conversation_id)
            kb = await self.kb_lookup(acl, context_lines) or {}

            plan = await plan_reply(
                acl=acl,
                role_system_prompt=system_prompt,
                context_last20=context_lines,
                kb=kb,
            )

            ok, why = await self.validate_plan(plan, acl)
            if not ok:
                await self.on_bad_plan(plan=plan, incoming_acl=acl, reason=why)
                return

            out_acl = realize_acl(incoming=acl, plan=plan)

        # 4) Wysyłka – domyślnie odsyłamy do nadawcy (dzieci mogą same wysyłać gdzie indziej w handle_acl)
        await self.send_acl(to_jid=sender_jid, acl=out_acl)

        # 5) Kontekst (wyjście) + audyt
        self._push_context(direction="out", peer=sender_jid, acl=out_acl)
        await self.audit(direction="out", acl=out_acl, peer=sender_jid)

    # ---------- Metody wspólne (nie trzeba nadpisywać) ----------
    async def send_acl(self, to_jid: str, acl: AclMessage):
        """Jednolita wysyłka AclMessage jako SPADE Message."""
        await self.send(acl.to_spade(to_jid, str(self.jid)))

    def _push_context(self, direction: str, peer: str, acl: AclMessage) -> None:
        """Zapis pojedynczej wypowiedzi do bufora kontekstu (ostatnie 20) dla danego conversation_id."""
        rec = {
            "ts": int(time.time()),
            "dir": direction,                      # 'in' lub 'out'
            "peer": peer,                          # z kim rozmawiamy
            "performative": acl.performative.upper(),
            "conversation_id": acl.conversation_id,
            "ontology": acl.ontology,
            "language": acl.language,
            "payload_preview": str(acl.payload)[:160],
        }
        self._context_store[acl.conversation_id].append(rec)

    def get_context_lines(self, conversation_id: str) -> List[str]:
        """Format kontekstu do podania AI (20 ostatnich)."""
        out: List[str] = []
        for r in list(self._context_store.get(conversation_id, [])):
            out.append(
                f"{r['ts']} | {r['dir']} | {r['peer']} | {r['performative']} | {r['payload_preview']}"
            )
        return out

    async def audit(self, direction: str, acl: AclMessage, peer: str) -> None:
        """Opcjonalny audyt – wyślij skrócony INFORM do Reportera."""
        if not self.reporter_jid:
            return
        try:
            audit_payload = {
                "direction": direction,
                "peer": peer,
                "performative": acl.performative,
                "conversation_id": acl.conversation_id,
                "ontology": acl.ontology,
                "language": acl.language,
                "payload": acl.payload,
            }
            audit_acl = AclMessage(
                performative="INFORM",
                conversation_id=acl.conversation_id,
                ontology=acl.ontology,
                language=acl.language,
                payload={"audit": audit_payload},
            )
            await self.send_acl(self.reporter_jid, audit_acl)
        except Exception as e:
            logging.warning("[%s] audit send failed: %s", self.name, e)

    # ---------- Hooki / kontrakty dla klas dziedziczących ----------
    async def handle_acl(self, acl: AclMessage, sender: str) -> Optional[AclMessage]:
        """
        Główna „logika roli”.
        Zwróć:
          - AclMessage → jeśli od razu wiesz, co wysłać (np. AGREE/REFUSE/INFORM),
          - None → jeśli baza ma uruchomić AI (plan_reply → realize_acl).
        """
        return None

    def allow_sender(self, sender_jid: str) -> bool:
        """Autoryzacja nadawcy (człowiek-operator itp.)."""
        if self.accept_unknown_senders:
            return True
        if not self.operator_allowlist:
            return True
        return sender_jid in self.operator_allowlist

    async def build_system_prompt(self) -> str:
        """Persona/styl roli do AI. Klasy pochodne mogą zwrócić własny prompt."""
        return f"You are '{self.name}', a disciplined FIPA-ACL agent. Be brief and precise."

    async def kb_lookup(self, acl: AclMessage, context_lines: List[str]) -> Dict[str, Any]:
        """Zwróć fragment KB (słownik) dla AI; domyślnie pusto."""
        return {}

    async def validate_plan(self, plan: Dict[str, Any], incoming_acl: AclMessage) -> Tuple[bool, str]:
        """
        Walidacja planu z AI (semantyka FIPA).
        Wymagane: performative ∈ ALLOWED_PERFORMATIVES; dopuszczalna odpowiedź względem incoming.
        """
        perf = str(plan.get("performative", "")).upper()
        if perf not in ALLOWED_PERFORMATIVES:
            return False, f"unsupported performative '{perf}'"
        # Prosta reguła: na REQUEST oczekujemy AGREE/REFUSE, a dopiero potem INFORM/FAILURE (baza tego nie wymusza twardo,
        # bo nie śledzimy stanu dialogu, ale można dodać surowsze zasady w klasach pochodnych).
        return True, "ok"

    # ---------- Obsługa błędów / sytuacji wyjątkowych ----------
    async def on_parse_error(self, raw_body: str, sender_jid: str, exc: Exception) -> None:
        logging.warning("[%s] parse error from %s: %s", self.name, sender_jid, exc)

    async def on_unauthorized(self, sender_jid: str, acl: AclMessage) -> None:
        logging.warning("[%s] unauthorized sender %s for conv %s", self.name, sender_jid, acl.conversation_id)

    async def on_bad_plan(self, plan: Dict[str, Any], incoming_acl: AclMessage, reason: str) -> None:
        logging.warning("[%s] bad AI plan for conv %s: %s | plan=%s", self.name, incoming_acl.conversation_id, reason, plan)
