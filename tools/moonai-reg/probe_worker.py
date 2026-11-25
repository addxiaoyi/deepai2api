import traceback, httpx
from moonai_reg.config import RunConfig, AntiBanConfig
from moonai_reg.identity import make_persona
from moonai_reg.mailbox import MailTM
from moonai_reg.protocol import MoonAIProtocol

cfg = RunConfig(mail_provider="mailtm", engine="protocol", out_dir="out-live")
box = MailTM.open()
print("mailbox", box.address, flush=True)
p = make_persona(seed="probe-1")
proto = MoonAIProtocol(config=cfg, persona=p, log=lambda m: print("  log:", m, flush=True))
try:
    print("1 signup", flush=True)
    proto.signup(box.address, p.password, p.username)
    print("2 wait mail", flush=True)
    link = box.wait_for_verify_link(timeout=120)
    print("   link", link, flush=True)
    print("3 verify", proto.verify_email(link), flush=True)
    print("4 signin", flush=True)
    proto.signin(box.address, p.password)
    print("5 oauth", flush=True)
    cb = proto.oauth_callback()
    print("   callback", cb, flush=True)
    data = proto.exchange_token(cb)
    print("6 token ok", bool(data.get("access_token")), flush=True)
    print("7 org", proto.ensure_organization(data["access_token"]), flush=True)
finally:
    proto.close()
