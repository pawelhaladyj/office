# common/llm.py
# Głowa AI: routing modelu, plan odpowiedzi (JSON), realizacja w pełny AclMessage.
from __future__ import annotations

import os
import json
from typing import Any, Dict, Tuple

import httpx
import logging

from common.acl import AclMessage, ALLOWED_PERFORMATIVES
from common.fipa import ensure_reply_by, is_valid_transition

logger = logging.getLogger("common.llm")

# Fallbacki na wypadek braku opcjonalnych modułów:
try:
    from common.history import format_for_prompt  # str(agent history JSON)
except Exception:
    def format_for_prompt(agent_name: str, conversation_id: str | None) -> str:
        return "[]"

try:
    from common.audit import save as audit_save, log_ai_request, log_ai_response  # audit_save(agent, conv_id, stage, payload_dict)
except Exception:
    def audit_save(agent_name: str, conversation_id: str, stage: str, payload: Dict[str, Any]) -> None:
        pass


# --- Ustawienia z .env ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "1"))
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "700"))
ACL_REPLY_BY_SECONDS = int(os.getenv("ACL_REPLY_BY_SECONDS", "30"))

# --- JSON Schema, które model MUSI zwrócić ---
ACL_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["performative", "conversation_id", "ontology", "language", "protocol", "payload"],
    "properties": {
        "performative": {"type": "string", "enum": sorted(list(ALLOWED_PERFORMATIVES))},
        "conversation_id": {"type": "string", "minLength": 1},
        "protocol": {"type": "string"},
        "ontology": {"type": "string"},
        "language": {"type": "string", "const": "json"},
        "reply_by": {"type": ["string", "null"]},
        "sender": {"type": ["string", "null"]},
        "receiver": {"type": ["string", "null"]},
        "payload": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "text": {"type": ["string", "null"]},
                "tags": {"type": ["array"], "items": {"type": "string"}},
                "ontology_hints": {"type": ["array"], "items": {"type": "string"}},
            }
        }
    }
}


def _system_prompt(agent_name: str, agent_character: str, registry_excerpt: str) -> str:
    return f"""You are an autonomous XMPP agent speaking FIPA-ACL via JSON.
STRICT RULES:
- Output MUST be a single JSON object matching the provided JSON Schema (no extra text).
- Keep 'conversation_id' and 'protocol' from the incoming message.
- 'language' MUST be "json".
- Choose 'performative' according to minimal FIPA transitions:
  REQUEST -> AGREE or REFUSE; after AGREE -> INFORM or FAILURE.
- Do not invent sender/receiver: they will be set by the runtime. You may omit them or set null.
- If ontology is unclear, keep the same, and optionally suggest alternatives in payload.ontology_hints (array of strings).
- payload.text is the main natural-language answer.
- Be concise, factual, and actionable. No roleplay fluff.

Agent identity:
- name: {agent_name}
- character: {agent_character}

Known peers (alias -> character -> jid):
{registry_excerpt}
"""


def _build_messages(history_json: str, incoming_acl: AclMessage) -> list[dict]:
    return [
        {"role": "user", "content": f"HISTORY (last messages for this agent):\n{history_json}"},
        {"role": "user", "content": "INCOMING FIPA-ACL JSON:\n" + incoming_acl.model_dump_json(indent=2, ensure_ascii=False)},
        {"role": "user", "content": "Respond with EXACTLY one JSON object that matches the schema."}
    ]


async def _call_openai(agent_name: str, conversation_id: str, system: str, messages: list[dict]) -> Tuple[str, Dict[str, Any]]:
    """
    Asynchroniczne wywołanie Responses API.
    Zwraca (raw_text, raw_json). raw_text = zserializowany JSON odpowiedzi.
    """
    url = f"{OPENAI_BASE_URL.rstrip('/')}/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "input": messages,                       # ← zakładam, że messages już jest w formacie Responses API
        "response_format": {"type": "json_object"},
    }

    # — log pełnego requestu + etap „prompt” do pliku audytu —
    try:
        log_ai_request(agent_name, conversation_id, "openai", OPENAI_MODEL, body, endpoint=url, headers=headers)
        audit_save(agent_name, conversation_id, "prompt", {"system": system, "messages": messages, "http_body": body})
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=body)

    # — odczyt odpowiedzi jako JSON —
    try:
        data: Dict[str, Any] = resp.json()
    except Exception:
        data = {"_non_json_body": resp.text}

    # — log pełnej odpowiedzi —
    try:
        log_ai_response(agent_name, conversation_id, "openai", OPENAI_MODEL, int(getattr(resp, "status_code", 0) or 0), data)
    except Exception:
        pass

    raw_text = json.dumps(data, ensure_ascii=False)
    return raw_text, data

async def ai_respond_to_acl(
    agent,
    incoming: AclMessage,
    incoming_sender_jid: str,
) -> AclMessage:
    """
    Zbuduj prompt, zawołaj LLM, zwaliduj i zwróć AclMessage (bez ustawionych sender/receiver).
    """
    agent_name = getattr(agent, "name", "agent")
    agent_character = getattr(agent, "character", "concise, helpful, task-oriented.")
    registry = []
    if hasattr(agent, "get_registry_snapshot"):
        snap = agent.get_registry_snapshot()
        for alias, meta in snap.items():
            registry.append({"alias": alias, "character": meta.get("character", ""), "jid": meta.get("jid", "")})
    registry_excerpt = json.dumps(registry, ensure_ascii=False, indent=2)

    history_json = format_for_prompt(agent_name, None)

    # Audyt: wejście
    audit_save(agent_name, incoming.conversation_id, "incoming", {
        "incoming_acl": json.loads(incoming.model_dump_json())
    })

    system = _system_prompt(agent_name, agent_character, registry_excerpt)
    messages = _build_messages(history_json, incoming)

    # Audyt: prompt
    audit_save(agent_name, incoming.conversation_id, "prompt", {
        "system": system,
        "messages": messages,
    })

    # Call LLM
    raw_text, raw_json = await _call_openai(agent_name, incoming.conversation_id, system, messages)

    # Audyt: surowa odpowiedź
    audit_save(agent_name, incoming.conversation_id, "raw_response", {
        "raw_text": raw_text,
        "raw_json": raw_json
    })

    # Parsowanie JSON
    try:
        obj = json.loads(raw_text)
    except Exception as e:
        refuse = AclMessage(
            performative="REFUSE",
            conversation_id=incoming.conversation_id,
            protocol=incoming.protocol or "fipa-request",
            ontology=incoming.ontology,
            language="json",
            payload={"text": "Model returned non-JSON response", "error": str(e)},
        )
        audit_save(agent_name, incoming.conversation_id, "error", {"reason": "non_json", "detail": str(e)})
        return refuse

    # Walidacja + dopięcie reply_by
    try:
        if not obj.get("reply_by"):
            obj["reply_by"] = ensure_reply_by(None)  # +30s
        obj["conversation_id"] = incoming.conversation_id
        obj["protocol"] = incoming.protocol or "fipa-request"
        obj["ontology"] = obj.get("ontology") or incoming.ontology
        obj["language"] = "json"
        obj["sender"] = None
        obj["receiver"] = None

        answer = AclMessage.model_validate(obj)

        if not is_valid_transition(incoming.performative, answer.performative):
            answer = AclMessage(
                performative="REFUSE",
                conversation_id=incoming.conversation_id,
                protocol=incoming.protocol or "fipa-request",
                ontology=incoming.ontology,
                language="json",
                payload={"text": f"Invalid transition {incoming.performative} -> {obj.get('performative')}, refusing."}
            )

        audit_save(agent_name, incoming.conversation_id, "validated", json.loads(answer.model_dump_json()))
        return answer

    except Exception as e:
        refuse = AclMessage(
            performative="REFUSE",
            conversation_id=incoming.conversation_id,
            protocol=incoming.protocol or "fipa-request",
            ontology=incoming.ontology,
            language="json",
            payload={"text": "Validation error", "error": str(e)},
        )
        audit_save(agent_name, incoming.conversation_id, "error", {"reason": "validation", "detail": str(e)})
        return refuse
    
# --- Shim kompatybilności dla starszego kodu (np. agents/explorer_ai.py) ---
def suggest(text: str, system: str = "You are concise.") -> str:
    """
    Prosty prompt pomocniczy. Zwraca tekst.
    Loguje pełne body żądania i pełną odpowiedź.
    """
    if not OPENAI_API_KEY:
        # fallback – brak klucza: zwróć wejście lub skrót zgodnie z Twoją dotychczasową logiką
        return text

    url = f"{OPENAI_BASE_URL.rstrip('/')}/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "input": [
            {"role": "system", "content": [{"type": "text", "text": system}]},
            {"role": "user",   "content": [{"type": "text", "text": text}]},
        ],
        # jeśli chcesz „czysty” tekst z Responses API:
        "response_format": {"type": "text"},
    }

    # — log pełnego requestu —
    try:
        log_ai_request(None, None, "openai", OPENAI_MODEL, body, endpoint=url, headers=headers)
    except Exception:
        pass

    r = httpx.post(url, headers=headers, json=body, timeout=15)

    # — zczytanie i zalogowanie odpowiedzi —
    try:
        data = r.json()
    except Exception:
        data = {"_non_json_body": r.text}

    try:
        log_ai_response(None, None, "openai", OPENAI_MODEL, r.status_code, data)
    except Exception:
        pass

    # — ekstrakcja tekstu (dostosowana do Responses API) —
    out = None
    # próba wg nowego schematu:
    try:
        out = data["output"][0]["content"][0]["text"]
    except Exception:
        # inne ścieżki, zależnie od providera/formatu
        out = data.get("content") or data.get("response") or str(data)

    return str(out) if out is not None else ""
