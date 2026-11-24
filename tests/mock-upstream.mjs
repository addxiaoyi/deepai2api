// A stand-in for api.deepai.org used by the tests: it speaks just enough of the
// lane (check-in, catalog, chat completions, sign-in exchange) that the gateway
// can be exercised end to end without a browser or the real account.
import { createServer } from "node:http";
import { randomBytes } from "node:crypto";

export const MOCK_MODELS = [
  { id: "deepseek-v4.1-flash", name: "DeepSeek V4.1 Flash", reasoning: true, input: ["text"], contextWindow: 320000, maxTokens: 8192, premier: false, theosPerRequest: "0" },
  { id: "deepai-cyber", name: "DeepAI Cyber", reasoning: true, input: ["text"], contextWindow: 320000, maxTokens: 8192, premier: false, theosPerRequest: "0" },
  { id: "glm-5.3", name: "GLM 5.3", reasoning: true, input: ["text"], contextWindow: 320000, maxTokens: 8192, premier: true, theosPerRequest: "3" },
];

export function startMockUpstream({ exchangeCode = "test-code" } = {}) {
  const seen = { requests: [], exchanges: 0, checkIns: 0 };
  const nonce = () => randomBytes(16).toString("hex");

  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const path = url.pathname;
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      const body = raw ? safeJson(raw) : null;
      seen.requests.push({ method: req.method, path, hasApiKey: Boolean(req.headers["api-key"]), nonce: req.headers["x-deepai-code-check-in"] ?? null });

      const json = (status, value) => {
        const payload = Buffer.from(JSON.stringify(value));
        res.writeHead(status, { "Content-Type": "application/json", "Content-Length": String(payload.length) });
        res.end(payload);
      };

      if (path === "/deepai_code/theos") {
        seen.checkIns++;
        return json(200, {
          enabled: true,
          balance: "42.00000",
          granted: "50.00000",
          refresh_at: "2099-01-01T00:00:00+00:00",
          lane_version: 7,
          check_in: { nonce: nonce(), seconds: 600 },
        });
      }

      if (path === "/deepai_code/models") {
        return json(200, { models: MOCK_MODELS });
      }

      if (path === "/deepai_code/desktop_auth/exchange/v2/" || path === "/deepai_code/desktop_auth/exchange/") {
        seen.exchanges++;
        if (!body?.code || !body?.code_verifier) return json(400, { error: { code: "invalid_request" } });
        if (body.code !== exchangeCode) return json(401, { error: { code: "code_expired" } });
        return json(200, { api_key: "mock-account-api-key", username: "mock-user", is_pro: false });
      }

      if (path === "/deepai_code/chat/completions") {
        if (!req.headers["api-key"]) return json(401, { error: { code: "auth_required" } });
        // A deliberately broken account, used to exercise failover.
        if (req.headers["api-key"] === "bad-key") return json(401, { error: { code: "auth_required", message: "revoked" } });
        const model = String(body?.model ?? "unknown");
        const text = `echo:${model}`;
        if (body?.stream === true) {
          res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store" });
          const chunk = (delta) =>
            `data: ${JSON.stringify({ id: "mock", object: "chat.completion.chunk", created: 1, model, choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`;
          res.write(chunk({ role: "assistant" }));
          res.write(chunk({ content: text }));
          res.write(`data: ${JSON.stringify({ id: "mock", object: "chat.completion.chunk", created: 1, model, choices: [{ index: 0, delta: {}, finish_reason: "stop" }] })}\n\n`);
          res.write("data: [DONE]\n\n");
          return res.end();
        }
        return json(200, {
          id: "mock",
          object: "chat.completion",
          created: 1,
          model,
          choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        });
      }

      if (path === "/deepai_code/web_search") {
        return json(200, { text: "mock search", sources: [] });
      }

      if (path === "/deepai_code/responses") {
        return json(200, { id: "resp_mock", object: "response", output: [] });
      }

      return json(404, { error: { code: "not_found", path } });
    });
  });

  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({
        origin: `http://127.0.0.1:${port}`,
        seen,
        close: () => new Promise((done) => server.close(done)),
      });
    });
  });
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
