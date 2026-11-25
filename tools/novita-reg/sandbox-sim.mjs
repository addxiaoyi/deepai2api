#!/usr/bin/env node
// Novita 沙箱「模拟运行环境」—— 让官方 CLI / SDK 在离线环境里跑通调用链。
//
// 背景：我们把 `novita-sandbox-cli@2.1.4` 下下来逆向过（取证见 docs/NOVITA-SANDBOX-RE.md）。
// 它读的环境变量与鉴权方式如下（从 dist/index.js 里直接读出来的，不是猜的）：
//
//   NOVITA_API_KEY        → 请求头 `X-API-KEY: <key>`（沙箱控制面）
//   NOVITA_ACCESS_TOKEN   → 请求头 `Authorization: Bearer <token>`（同上，二者择一）
//   NOVITA_API_URL        → 控制面基址；未设时由 NOVITA_DOMAIN 推：`https://api.${domain}`
//   NOVITA_DOMAIN         → 默认 `us-phx-1.sandbox.novita.ai`
//   ~/.novita/config.json → 持久化的 apiKey / accessToken / domain
//
// 所以「模拟运行环境」= 一个实现了这些端点的本地服务 + 三个环境变量：
//   node tools/novita-reg/sandbox-sim.mjs --port 8790
//   $env:NOVITA_API_KEY="sk-sim"; $env:NOVITA_API_URL="http://127.0.0.1:8790"
//   npx novita-sandbox-cli sandbox list
//
// 这样注册机拿到的真 key 和编程工具里的沙箱调用可以分开验证：先离线跑通，
// 再换真 key 打线上，出错时能立刻区分「代码问题」还是「上游问题」。
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";

const argv = process.argv.slice(2);
const argOf = (flag, fallback) => {
  const index = argv.indexOf(flag);
  return index !== -1 && argv[index + 1] ? argv[index + 1] : fallback;
};

const port = Number(argOf("--port", 8790));
const expectedKey = argOf("--key", process.env.NOVITA_API_KEY || "");

const sandboxes = new Map();
const templates = [
  { templateID: "tpl-base", name: "base", status: "ready", image: "novita/base:latest", createdAt: new Date().toISOString() },
  { templateID: "tpl-codex", name: "codex", status: "ready", image: "novita/codex:latest", createdAt: new Date().toISOString() },
];

const sendJson = (res, status, value) => {
  const body = Buffer.from(JSON.stringify(value));
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": String(body.length) });
  res.end(body);
};
const notFound = (res, path) => sendJson(res, 404, { code: 404, message: `no mock route for ${path}` });

const readBody = async (req) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const text = Buffer.concat(chunks).toString("utf8");
  if (!text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
};

/** 与官方 CLI 一致的鉴权：X-API-KEY 或 Bearer；模拟环境默认放行空 key。 */
function authorised(req) {
  if (!expectedKey) return true;
  const apiKey = String(req.headers["x-api-key"] ?? "");
  const bearer = String(req.headers.authorization ?? "").replace(/^Bearer\s+/i, "");
  return apiKey === expectedKey || bearer === expectedKey;
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", `http://127.0.0.1:${port}`);
  const path = url.pathname.replace(/\/+$/, "") || "/";

  if (path === "/health" || path === "/healthz") return sendJson(res, 200, { status: "ok", mock: true });
  if (path === "/") {
    return sendJson(res, 200, {
      mock: "novita-sandbox",
      hint: "把 NOVITA_API_URL 指到这里即可离线跑官方 CLI",
      sandboxes: sandboxes.size,
      templates: templates.length,
    });
  }
  if (!authorised(req)) return sendJson(res, 401, { code: 401, message: "invalid api key" });

  // ---- 配额 ----
  if (path === "/sandboxes/quota" && req.method === "GET") {
    return sendJson(res, 200, {
      limit: { concurrentSandboxes: 5, vcpu: 8, memoryMiB: 16384 },
      usage: { concurrentSandboxes: sandboxes.size, vcpu: sandboxes.size, memoryMiB: sandboxes.size * 1024 },
    });
  }

  // ---- 模板 ----
  if (path === "/v2/templates" && req.method === "GET") {
    const page = Number(url.searchParams.get("page") ?? 1);
    const limit = Number(url.searchParams.get("limit") ?? 20);
    return sendJson(res, 200, { templates: templates.slice((page - 1) * limit, page * limit), total: templates.length });
  }

  // ---- 沙箱集合 ----
  if (path === "/sandboxes" && req.method === "GET") {
    return sendJson(res, 200, { sandboxes: [...sandboxes.values()], total: sandboxes.size });
  }
  if (path === "/sandboxes" && req.method === "POST") {
    const body = await readBody(req);
    const id = `sbx-${randomUUID().slice(0, 8)}`;
    const record = {
      sandboxID: id,
      templateID: body.templateID ?? "tpl-base",
      status: "running",
      metadata: body.metadata ?? {},
      startedAt: new Date().toISOString(),
      endAt: new Date(Date.now() + (Number(body.timeout) || 300) * 1000).toISOString(),
      domain: "127.0.0.1",
    };
    sandboxes.set(id, record);
    return sendJson(res, 200, record);
  }

  const sandboxMatch = /^\/sandboxes\/([^/]+)(\/.*)?$/.exec(path);
  if (sandboxMatch) {
    const id = sandboxMatch[1];
    const rest = sandboxMatch[2] ?? "";
    const record = sandboxes.get(id);
    if (!record) return sendJson(res, 404, { code: 404, message: `sandbox ${id} not found` });

    if (rest === "" && req.method === "GET") return sendJson(res, 200, record);
    if (rest === "" && req.method === "DELETE") {
      sandboxes.delete(id);
      return sendJson(res, 200, { sandboxID: id, status: "killed" });
    }
    if (rest === "/timeout" && req.method === "POST") {
      const body = await readBody(req);
      record.endAt = new Date(Date.now() + (Number(body.timeout) || 300) * 1000).toISOString();
      return sendJson(res, 200, record);
    }
    // 执行命令：模拟环境直接回显，不做真执行（避免"跑起来像真的"却不可信的假象）
    if ((rest === "/commands" || rest === "/processes") && req.method === "POST") {
      const body = await readBody(req);
      const cmd = String(body.command ?? body.cmd ?? "");
      return sendJson(res, 200, {
        exitCode: 0,
        stdout: `[mock] would run: ${cmd}\n`,
        stderr: "",
        sandboxID: id,
        mocked: true,
      });
    }
    if (rest.startsWith("/files") && req.method === "GET") {
      return sendJson(res, 200, { path: url.searchParams.get("path") ?? "/", entries: [] });
    }
  }

  // ---- 卷 ----
  if (path.startsWith("/volumes")) {
    if (req.method === "GET") return sendJson(res, 200, { volumes: [], total: 0 });
    return sendJson(res, 200, { volumeID: `vol-${randomUUID().slice(0, 8)}` });
  }

  return notFound(res, path);
});

server.listen(port, "127.0.0.1", () => {
  process.stdout.write(
    `[mock] novita sandbox simulator on http://127.0.0.1:${port}\n` +
      `[mock] 用法：NOVITA_API_KEY=sk-sim NOVITA_API_URL=http://127.0.0.1:${port} npx novita-sandbox-cli sandbox list\n`,
  );
});
