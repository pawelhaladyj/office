# agents/reporter.py
import os
import json
import time
import logging
import asyncio

from spade.behaviour import CyclicBehaviour
from spade.template import Template

from common.base import BaseACLAgent
from common.acl import AclMessage
from common.fipa import conv_id, perf, protocol_of

OUTDIR = "out"
os.makedirs(OUTDIR, exist_ok=True)


class ReporterAgent(BaseACLAgent):
    async def setup(self):
        await super().setup()
        # 1) JSON FIPA-ACL (wpada przez handle_acl)
        self.add_behaviour(self.JsonAuditSink())  # „kotwica” pętli
        # 2) Klasyczny tor (language=text), bez dublowania JSON
        self.add_behaviour(self.ClassicAuditSink(), Template(metadata={"language": "text"}))

    # --- JSON: przychodzi przez BaseACLAgent.handle_acl -> tutaj zapis ---
    async def handle_acl(self, acl: AclMessage, sender: str):
        rec = {
            "ts": int(time.time()),
            "from": sender,
            "to": str(self.jid),
            "performative": acl.performative,
            "protocol": acl.protocol or "fipa-request",
            "conversation_id": acl.conversation_id,
            "body": acl.model_dump_json(),
            "metadata": {
                "ontology": acl.ontology,
                "language": acl.language,
                "reply_by": acl.reply_by,
            },
        }
        path = os.path.join(OUTDIR, f"audit-{rec['ts']}-{rec['conversation_id']}.json")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logging.info("[reporter] zapisano audyt: %s", os.path.basename(path))

    class JsonAuditSink(CyclicBehaviour):
        async def run(self):
            # nic nie odbiera – tylko pozwala mieć cykl
            await asyncio.sleep(0.1)

    class ClassicAuditSink(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=10)
            if not msg:
                return
            rec = {
                "ts": int(time.time()),
                "from": str(msg.sender),
                "to": str(msg.to),
                "performative": perf(msg),
                "protocol": protocol_of(msg),
                "conversation_id": conv_id(msg),
                "body": msg.body,
                "metadata": dict(msg.metadata or {}),
            }
            path = os.path.join(OUTDIR, f"audit-{rec['ts']}-{rec['conversation_id']}.json")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            logging.info("[reporter] zapisano audyt: %s", os.path.basename(path))
