import os, httpx
from typing import Optional

def suggest(text: str, system: str="You are concise.") -> str:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        # Fallback „po staremu”
        return "OK. Budżet zaakceptowany. Proponuję kanapki + woda."
    try:
        # Minimalny call do OpenAI Responses API (v1)
        r = httpx.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}"},
            json={"model":"gpt-4o-mini","input":f"{system}\nUser: {text}"}, timeout=15
        )
        return r.json().get("output",[{"content":[{"text":"OK."}]}])[0]["content"][0]["text"]
    except Exception:
        return "OK."
