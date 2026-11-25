// Stand-in for dash.allmodels.io: the same endpoints the Python worker drives.
//
// Used by `node tests/allmodels-e2e.mjs` and by the pool tests, so the whole
// chain (python worker -> mock dashboard -> gateway pool -> export) can run
// without touching the real service, a mailbox or a captcha balance.
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

// A real form with the same selectors/placeholders as the production shadcn
// form, so the fingerprint-browser engine can be exercised end to end.
const SIGNUP_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Allmodels Dashboard</title></head><body>
<h1>Create your workspace</h1>
<form id="f">
  <label>Name <input autocomplete="name" placeholder="Ada Lovelace"></label>
  <label>Email <input type="email" autocomplete="email" placeholder="ada@company.com"></label>
  <label>Password <input type="password" autocomplete="new-password" placeholder="At least 8 characters"></label>
  <label>Confirm password <input type="password" autocomplete="new-password" placeholder="Re-enter your password"></label>
  <input type="hidden" name="cf-turnstile-response" value="mock-turnstile-token">
  <div id="err" role="alert" style="color:#b00;display:none"></div>
  <button type="submit">Create account</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', async (event) => {
  event.preventDefault();
  const inputs = document.querySelectorAll('input');
  const body = { name: inputs[0].value, email: inputs[1].value, password: inputs[2].value };
  const token = document.querySelector('input[name="cf-turnstile-response"]').value;
  const response = await fetch('/api/auth/sign-up/email', {
    method: 'POST', credentials: 'include',
    headers: { 'content-type': 'application/json', 'x-turnstile-token': token },
    body: JSON.stringify(body),
  });
  if (response.ok) { window.location.href = '/verify-email'; return; }
  const payload = await response.json().catch(() => ({}));
  const node = document.getElementById('err');
  node.textContent = payload.message || payload.code || 'Request failed';
  node.style.display = 'block';
});
</script></body></html>`;

const VERIFY_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Allmodels Dashboard</title></head><body><h1>Check your email</h1>
<p>We sent a verification link.</p></body></html>`;

const readBody = (req) =>  new Promise((resolve) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch {
        resolve({});
      }
    });
  });

const json = (res, status, body, headers = {}) => {
  const payload = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": String(payload.length),
    ...headers,
  });
  res.end(payload);
};

export function createMockAllmodels({ port = 0, requireTurnstile = true, verifyAtSignup = true } = {}) {
  const state = {
    accounts: new Map(), // email -> {password, verified, keys[], session}
    sessions: new Map(), // token -> email
    keys: new Map(), // key id -> {email, key}
    calls: [],
    created: 0,
  };

  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const path = url.pathname;
    state.calls.push(`${req.method} ${path}`);

    const cookie = String(req.headers.cookie ?? "");
    const token = /better-auth\.session_token=([^;]+)/.exec(cookie)?.[1] ?? "";
    const email = state.sessions.get(token) ?? "";
    const account = email ? state.accounts.get(email) : null;

    if (path === "/signup" || path === "/login") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      return res.end(SIGNUP_PAGE);
    }
    if (path === "/verify-email") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      return res.end(VERIFY_PAGE);
    }
    if (path === "/api/auth/ok") return json(res, 200, { ok: true });

    if (path === "/api/auth/sign-up/email" && req.method === "POST") {
      const body = await readBody(req);
      if (requireTurnstile && !req.headers["x-turnstile-token"]) {
        return json(res, 403, { code: "captcha_failed", message: "Verification failed. Please try again." });
      }
      if (state.accounts.has(body.email)) {
        return json(res, 422, { code: "user_already_exists", message: "User already exists" });
      }
      const session = `sess-${randomUUID()}`;
      state.accounts.set(body.email, {
        password: body.password,
        verified: verifyAtSignup,
        keys: [],
        session,
        name: body.name,
      });
      state.sessions.set(session, body.email);
      return json(
        res,
        200,
        { user: { id: randomUUID(), email: body.email, name: body.name } },
        { "set-cookie": `better-auth.session_token=${session}; Path=/; HttpOnly` },
      );
    }

    if (path === "/api/auth/get-session") {
      if (!account) return json(res, 200, null);
      return json(res, 200, { user: { id: "u1", email, name: account.name, emailVerified: account.verified } });
    }

    if (path === "/api/auth/verify-email") {
      if (!account) return json(res, 401, { error: "unauthenticated" });
      account.verified = true;
      res.writeHead(302, { location: "/verify-email?verified=1" });
      return res.end();
    }

    if (path === "/api/me/overview") {
      if (!account) return json(res, 401, { error: "unauthenticated" });
      return json(res, 200, {
        user: { id: "u1", email, name: account.name },
        organization: { id: "org1", name: "org", slug: "org", status: "active" },
        projects: [{ id: "proj1", name: "Default", slug: "default", status: "active", isDefault: true }],
        guardrails: [
          { id: "gr1", displayName: "Default", budgetMode: "unlimited", enabled: true, isDefault: true },
        ],
        apiKeys: [],
      });
    }

    if (path === "/api/me/signup-key") {
      if (!account) return json(res, 401, { error: "unauthenticated" });
      if (!account.keys.length) {
        const key = `sk_live_signup_${randomUUID().replaceAll("-", "").slice(0, 24)}`;
        const id = randomUUID();
        account.keys.push({ id, key });
        state.keys.set(id, { email, key });
      }
      return json(res, 200, { key: account.keys[0].key });
    }

    if (path === "/api/keys" && req.method === "GET") {
      if (!account) return json(res, 401, { error: "unauthenticated" });
      return json(res, 200, {
        keys: account.keys.map((entry) => ({
          id: entry.id,
          keyPrefix: entry.key.slice(0, 8),
          last4: entry.key.slice(-4),
          displayName: "k",
          status: "active",
          guardrailId: "gr1",
          projectId: "proj1",
          createdAt: Date.now(),
          lastUsedAt: null,
        })),
      });
    }

    if (path === "/api/keys" && req.method === "POST") {
      const body = await readBody(req);
      if (!account) return json(res, 401, { error: "unauthenticated" });
      const key = `sk_live_created_${account.keys.length}_${randomUUID().slice(0, 8)}`;
      const id = randomUUID();
      account.keys.push({ id, key });
      state.keys.set(id, { email, key });
      state.created++;
      return json(res, 200, { key, record: { id, displayName: body.displayName } });
    }

    return json(res, 404, { error: "not_found" });
  });

  return new Promise((resolve) => {
    server.listen(port, "127.0.0.1", () => {
      const address = server.address();
      resolve({
        url: `http://127.0.0.1:${address.port}`,
        state,
        close: () => new Promise((done) => server.close(done)),
      });
    });
  });
}

// `node tests/mock-allmodels.mjs [port]` runs it standalone for manual probes.
if (process.argv[1] && resolve(fileURLToPath(import.meta.url)) === resolve(process.argv[1])) {
  const port = Number(process.argv[2] ?? 8898);
  const mock = await createMockAllmodels({ port });
  process.stdout.write(`mock allmodels on ${mock.url}\n`);
}
