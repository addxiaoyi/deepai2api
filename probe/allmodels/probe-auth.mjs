// Protocol probe for dash.allmodels.io (Better Auth front end).
// Prints raw status/headers/body for the endpoints the registrar needs.
const BASE = process.env.AM_BASE || "https://dash.allmodels.io";
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

async function hit(path, init = {}) {
  const url = `${BASE}${path}`;
  const res = await fetch(url, {
    redirect: "manual",
    ...init,
    headers: {
      "user-agent": UA,
      origin: BASE,
      referer: `${BASE}/signup`,
      accept: "application/json, text/plain, */*",
      ...(init.headers || {}),
    },
  });
  const text = await res.text();
  const out = {
    request: `${init.method || "GET"} ${path}`,
    status: res.status,
    setCookie: res.headers.getSetCookie?.() ?? [],
    contentType: res.headers.get("content-type"),
    body: text.slice(0, 1200),
  };
  return out;
}

const results = [];
results.push(await hit("/api/auth/ok"));
results.push(await hit("/api/auth/get-session"));
results.push(await hit("/api/me/signup-key"));
results.push(
  await hit("/api/auth/sign-up/email", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      name: "Probe Fixture",
      email: process.env.AM_PROBE_EMAIL || `probe.${Date.now().toString(36)}@mailinator.com`,
      password: `Pw-${Math.random().toString(36).slice(2)}-Aa1`,
      callbackURL: `${BASE}/verify-email`,
    }),
  }),
);
console.log(JSON.stringify(results, null, 2));
