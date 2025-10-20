# common/llm.py
# Głowa AI: routing modelu, plan odpowiedzi (JSON), realizacja w pełny AclMessage.
from __future__ import annotations

import os
import json
from typing import Any, Dict, Tuple

import httpx

from common.acl import AclMessage, ALLOWED_PERFORMATIVES
from common.fipa import ensure_reply_by, is_valid_transition

# Fallbacki na wypadek braku opcjonalnych modułów:
try:
    from common.history import format_for_prompt  # str(agent history JSON)
except Exception:
    def format_for_prompt(agent_name: str, conversation_id: str | None) -> str:
        return "[]"

try:
    from common.audit import save as audit_save  # audit_save(agent, conv_id, stage, payload_dict)
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


async def _call_openai(system: str, messages: list[dict]) -> Tuple[str, Dict[str, Any]]:
    """
    Woła OpenAI Responses API, zwraca (raw_text, raw_json_dict).
    """
    if not OPENAI_API_KEY:
        # fallback offline — zwróć prosty AGREE z echem
        dummy = {
            "performative": "AGREE",
            "conversation_id": "<fill-me>",
            "protocol": "fipa-request",
            "ontology": "office.demo",
            "language": "json",
            "reply_by": None,
            "sender": None,
            "receiver": None,
            "payload": {"text": "OK."}
        }
        raw = json.dumps(dummy, ensure_ascii=False)
        return raw, {"output": [{"content": [{"text": raw}]}]}

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "temperature": LLM_TEMPERATURE,
        "top_p": LLM_TOP_P,
        "max_output_tokens": LLM_MAX_OUTPUT_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "acl_message", "schema": ACL_JSON_SCHEMA, "strict": True}
        },
        "input": [
            {"role": "system", "content": system},
            *messages
        ],
    }
    url = f"{OPENAI_BASE_URL.rstrip('/')}/responses"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()

    # Najprostszy sposób na wyciągnięcie tekstu
    raw_text = ""
    try:
        raw_text = data.get("output", [])[0]["content"][0].get("text", "")
    except Exception:
        raw_text = data.get("response", "") or json.dumps(data)

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
    raw_text, raw_json = await _call_openai(system, messages)

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
    Zwraca krótki tekst podpowiedzi. Jeśli brak klucza API, zwraca stałą odpowiedź.
    """
    if not OPENAI_API_KEY:
        return "OK. Budżet zaakceptowany. Proponuję kanapki + woda."
    try:
        url = f"{OPENAI_BASE_URL.rstrip('/')}/responses"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "model": OPENAI_MODEL,
            "input": f"{system}\nUser: {text}",
            "temperature": LLM_TEMPERATURE,
        }
        r = httpx.post(url, headers=headers, json=body, timeout=15)
        data = r.json()
        # Wyciągnij najprostszy wariant tekstu
        try:
            out = data.get("output", [])[0]["content"][0].get("text", "")
        except Exception:
            out = data.get("response", "") or "OK."
        return (out or "OK.").strip()
    except Exception:
        return "OK."

