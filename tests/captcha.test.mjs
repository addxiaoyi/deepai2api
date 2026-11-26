// Turnstile 求解器客户端测试（本地 mock server，不联网）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";

import { DEFAULT_SOLVER_URL, solveTurnstile, solverStatus } from "../src/captcha.mjs";

/** 起一个假的 solver：/turnstile 返回 task_id，/result 按脚本返回。 */
async function withSolver(script, run) {
  const state = { polls: 0, calls: [] };
  const server = createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    state.calls.push(`${url.pathname}${url.search}`);
    if (url.pathname === "/") {
      res.writeHead(200, { "Content-Type": "text/html" });
      return res.end("<h1>Turnstile Solver</h1>");
    }
    if (url.pathname === "/turnstile") {
      if (!url.searchParams.get("url") || !url.searchParams.get("sitekey")) {
        res.writeHead(400, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ status: "error", error: "both required" }));
      }
      res.writeHead(202, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ task_id: "task-1" }));
    }
    if (url.pathname === "/result") {
      state.polls++;
      const { status, body } = script(state.polls);
      res.writeHead(status, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(body));
    }
    res.writeHead(404);
    res.end("nope");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    await run({ url, state });
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

test("solves a token after a few polls", async () => {
  await withSolver(
    (n) =>
      n < 3
        ? { status: 200, body: { value: "CAPTCHA_NOT_READY" } }
        : { status: 200, body: { value: "0.token-value", elapsed_time: 1.2 } },
    async ({ url, state }) => {
      const result = await solveTurnstile({
        url: "https://dashboard.proxyscrape.com/v2/sign-up",
        sitekey: "0x4AAAAAAAFWUVCKyusT9T8r",
        solverUrl: url,
        pollMs: 5,
      });
      assert.equal(result.token, "0.token-value");
      assert.equal(result.attempts, 3);
      assert.ok(state.calls[0].startsWith("/turnstile?"), "先建任务再轮询");
      assert.ok(state.calls[0].includes("sitekey="));
    },
  );
});

test("surfaces CAPTCHA_FAIL as solver_failed", async () => {
  await withSolver(
    () => ({ status: 422, body: { value: "CAPTCHA_FAIL", elapsed_time: 9 } }),
    async ({ url }) => {
      await assert.rejects(
        () => solveTurnstile({ url: "https://x.test", sitekey: "k", solverUrl: url, pollMs: 5 }),
        (error) => {
          assert.equal(error.code, "solver_failed");
          return true;
        },
      );
    },
  );
});

test("times out when the task never resolves", async () => {
  await withSolver(
    () => ({ status: 200, body: { value: "CAPTCHA_NOT_READY" } }),
    async ({ url }) => {
      await assert.rejects(
        () => solveTurnstile({ url: "https://x.test", sitekey: "k", solverUrl: url, pollMs: 5, timeoutMs: 60 }),
        (error) => {
          assert.equal(error.code, "solver_timeout");
          return true;
        },
      );
    },
  );
});

test("rejects missing url/sitekey without calling the solver", async () => {
  await assert.rejects(() => solveTurnstile({ url: "", sitekey: "k" }), (error) => error.status === 400);
});

test("solverStatus reports liveness and docs", async () => {
  await withSolver(
    () => ({ status: 200, body: { value: "CAPTCHA_NOT_READY" } }),
    async ({ url }) => {
      const status = await solverStatus({ solverUrl: url });
      assert.equal(status.ok, true);
      assert.equal(status.hasDocs, true);

      const dead = await solverStatus({ solverUrl: "http://127.0.0.1:1", timeoutMs: 500 });
      assert.equal(dead.ok, false);
    },
  );
});

test("default solver url is the local service", () => {
  assert.equal(DEFAULT_SOLVER_URL, "http://127.0.0.1:5000");
});
