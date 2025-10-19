# agents/reporter.py (fragment)
import os, json, time, logging
from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template
from common.fipa import conv_id, protocol, perf

OUTDIR = "out"
os.makedirs(OUTDIR, exist_ok=True)

class ReporterAgent(Agent):
    async def setup(self):
        self.add_behaviour(self.AuditSink(), Template())  # łapie wszystkie

    class AuditSink(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=10)
            if not msg:
                return
            rec = {
                "ts": int(time.time()),
                "from": str(msg.sender),
                "to": str(msg.to),
                "performative": perf(msg),
                "protocol": protocol(msg),
                "conversation_id": conv_id(msg),
                "body": msg.body,
                "metadata": dict(msg.metadata or {}),
            }
            path = os.path.join(OUTDIR, f"audit-{rec['ts']}-{rec['conversation_id']}.json")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            logging.info("[reporter] zapisano audyt: %s", os.path.basename(path))

