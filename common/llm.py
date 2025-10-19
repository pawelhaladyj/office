# common/llm.py
# Głowa AI: routing modelu, plan odpowiedzi (JSON), realizacja w pełny AclMessage.
from __future__ import annotations

import os, json, re, asyncio
from typing import Dict, Any, List, Optional, Tuple

import httpx

from common.acl import AclMessage
from common.fipa import make_reply, ensure_reply_by


# ---------------- Routing modelu ----------------

def select_model(purpose: str = "reply") -> str:
    """
    Prosty routing po ENV lub roli celu.
    ENV ma pierwszeństwo:
      - LLM_MODEL_REPLY (dla odpowiedzi)
      - LLM_MODEL_DEFAULT (gdy brak specyficznego)
    Domyślnie: gpt-4o-mini
    """
    if purpose == "reply":
        return os.getenv("LLM_MODEL_REPLY") or os.getenv("LLM_MODEL_DEFAULT") or "gpt-4o-mini"
    return os.getenv("LLM_MODEL_DEFAULT") or "gpt-4o-mini"


# ---------------- Pomocniki ----------------

def _summarize_acl(acl: AclMessage) -> str:
    preview = ""
    try:
        preview = json.dumps(acl.payload)[:400]
    except Exception:
        preview = str(acl.payload)[:400]
    return (
        f"INCOMING:\n"
        f"- performative: {acl.performative}\n"
        f"- conversation_id: {acl.conversation_id}\n"
        f"- protocol: {acl.protocol}\n"
        f"- ontology: {acl.ontology}\n"
        f"- language: {acl.language}\n"
        f"- reply_by: {acl.reply_by or 'null'}\n"
        f"- payload: {preview}\n"
    )


def _contract_text() -> str:
    return (
        "Zwróć WYŁĄCZNIE jeden obiekt JSON planu odpowiedzi.\n"
        "Klucze dokładnie:\n"
        "{\n"
        '  "performative": "REQUEST|AGREE|REFUSE|INFORM|FAILURE|CANCEL",\n'
        '  "payload": { ... },\n'
        '  "text": "krótki opis (<=120 znaków) lub null",\n'
        '  "reply_by": "ISO8601 UTC (np. 2025-10-19T08:58:46Z) lub null"\n'
        "}\n"
        "Zero komentarzy/Markdown. Tylko JSON.\n"
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

def _extract_json(blob: str) -> Dict[str, Any]:
    if not blob:
        raise ValueError("empty model output")
    m = _JSON_RE.search(blob)
    if not m:
        try:
            return json.loads(blob)
        except Exception:
            pass
        raise ValueError("no JSON object found")
    return json.loads(m.group(0))


def _coerce_plan(obj: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["performative"] = str(obj.get("performative", "")).upper()
    payload = obj.get("payload") or {}
    out["payload"] = payload if isinstance(payload, dict) else {"value": str(payload)}
    text = obj.get("text", None)
    out["text"] = (None if text in (None, "", "null") else str(text)[:120])
    rby = obj.get("reply_by")
    out["reply_by"] = (None if rby in (None, "", "null") else str(rby))
    return out


def _default_plan_for(acl: AclMessage) -> Dict[str, Any]:
    if acl.performative == "REQUEST":
        return {"performative": "AGREE", "payload": {"text": "przyjąłem zlecenie"}, "text": "przyjąłem", "reply_by": None}
    if acl.performative == "AGREE":
        return {"performative": "INFORM", "payload": {"result": "wykonano"}, "text": "zrobione", "reply_by": None}
    return {"performative": "INFORM", "payload": {"echo": acl.payload}, "text": "informacja", "reply_by": None}


# ---------------- Główny plan odpowiedzi (AI) ----------------

async def plan_reply(
    acl: AclMessage,
    role_system_prompt: str,
    context_last20: List[str],
    kb: Dict[str, Any],
) -> Dict[str, Any]:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return _default_plan_for(acl)

    model = select_model("reply")
    temperature = float(os.getenv("LLM_TEMPERATURE", "0.0"))
    timeout = float(os.getenv("LLM_TIMEOUT", "15"))

    system = (
        role_system_prompt.strip() + "\n\n"
        "Zasady:\n"
        "- mów krótko i rzeczowo, po staremu;\n"
        "- stosuj FIPA-ACL; tylko dozwolone performatywy;\n"
        "- 'text' maks. 120 znaków; jeśli zbędny, ustaw null.\n"
    )

    context_block = "\n".join(context_last20[-20:]) if context_last20 else "(brak kontekstu)"
    kb_block = json.dumps(kb) if kb else "{}"

    user = (
        _contract_text()
        + "\n"
        + _summarize_acl(acl)
        + "\nKONTEXT (ostatnie 20):\n"
        + context_block
        + "\n\nKB (wycinek):\n"
        + kb_block
        + "\n\nPodaj wyłącznie JSON planu odpowiedzi."
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "input": f"{system}\n\n{user}",
                    "temperature": temperature,
                },
            )
        data = resp.json()
        raw = data.get("output", [{"content": [{"text": ""}]}])[0]["content"][0].get("text", "")
        plan_json = _extract_json(raw)
        plan = _coerce_plan(plan_json)
        return plan
    except Exception:
        return _default_plan_for(acl)


# ---------------- Realizacja planu w pełny AclMessage ----------------

def realize_acl(incoming: AclMessage, plan: Dict[str, Any]) -> AclMessage:
    perf = str(plan.get("performative", "INFORM")).upper()
    payload = plan.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {"value": str(payload)}

    text = plan.get("text", None)
    if text and isinstance(text, str):
        payload.setdefault("text", text[:120])

    rby = plan.get("reply_by", None)
    if rby is not None:
        rby = ensure_reply_by(str(rby))

    out = make_reply(
        incoming,
        performative=perf,
        payload=payload,
        reply_by=rby,
        strict_transition=False,
    )
    return out


# ---------------- Shim kompatybilności: suggest(...) ----------------

def suggest(text: str, system: str = "You are concise.") -> str:
    """
    Shim dla starszego kodu (np. explorer_ai.py).
    Zwraca krótki tekst, bez FIPA – do luźnych sugestii.
    Blokujący (synchron.), ale prosty i zgodny z wcześniejszym użyciem.
    """
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return "OK. Budżet zaakceptowany. Proponuję kanapki + woda."
    try:
        r = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": select_model("reply"),
                "input": f"{system}\nUser: {text}",
                "temperature": float(os.getenv("LLM_TEMPERATURE", "0.0")),
            },
            timeout=float(os.getenv("LLM_TIMEOUT", "15")),
        )
        data = r.json()
        return data.get("output", [{"content": [{"text": "OK."}]}])[0]["content"][0].get("text", "OK.")
    except Exception:
        return "OK."
