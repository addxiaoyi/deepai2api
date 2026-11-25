// 模拟运行环境：一个假上游，同时扮演 WaveSpeed 与 Novita 的面板 + 推理端点。
//
// 它让整条链路可以**完全离线**跑通：
//
//   注册 worker（Python）  →  假面板 /v1/user/register、/v2/user/key
//   网关控制器             →  POST /api/admin/providers/<id>/pool/import
//   号池                   →  data/<provider>-pool.json
//   反代层                 →  /p/<provider>/v1/chat/completions
//
// 用法（编程 / 测试）：
//
//   import { startMockProvider } from "./mock-provider.mjs";
//   const mock = await startMockProvider();
//   mock.panelBase   // 给 worker 的 --base-url
//   mock.apiBase     // 给 catalog 的 apiBase
//   mock.taskBase    // 给 catalog 的 taskApiBase
//   await mock.close();
//
// 也支持命令行起一个常驻的（手工 curl 调试）：
//
//   node tests/mock-provider.mjs --port 8899
import { createServer } from "node:http";

/** 生成一把长得像真的面板 Key。 */
const mintPanelKey = (provider, seed) => {
  const body = Buffer.from(`${provider}:${seed}:${Date.now()}`).toString("base64url").slice(0, 28);
  return provider === "novita" ? `sk_${body}` : `ws_${body}`;
};

/**
 * @param {{port?: number, provider?: "wavespeed"|"novita"|"both", latencyMs?: number}} options
 */
export async function startMockProvider({ port = 0, provider = "both", latencyMs = 0 } = {}) {
  const state = {
    /** 已注册的账号：email -> {email, password, keys[]} */
    accounts: new Map(),
    /** 面板 Key -> email */
    keys: new Map(),
    /** 生成任务：task_id -> {status, model, payload} */
    tasks: new Map(),
    /** 收到的每个请求，便于断言 */
    calls: [],
    /** 人为制造的故障：'blocked' | 'captcha' | '402' | '429' | null */
    fault: null,
  };

  const json = (res, status, body) => {
    const payload = Buffer.from(JSON.stringify(body));
    res.writeHead(status, { "Content-Type": "application/json", "Content-Length": String(payload.length) });
    res.end(payload);
  };

  const readBody = (req) =>
    new Promise((done) => {
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        try {
          done(text ? JSON.parse(text) : {});
        } catch {
          done({});
        }
      });
    });

  const bearer = (req) => {
    const header = req.headers.authorization ?? "";
    return header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  };

  /** 统一处理故障注入。返回 true 表示已应答。 */
  const injectFault = (res) => {
    if (state.fault === "blocked") {
      res.writeHead(403, { "Content-Type": "text/html", server: "cloudflare" });
      res.end("<html><title>Just a moment...</title></html>");
      return true;
    }
    if (state.fault === "captcha") {
      json(res, 400, { code: "captcha_failed", message: "captcha required" });
      return true;
    }
    if (state.fault === "402") {
      json(res, 402, { errorNo: "INSUFFICIENT_BALANCE", errorInfo: "no balance" });
      return true;
    }
    if (state.fault === "429") {
      json(res, 429, { code: "rate_limited", message: "too many requests" });
      return true;
    }
    return false;
  };

  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const path = url.pathname;
    const body = req.method === "POST" || req.method === "PUT" ? await readBody(req) : {};
    state.calls.push({ method: req.method, path, query: url.searchParams.toString(), body, auth: bearer(req) });

    if (latencyMs > 0) await new Promise((done) => setTimeout(done, latencyMs));

    /* ------------------------------------------------ 面板：Novita 形状 */

    if (path === "/v1/user/register") {
      if (injectFault(res)) return;
      const email = String(body.email ?? "").toLowerCase();
      if (!email || !body.password) return json(res, 400, { errorNo: "INVALID_PARAM", errorInfo: "email/password required" });
      if (!body.cloudflareToken) return json(res, 400, { errorNo: "TURNSTILE_ERROR", errorInfo: "captcha required" });
      if (state.accounts.has(email)) return json(res, 200, { errorNo: "USER_ALREADY_EXIST", errorInfo: "already exists" });
      state.accounts.set(email, { email, password: body.password, keys: [], provider: "novita" });
      return json(res, 200, { errorNo: 0, data: { email } });
    }

    if (path === "/v1/user/login") {
      const email = String(body.email ?? "").toLowerCase();
      const account = state.accounts.get(email);
      if (!account || account.password !== body.password) {
        return json(res, 401, { errorNo: "WRONG_PASSWORD", errorInfo: "bad credentials" });
      }
      return json(res, 200, { errorNo: 0 });
    }

    if (path === "/v1/user/info") {
      const key = bearer(req);
      const email = state.keys.get(key);
      if (!email) return json(res, 401, { errorNo: "UNAUTHORIZED", errorInfo: "no session" });
      return json(res, 200, { errorNo: 0, data: { email } });
    }

    if (path === "/v1/user/email/verify") {
      return json(res, 200, { errorNo: 0, data: { verified: true } });
    }

    if (path === "/v2/user/key" && req.method === "GET") {
      const email = bearer(req) && state.keys.get(bearer(req));
      if (!email) return json(res, 401, { errorNo: "UNAUTHORIZED" });
      const account = state.accounts.get(email);
      return json(res, 200, { errorNo: 0, keys: account?.keys ?? [] });
    }

    if (path === "/v2/user/key" && req.method === "POST") {
      if (injectFault(res)) return;
      // 面板建 key 需要一个"已登录"的信号：mock 里用 X-Session-Email 头代替 cookie
      const email = String(req.headers["x-session-email"] ?? "").toLowerCase();
      const account = state.accounts.get(email) ?? [...state.accounts.values()].at(-1);
      if (!account) return json(res, 401, { errorNo: "UNAUTHORIZED", errorInfo: "no account" });
      if (account.keys.length >= 10) return json(res, 200, { errorNo: "KEY_LIMIT_EXCEEDED" });
      const key = mintPanelKey("novita", account.keys.length);
      const record = { key, id: `key-${account.keys.length + 1}`, name: body.name ?? "", expireTime: body.expireTime ?? -1 };
      account.keys.push(record);
      state.keys.set(key, account.email);
      // 明文只在创建响应里出现——与真实面板一致
      return json(res, 200, { errorNo: 0, ...record });
    }

    /* -------------------------------------------- 面板：WaveSpeed 形状 */

    if (path === "/sign-in") {
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(
        '<html><head><title>Sign in - WaveSpeedAI</title></head><body>' +
          '<button>Continue with Google</button><button>Continue with GitHub</button>' +
          "</body></html>",
      );
      return;
    }

    if (path === "/accesskey") {
      if (injectFault(res)) return;
      res.writeHead(200, { "Content-Type": "text/html" });
      res.end(
        '<html><head><title>API Keys</title></head><body>' +
          '<form><input placeholder="Key name" /></form><button>Generate</button>' +
          "</body></html>",
      );
      return;
    }

    if (path === "/api/v3/balance") {
      const key = bearer(req);
      if (!state.keys.has(key)) return json(res, 401, { code: "unauthorized", message: "invalid key" });
      return json(res, 200, { code: 200, data: { balance: 1.0 } });
    }

    /* ------------------------------------------------------ 推理：共同 */

    const isModels = path === "/v1/models";
    if (isModels) {
      const key = bearer(req);
      if (!state.keys.has(key)) return json(res, 401, { error: { message: "invalid api key", code: "invalid_api_key" } });
      return json(res, 200, {
        data: [
          { id: "deepseek/deepseek-v4-flash", object: "model", owned_by: "mock" },
          { id: "zai-org/glm-5.3", object: "model", owned_by: "mock" },
        ],
      });
    }

    if (path === "/v1/chat/completions") {
      const key = bearer(req);
      if (!state.keys.has(key)) return json(res, 401, { error: { message: "invalid api key", code: "invalid_api_key" } });
      const model = String(body.model ?? "mock-model");
      const content = `echo:${(body.messages ?? []).at(-1)?.content ?? ""}`;
      return json(res, 200, {
        id: "chatcmpl-mock",
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model,
        choices: [{ index: 0, message: { role: "assistant", content }, finish_reason: "stop" }],
        usage: { prompt_tokens: 3, completion_tokens: 3, total_tokens: 6 },
      });
    }

    /* ---------------------------------------------- 生成任务：wavespeed */

    if (req.method === "POST" && path.startsWith("/wavespeed-ai/")) {
      const key = bearer(req);
      if (!state.keys.has(key)) return json(res, 401, { code: "unauthorized" });
      const id = `task-${state.tasks.size + 1}`;
      state.tasks.set(id, { status: "created", model: path.slice(1), payload: body, provider: "wavespeed" });
      return json(res, 200, {
        code: 200,
        data: { id, status: "created", urls: { get: `/predictions/${id}/result` } },
      });
    }

    if (req.method === "GET" && path.startsWith("/predictions/")) {
      const id = path.split("/")[2];
      const task = state.tasks.get(id);
      if (!task) return json(res, 404, { code: 404, message: "task not found" });
      // 第一次查 processing，第二次 completed —— 保证轮询逻辑被真正走到
      task.status = task.status === "created" ? "processing" : "completed";
      return json(res, 200, { code: 200, data: { id, status: task.status, outputs: ["https://mock/out.png"] } });
    }

    /* ------------------------------------------------- 生成任务：novita */

    if (req.method === "POST" && path === "/v3/async/txt2img") {
      const key = bearer(req);
      if (!state.keys.has(key)) return json(res, 401, { errorNo: "UNAUTHORIZED" });
      const id = `nv-${state.tasks.size + 1}`;
      state.tasks.set(id, { status: "TASK_STATUS_QUEUED", payload: body, provider: "novita" });
      return json(res, 200, { task_id: id });
    }

    if (req.method === "GET" && path === "/v3/async/task-result") {
      const id = url.searchParams.get("task_id") ?? "";
      const task = state.tasks.get(id);
      if (!task) return json(res, 404, { errorNo: "TASK_NOT_FOUND" });
      task.status = task.status === "TASK_STATUS_QUEUED" ? "TASK_STATUS_SUCCEED" : task.status;
      return json(res, 200, { task: { task_id: id, status: task.status, progress_percent: 100 } });
    }

    /* -------------------------------------------------------- 兜底 */

    return json(res, 404, { error: "mock: no route", path });
  });

  await new Promise((done) => server.listen(port, "127.0.0.1", done));
  const bound = server.address().port;
  const base = `http://127.0.0.1:${bound}`;

  return {
    base,
    panelBase: base,
    apiBase: base,
    taskBase: base,
    port: bound,
    state,
    /** 直接往 mock 里塞一个"已注册且已发 key"的账号（跳过 worker）。 */
    seedAccount({ email, provider: kind = "novita", apiKey = "" }) {
      const key = apiKey || mintPanelKey(kind, email);
      state.accounts.set(String(email).toLowerCase(), {
        email: String(email).toLowerCase(),
        password: "seeded",
        keys: [{ key, id: "key-1", name: "seeded", expireTime: -1 }],
        provider: kind,
      });
      state.keys.set(key, String(email).toLowerCase());
      return key;
    },
    setFault(fault) {
      state.fault = fault;
    },
    lastCall(path) {
      return [...state.calls].reverse().find((call) => call.path === path) ?? null;
    },
    close: () => new Promise((done) => server.close(done)),
  };
}

/* ---------------------------------------------------------------- CLI */

const isMain = process.argv[1] && /mock-provider\.mjs$/.test(process.argv[1].replace(/\\/g, "/"));
if (isMain) {
  const portFlag = process.argv.indexOf("--port");
  const port = portFlag !== -1 ? Number(process.argv[portFlag + 1]) : 8899;
  const mock = await startMockProvider({ port });
  const seed = mock.seedAccount({ email: "demo@example.com", provider: "novita" });
  process.stdout.write(
    [
      `mock provider listening on ${mock.base}`,
      `  面板/推理共用同一端口，可同时喂 wavespeed 与 novita`,
      `  预置账号 demo@example.com，key=${seed}`,
      `  挂号: curl -H "Authorization: Bearer ${seed}" ${mock.base}/v1/models`,
      "",
    ].join("\n"),
  );
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => {
      mock.close().then(() => process.exit(0));
    });
  }
}
