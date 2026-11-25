// Mock of the two Blackbox front doors, for offline tests.
//
//   app side : Auth.js (providers / csrf / session / session-challenge) + the
//              dashboard key endpoint that the worker discovers
//   api side : OpenAI-compatible /v1/chat/completions (JSON + SSE), and *no*
//              /v1/models — that is what the real upstream looks like, and it is
//              why the provider descriptor carries a model list.
//
//   node tests/mock-blackbox.mjs        # run standalone and poke it with curl
import { createServer } from "node:http";

const json = (res, status, value, extra = {}) => {
  const body = Buffer.from(JSON.stringify(value));
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": String(body.length),
    "Cache-Control": "no-store",
    ...extra,
  });
  res.end(body);
};

const sse = (res, text) => {
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-store",
    Connection: "keep-alive",
  });
  const id = `chatcmpl-mock-${Date.now().toString(36)}`;
  const base = { id, object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model: "blackboxai/blackbox-pro" };
  res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }] })}\n\n`);
  for (const piece of ["mock ", "stream ", "ok"]) {
    res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { content: piece }, finish_reason: null }] })}\n\n`);
  }
  res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 3, completion_tokens: 3, total_tokens: 6 } })}\n\n`);
  res.write("data: [DONE]\n\n");
  res.end();
};

export async function createMockBlackbox({ requirePro = false, keyPerAccount = 1 } = {}) {
  const state = {
    requests: [],
    issues: 0,
    accounts: new Map(), // email -> [keys]
    calls: { chat: 0, keysList: 0, keysCreate: 0, session: 0 },
  };

  const sessionEmail = (req) => {
    const cookie = String(req.headers.cookie ?? "");
    const match = /__Secure-authjs\.session-token=([^;]+)/.exec(cookie) || /session-token=([^;]+)/.exec(cookie);
    return match ? decodeURIComponent(match[1]) : "";
  };

  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const path = url.pathname;
    const method = req.method ?? "GET";
    state.requests.push(`${method} ${path}`);

    // ---------------------------------------------------------------- app side
    if (method === "GET" && path === "/login") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end("<!doctype html><title>Sign in</title><button>Continue with Google</button>");
      return;
    }
    if (method === "GET" && path === "/api/auth/providers") {
      json(res, 200, {
        google: {
          id: "google",
          name: "Google",
          type: "oidc",
          signinUrl: "/api/auth/signin/google",
          callbackUrl: "/api/auth/callback/google",
        },
      });
      return;
    }
    if (method === "GET" && path === "/api/auth/csrf") {
      json(res, 200, { csrfToken: `csrf-${Date.now().toString(36)}` });
      return;
    }
    if (method === "POST" && path === "/api/auth/signin/google") {
      res.writeHead(302, {
        Location: "https://accounts.google.com/o/oauth2/v2/auth?client_id=mock-client&code_challenge_method=S256",
        "Set-Cookie": "__Secure-authjs.state=mock-state; Path=/; HttpOnly; Secure; SameSite=Lax",
      });
      res.end();
      return;
    }
    if (method === "GET" && path === "/api/auth/callback/google") {
      const email = url.searchParams.get("login_hint") || "mock@own.example";
      res.writeHead(302, {
        Location: "/",
        "Set-Cookie": `__Secure-authjs.session-token=${encodeURIComponent(email)}; Path=/; HttpOnly; Secure; SameSite=Lax`,
      });
      res.end();
      return;
    }
    if (method === "GET" && path === "/api/auth/session") {
      state.calls.session++;
      const email = sessionEmail(req);
      if (!email) return json(res, 200, null);
      return json(res, 200, { user: { id: `u-${email}`, email, name: "Mock User", plan: requirePro ? "free" : "pro" }, expires: new Date(Date.now() + 86400000).toISOString() });
    }
    if (method === "GET" && path === "/api/auth/session-challenge") {
      return json(res, sessionEmail(req) ? 200 : 401, sessionEmail(req) ? { status: "authenticated" } : { status: "unauthenticated" });
    }
    if (method === "POST" && path === "/api/user-activity") {
      return json(res, 200, { ok: true });
    }

    // dashboard keys: list + create (the worker discovers this path)
    if (path === "/api/keys") {
      const email = sessionEmail(req);
      if (!email) return json(res, 401, { error: "unauthorized" });
      if (method === "GET") {
        state.calls.keysList++;
        const keys = state.accounts.get(email) ?? [];
        return json(res, 200, { keys });
      }
      if (method === "POST") {
        state.calls.keysCreate++;
        state.issues += 1;
        const key = `sk-mock${String(state.issues).padStart(4, "0")}${"x".repeat(20)}`;
        const row = {
          id: `key-${state.issues}`,
          key,
          keyPrefix: key.slice(0, 8),
          last4: key.slice(-4),
          name: `mock-${state.issues}`,
          createdAt: new Date().toISOString(),
        };
        const rows = state.accounts.get(email) ?? [];
        rows.push(row);
        state.accounts.set(email, rows);
        return json(res, 201, { key });
      }
    }

    // ---------------------------------------------------------------- api side
    if (method === "POST" && (path === "/v1/chat/completions" || path === "/chat/completions")) {
      const auth = String(req.headers.authorization ?? "");
      if (!auth.startsWith("Bearer sk-mock")) {
        return json(res, 403, {
          error: { message: "blackbox.Error: Invalid API key", type: "auth_error", code: "403" },
        });
      }
      state.calls.chat++;
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      let body = {};
      try {
        body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      } catch {
        body = {};
      }
      const model = String(body.model ?? "blackboxai/blackbox-pro");
      if (body.stream === true) return sse(res, "mock stream ok");
      return json(res, 200, {
        id: `chatcmpl-mock-${Date.now().toString(36)}`,
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model,
        choices: [
          {
            index: 0,
            message: {
              role: "assistant",
              content: JSON.stringify({ tool: "shell", arguments: { command: "pwd" } }),
              tool_calls: [
                {
                  id: "call_mock",
                  type: "function",
                  function: { name: "shell", arguments: '{"command":"pwd"}' },
                },
              ],
            },
            finish_reason: "tool_calls",
          },
        ],
        usage: { prompt_tokens: 12, completion_tokens: 8, total_tokens: 20 },
      });
    }
    // 真实上游没有 /models —— 故意 404，验证 catalog 兜底
    if (path === "/v1/models" || path === "/models") {
      return json(res, 404, { error: "Not Found" });
    }

    return json(res, 404, { error: `no route ${method} ${path}` });
  });

  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const { port } = server.address();

  return {
    url: `http://127.0.0.1:${port}`,
    port,
    state,
    seed(email = "mock@own.example") {
      const key = `sk-mockseed${"y".repeat(18)}`;
      state.accounts.set(email, [
        { id: "key-seed", key, keyPrefix: key.slice(0, 8), last4: key.slice(-4), name: "seed", createdAt: new Date().toISOString() },
      ]);
      return key;
    },
    async close() {
      await new Promise((done) => server.close(done));
    },
  };
}

// Standalone: `node tests/mock-blackbox.mjs`
if (import.meta.url === `file://${process.argv[1].replace(/\\/g, "/")}`) {
  const mock = await createMockBlackbox({});
  console.log(`mock blackbox on ${mock.url}`);
  console.log(`  key  : ${mock.seed()}`);
}
