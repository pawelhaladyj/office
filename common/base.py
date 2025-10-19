# common/base.py
# Baza agentów: odbiór AclMessage, wysyłanie AclMessage,
# wspólny rejestr (auto-odkrywanie w procesie) + obsługa zapytań o rejestr,
# + CHARAKTER (persona) agenta oraz routing po charakterze (AI/fallback).

import os
import json
import time
import asyncio
import logging
from typing import Dict, Any, Optional, List, Tuple

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template

from common.acl import AclMessage
from common.fipa import make_reply

# Opcjonalny wybór przez AI (jeśli istnieje w common.llm)
try:
    from common.llm import pick_agent  # def pick_agent(prompt: str, registry: Dict[str, Dict[str, Any]]) -> Optional[str]
except Exception:
    pick_agent = None  # fallback na heurystykę

# Ścieżka do pliku z migawką rejestru (dla podglądu z zewnątrz)
_REG_PATH = os.getenv("AGENTS_REG_PATH", "out/agents_registry.json")


class InboxBehaviour(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if not msg:
            return
        try:
            acl = AclMessage.model_validate_json(msg.body)
        except Exception as e:
            print(f"[{self.agent.name}] parse error: {e}")
            return

        # Zapamiętaj ostatniego nadawcę dla CID (pomocne m.in. HumanAgent i reply)
        if acl.conversation_id:
            self.agent._last_sender_by_cid[acl.conversation_id] = str(msg.sender)

        # Wbudowana obsługa zapytań o rejestr (ontology=office.registry, action=LIST/DISCOVER)
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

        # Przekaż do logiki konkretnego agenta
        await self.agent.handle_acl(acl, str(msg.sender))


class BaseACLAgent(Agent):
    """
    Wspólna baza:
    - automatyczne dopisywanie się do rejestru przy starcie,
    - szybki dostęp do rejestru (w tym 'character' każdego agenta),
    - odbiór AclMessage i przekazywanie do handle_acl(),
    - możliwość odpowiedzi na zapytania o rejestr (FIPA-ACL),
    - routing po 'character' (AI jeśli dostępne, inaczej heurystyka),
    - pomocnicze: resolve(alias)->JID, last_sender_for(CID).
    """

    # Rejestr wspólny dla wszystkich instancji w tym samym procesie
    _REGISTRY: Dict[str, Dict[str, Any]] = {}
    _REG_LOCK = asyncio.Lock()

    def __init__(self, jid: str, password: str):
        super().__init__(jid, password)
        self._last_sender_by_cid: Dict[str, str] = {}  # CID -> JID
        self._character: str = ""  # ustawiane w setup() z ENV lub domyślne

    # --------- API rejestru (dla wszystkich agentów) ---------

    @classmethod
    def registry_snapshot(cls) -> Dict[str, Dict[str, Any]]:
        """Płytka kopia rejestru (bezpieczna do logowania/serializacji)."""
        return {k: dict(v) for k, v in cls._REGISTRY.items()}

    @classmethod
    async def _register(cls, alias: str, info: Dict[str, Any]) -> None:
        async with cls._REG_LOCK:
            cls._REGISTRY[alias] = info
            # Opcjonalny zrzut na dysk — „po staremu” do wglądu
            try:
                os.makedirs(os.path.dirname(_REG_PATH) or ".", exist_ok=True)
                with open(_REG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cls._REGISTRY, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logging.debug(f"[base] write registry file failed: {e}")

    def agents(self) -> Dict[str, Dict[str, Any]]:
        """Skrót: migawka rejestru z poziomu instancji."""
        return self.registry_snapshot()

    @staticmethod
    def _guess_alias(jid_str: str) -> str:
        """Alias z lokalnej części JID (np. 'coordinator_office' → 'coordinator')."""
        local = jid_str.split("@", 1)[0]
        return local.split("_", 1)[0] if "_" in local else local

    def resolve(self, alias_or_jid: str) -> str:
        """
        Resolucja aliasu do JID:
        - jeśli wygląda jak JID (z '@') → zwróć jak jest,
        - jeśli alias jest w rejestrze → zwróć jego JID,
        - w ostateczności spróbuj zmiennej środowiskowej JID_<ALIAS>,
        - jeśli nic nie znajdziemy → zwróć wejście (niech spadnie na błąd wysyłki).
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
        alias = self._guess_alias(str(self.jid))
        # bez await: aktualizacja zapisu do pliku może poczekać — ale trzymajmy konsekwencję:
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
        """
        Bardzo prosta heurystyka: liczba wspólnych słów kluczowych (lowercase, alfanum.).
        Zastępcza, gdy AI niedostępne.
        """
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

        # Zbierz kandydatów
        candidates: List[Tuple[str, Dict[str, Any]]] = []
        my_alias = self._guess_alias(str(self.jid))
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

        # 2) Heurystyka bez-ML
        scored = []
        for alias, info in candidates:
            persona = str(info.get("character", "")) + " " + str(info.get("class", ""))
            scored.append((self._score_text_overlap(prompt, persona), alias))
        scored.sort(key=lambda t: (-t[0], t[1]))  # najlepszy wynik, potem alfabetycznie
        return scored[0][1] if scored else None

    # --------- Cykl życia i komunikacja ---------

    async def setup(self):
        # 1) Uruchom wspólną skrzynkę odbiorczą AclMessage
        self.add_behaviour(InboxBehaviour(), Template())

        # 2) Auto-rejestracja w rejestrze procesu (+ charakter)
        alias = self._guess_alias(str(self.jid))
        # Wczytaj charakter z ENV
        self._character = self._env_character_for(alias)

        info = {
            "alias": alias,
            "jid": str(self.jid),
            "class": self.__class__.__name__,
            "protocols": ["fipa-request"],
            "ontologies": ["office.demo", "office.registry"],
            "character": self._character,
            "ts": int(time.time()),
        }
        await self._register(alias, info)

        print(f"[{self.name}] up (alias={alias})")

    async def handle_acl(self, acl: AclMessage, sender: str):
        """
        Domyślnie nic nie robi. Nadpisywane w klasach pochodnych.
        Jeśli potrzebujesz „po staremu” prostego loga – dopisz w klasie dziecka.
        """
        pass

    async def send_acl(self, to_jid: str, acl: AclMessage):
        """Wysyłka AclMessage jako SPADE Message (JSON w body, FIPA-metadane w metadata)."""
        await self.send(acl.to_spade(to_jid, self.jid))
