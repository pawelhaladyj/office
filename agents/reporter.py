import pathlib, json, datetime
from agents.base import BaseACLAgent
from common.acl import AclMessage

OUT = pathlib.Path("out"); OUT.mkdir(exist_ok=True)

class ReporterAgent(BaseACLAgent):
    async def handle_acl(self, acl: AclMessage, sender: str):
        if acl.performative=="INFORM" and acl.payload.get("type")=="AUDIT":
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            (OUT/f"{acl.conversation_id}-{ts}.json").write_text(json.dumps(acl.payload, indent=2))
            print("[report] zapisano ofertę")
