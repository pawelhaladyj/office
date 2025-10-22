# common/base.py
# Baza agentów: odbiór AclMessage, wysyłanie AclMessage,
# rejestr (auto-odkrywanie w procesie), obsługa zapytań o rejestr,
# CHARACTER (persona) z routowaniem po charakterze, historia IN/OUT,
# oraz opcjonalny autopilot AI (ENV: AGENT_AUTO_AI).

from __future__ import annotations

import os
import json
import time
import asyncio
import logging
from typing import Dict, Any, Optional, List, Tuple

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour
from spade.template import Template

from common.acl import AclMessage
from common.fipa import make_reply

# --- Opcjonalne moduły (nie wymagane do startu) ---
try:
    # AI autopilot (plan -> AclMessage) — jeżeli dostępne
    from common.llm import ai_respond_to_acl  # async def ai_respond_to_acl(agent, incoming: AclMessage, sender_jid: str) -> AclMessage
except Exception:
    ai_respond_to_acl = None

try:
    # Historia (IN/OUT) do promptu i debugowania
    from common.history import record  # def record(agent_name: str, direction: Literal["IN","OUT"], acl: AclMessage, peer_jid: str) -> None
except Exception:
    record = None

try:
    # Wybór najlepszego adresata po „character”
    from common.llm import pick_agent  # def pick_agent(prompt: str, registry: Dict[str, Dict[str, Any]]) -> Optional[str]
except Exception:
    pick_agent = None

# Ścieżka do pliku z migawką rejestru (podgląd z zewnątrz)
_REG_PATH = os.getenv("AGENTS_REG_PATH", "out/agents_registry.json")


class InboxBehaviour(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if not msg:
            return

        # Parsuj tylko, jeśli to wygląda na JSON-ACL
        md = dict(msg.metadata or {})
        lang = (md.get("language") or "").lower()
        body = (msg.body or "").lstrip()

        is_json_like = body.startswith("{") or body.startswith("[")
        if not (lang == "json" or is_json_like):
            # Nie-JSON: zostaw innym behawiorom (np. klasycznym FIPA u providera/koordynatora)
            return

        try:
            acl = AclMessage.from_spade(msg)
        except Exception as e:
            print(f"[{self.agent.name}] parse error: {e}")
            return
        
        try:
            from common.history import record as _rec
            _rec(self.agent.name, "IN", acl, str(msg.sender))
        except Exception:
            pass

        if acl.conversation_id:
            self.agent._last_sender_by_cid[acl.conversation_id] = str(msg.sender)

        # Obsługa zapytań o rejestr
        if (acl.ontology or "").startswith("office.registry") and acl.performative == "REQUEST":
            action = str((acl.payload or {}).get("action", "")).upper()
            if action in ("LIST", "DISCOVER"):
                snapshot = self.agent.registry_snapshot()
                out = make_reply(
                    acl, performative="INFORM",
                    payload={"agents": snapshot, "ts": int(time.time())},
                )
                await self.agent.send_acl(str(msg.sender), out)
                return

        await self.agent.handle_acl(acl, str(msg.sender))


class BaseACLAgent(Agent):
    """
    Wspólna baza:
    - auto-rejestracja (alias, klasa, charakter, protokoły/ontologie),
    - prosty rejestr współdzielony w procesie (+ snapshot do pliku),
    - odbiór AclMessage i przekazanie do handle_acl(),
    - obsługa „office.registry” (LIST/DISCOVER),
    - historia IN/OUT (jeśli obecny common.history),
    - prosty routing po „character” (AI lub heurystyka),
    - helpery: resolve(alias)->JID, last_sender_for(CID), alias(), character(), set_character().
    - opcjonalny autopilot AI: AGENT_AUTO_AI=1 (wtedy domyślne handle_acl odsyła odpowiedź z AI).
    """

    # Rejestr wspólny dla wszystkich instancji w tym samym procesie
    _REGISTRY: Dict[str, Dict[str, Any]] = {}
    _REG_LOCK = asyncio.Lock()

    def __init__(self, jid: str, password: str):
        super().__init__(jid, password)
        self._last_sender_by_cid: Dict[str, str] = {}  # CID -> JID
        self._character: str = ""  # ustawiane w setup() z ENV lub domyślne
        self._role: str = ""            # rola agenta: 'coordinator' | 'provider_simple' | ''
        self._pending: Dict[str, str] = {} 
        def _auto_ai_from_env() -> bool:
            return (os.getenv("AGENT_AUTO_AI", "0").strip().lower() in {"1", "true", "yes", "on"})

        # ... w BaseACLAgent.__init__ zamień:
        # self._auto_ai: bool = _AGENT_AUTO_AI_DEFAULT
        self._auto_ai: bool = _auto_ai_from_env()

    # --------- API rejestru ---------
    
    @staticmethod
    def _env_role_for(alias: str) -> str:
        # najpierw ROLE_<ALIAS>, potem AGENT_ROLE
        return (
            os.getenv(f"ROLE_{alias.upper()}") or
            os.getenv("AGENT_ROLE") or
            ""
        ).strip().lower()

    @classmethod
    def registry_snapshot(cls) -> Dict[str, Dict[str, Any]]:
        """Płytka kopia rejestru (bezpieczna do logowania/serializacji)."""
        return {k: dict(v) for k, v in cls._REGISTRY.items()}

    @classmethod
    async def _register(cls, alias: str, info: Dict[str, Any]) -> None:
        async with cls._REG_LOCK:
            cls._REGISTRY[alias] = info
            # Opcjonalny zrzut na dysk
            try:
                os.makedirs(os.path.dirname(_REG_PATH) or ".", exist_ok=True)
                with open(_REG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cls._REGISTRY, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logging.debug(f"[base] write registry file failed: {e}")

    def agents(self) -> Dict[str, Dict[str, Any]]:
        """Skrót: migawka rejestru z poziomu instancji."""
        return self.registry_snapshot()

    # --------- Alias/JID/resolve ---------

    @staticmethod
    def _guess_alias(jid_str: str) -> str:
        """Alias z lokalnej części JID (np. 'coordinator_office' → 'coordinator')."""
        local = jid_str.split("@", 1)[0]
        return local.split("_", 1)[0] if "_" in local else local

    def alias(self) -> str:
        return self._guess_alias(str(self.jid))

    def resolve(self, alias_or_jid: str) -> str:
        """
        Resolucja aliasu do JID:
        - jeśli wygląda jak JID (z '@') → zwróć jak jest,
        - jeśli alias jest w rejestrze → zwróć jego JID,
        - w ostateczności spróbuj zmiennej środowiskowej JID_<ALIAS>,
        - jeśli nic nie znajdziemy → zwróć wejście (niech błąd poleci przy send()).
        """
        if "@" in alias_or_jid:
            return alias_or_jid
        snapshot = self.registry_snapshot()
        if alias_or_jid in snapshot:
            return snapshot[alias_or_jid]["jid"]
        env_jid = os.getenv(f"JID_{alias_or_jid.upper()}")
        return env_jid or alias_or_jid

    def last_sender_for(self, conversation_id: str) -> Optional[str]:
        """Zwraca ostatniego nadawcę dla danego CID (jeśli znany)."""
        return self._last_sender_by_cid.get(conversation_id)

    # --------- Character (persona) ---------

    def character(self) -> str:
        """Tekstowy charakter agenta (persona)."""
        return self._character

    def set_character(self, text: str) -> None:
        """Ustaw charakter w locie i zaktualizuj rejestr."""
        self._character = (text or "").strip()
        alias = self.alias()

        async def _upd():
            info = self.registry_snapshot().get(alias, {})
            if info:
                info["character"] = self._character
                await self._register(alias, info)

        asyncio.create_task(_upd())

    @staticmethod
    def _env_character_for(alias: str) -> str:
        """
        Poszuka charakteru w ENV:
        - CHAR_<ALIAS> (np. CHAR_COORDINATOR, CHAR_PROVIDER, ...)
        - jeśli brak, to AGENT_CHARACTER (globalny fallback)
        - jeśli brak, domyślny opis tradycyjny.
        """
        return (
            os.getenv(f"CHAR_{alias.upper()}") or
            os.getenv("AGENT_CHARACTER") or
            "Tradycyjny, rzeczowy styl; rola ogólna."
        ).strip()

    # --------- Routing po charakterze ---------

    @staticmethod
    def _score_text_overlap(prompt: str, persona: str) -> int:
        """Prosta heurystyka: liczba wspólnych tokenów alfanum. (lowercase)."""
        import re
        tok = lambda s: set(re.findall(r"[a-z0-9]{3,}", s.lower()))
        a, b = tok(prompt), tok(persona)
        return len(a & b)

    def choose_agent_by_character(
        self,
        prompt: str,
        *,
        include_self: bool = False,
        allowed: Optional[List[str]] = None,
    ) -> Optional[str]:
        """
        Wybierz alias najlepszego adresata po 'character':
        - jeśli dostępne AI (common.llm.pick_agent) i klucz API → użyj AI,
        - inaczej heurystyka overlap.
        Parametr allowed — ogranicz do danej listy aliasów.
        """
        registry = self.registry_snapshot()
        if not registry:
            return None

        candidates: List[Tuple[str, Dict[str, Any]]] = []
        my_alias = self.alias()
        for alias, info in registry.items():
            if not include_self and alias == my_alias:
                continue
            if allowed and alias not in allowed:
                continue
            candidates.append((alias, info))

        if not candidates:
            return None

        # 1) AI, jeśli dostępne
        if pick_agent is not None:
            try:
                choice = pick_agent(prompt, {a: i for a, i in candidates})
                if choice and any(choice == a for a, _ in candidates):
                    return choice
            except Exception as e:
                logging.debug(f"[base] pick_agent failed, fallback used: {e}")

        # 2) Heurystyka
        scored = []
        for alias, info in candidates:
            persona = f"{info.get('character','')} {info.get('class','')}"
            scored.append((self._score_text_overlap(prompt, persona), alias))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return scored[0][1] if scored else None

    # --------- Cykl życia ---------

    async def setup(self):
        # 1) Uruchom wspólną skrzynkę odbiorczą AclMessage
        self.add_behaviour(InboxBehaviour(), Template())

        # 2) Auto-rejestracja w rejestrze procesu (+ charakter)
        alias = self.alias()
        self._character = self._env_character_for(alias)
        self._role = self._env_role_for(alias)

        info = {
            "alias": alias,
            "jid": str(self.jid),
            "class": self.__class__.__name__,
            "protocols": ["fipa-request"],
            "ontologies": ["office.demo", "office.registry"],
            "character": self._character,
            "role": self._role or "generic",    # <— DODAJ
            "ts": int(time.time()),
        }
        await self._register(alias, info)

        print(f"[{self.name}] up (alias={alias})")

    # --------- Domyślne handle_acl ---------

    async def handle_acl(self, acl: AclMessage, sender_jid: str):
        perf = (acl.performative or "").upper()
        cid = acl.conversation_id

        # --- tryb KOORDYNATORA: przyjmuje REQUEST od człowieka/AI, forwarduje do właściwego agenta,
        #     a wyniki (INFORM/FAILURE/REFUSE) odsyła inicjatorowi ---
        if self._role == "coordinator":
            if perf == "REQUEST":
                # zapamiętaj komu oddać wynik
                if cid:
                    self._pending[cid] = sender_jid

                # szybkie AGREE do inicjatora
                from common.fipa import make_reply
                agree = make_reply(acl, performative="AGREE", payload={"text": "przyjęto do realizacji"})
                await self.send_acl(sender_jid, agree)

                # wybór adresata po charakterze
                user_text = ""
                if isinstance(acl.payload, dict):
                    user_text = str(acl.payload.get("text") or acl.payload.get("user_text") or "")

                target_alias = self.choose_agent_by_character(user_text or "zamówienie pieczywa") or "provider"
                target_jid = self.resolve(target_alias)

                down_req = make_reply(acl, performative="REQUEST", payload=acl.payload or {"text": user_text})
                await self.send_acl(target_jid, down_req)
                logging.info("[%s] REQUEST → %s (%s)", self.alias(), target_alias, cid)
                return

            if perf in ("INFORM", "FAILURE", "REFUSE"):
                reply_to = self._pending.get(cid) or sender_jid
                from common.fipa import make_reply
                fwd = make_reply(acl, performative=perf, payload=acl.payload)
                await self.send_acl(reply_to, fwd)
                logging.info("[%s] %s (%s) → przekazano do inicjatora", self.alias(), perf, cid)
                if perf in ("INFORM", "FAILURE", "REFUSE"):
                    self._pending.pop(cid, None)
                return

            # AGREE od providera można zignorować albo forwardować — tu ignorujemy “szum”
            return

        # --- tryb PROSTY PROVIDER: na REQUEST → AGREE + po chwili INFORM ---
        if self._role == "provider_simple":
            if perf != "REQUEST":
                return
            from common.fipa import make_reply
            agree = make_reply(acl, performative="AGREE", payload={"text": "ok, realizuję"})
            await self.send_acl(sender_jid, agree)

            await asyncio.sleep(0.5)
            txt = "zamówienie zrealizowane"
            if isinstance(acl.payload, dict) and acl.payload.get("text"):
                txt = f"zrealizowano: {acl.payload['text']}"

            inform = make_reply(acl, performative="INFORM", payload={"text": txt})
            await self.send_acl(sender_jid, inform)
            return

        # --- fallback: autopilot AI, jeśli włączony i dostępny ---
        if self._auto_ai and ai_respond_to_acl is not None:
            try:
                # IN do historii (jeśli jest)
                try:
                    from common.history import record as _rec
                    _rec(self.name, "IN", acl, sender_jid)
                except Exception:
                    pass

                reply_acl = await ai_respond_to_acl(self, acl, sender_jid)
                await self.send_acl(sender_jid, reply_acl)
                return
            except Exception as e:
                logging.warning(f"[{self.name}] AI autopilot failed: {e}")

        # w pozostałych trybach brak domyślnej akcji
        return

    # --------- Wysyłka ---------

    async def send_acl(self, to_jid: str, acl: AclMessage):
        """Wysyłka AclMessage jako SPADE Message (JSON w body, FIPA-meta w metadata)
        przez OneShotBehaviour (używa Behaviour.send, które jest stabilne)."""
        spade_msg = acl.to_spade(to_jid, str(self.jid))

        class _SendOnce(OneShotBehaviour):
            def __init__(self, m):
                super().__init__()
                self._m = m
            async def run(self):
                await self.send(self._m)
                
        try:
            from common.history import record as _rec
            _rec(self.name, "OUT", acl, to_jid)
        except Exception:
            pass        

        self.add_behaviour(_SendOnce(spade_msg))

