"""Recon jev-ai.org (direct connection; the env proxy breaks TLS for this host)."""
import json, os, time, httpx

BASE = "https://jev-ai.org"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

c = httpx.Client(timeout=30, follow_redirects=True, trust_env=False, headers={
    "User-Agent": UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
    "Origin": BASE, "Referer": f"{BASE}/sign-up",
})
email = f"probe{int(time.time())}@outlook.com"
r = c.post(f"{BASE}/api/auth/sign-up/email", json={"email": email, "password": "Xk9#mQ2vLp7!aB", "name": "Ada Probe"})
print("signup:", r.status_code, r.text[:300])
print("cookies:", [ck.name for ck in c.cookies.jar])
s = c.get(f"{BASE}/api/auth/get-session")
print("session:", s.status_code, s.text[:400])

for p in ["/api/user/api-keys", "/api/apikeys", "/api/keys", "/api/user/keys",
          "/api/settings/apikeys", "/api/settings/api-keys", "/api/user/apikeys",
          "/api/v1/models"]:
    try:
        g = c.get(f"{BASE}{p}")
        print(f"GET {p} -> {g.status_code} {g.headers.get('content-type')} {g.text[:140]!r}")
    except Exception as exc:
        print(f"GET {p} -> ERR {type(exc).__name__}: {exc}")
print("EMAIL", email)
