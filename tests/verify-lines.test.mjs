// 巡检脚本的判定口径测试（纯函数，不联网）。
import { test } from "node:test";
import assert from "node:assert/strict";

import { judgeChat, judgeEndpoint, judgeModels } from "../scripts/verify-lines.mjs";

test("judgeModels: 200/401/403 都算活，HTML 算路径错", () => {
  assert.equal(judgeModels({ status: 200, text: '{"data":[]}' }).level, "ok");
  assert.equal(judgeModels({ status: 401, text: "{}" }).level, "ok");
  assert.equal(judgeModels({ status: 403, text: "{}" }).level, "ok");
  assert.equal(judgeModels({ status: 404, text: "<!DOCTYPE html><html>" }).level, "wrong-path");
  assert.equal(judgeModels({ status: 0, error: "ETIMEDOUT" }).level, "unreachable");
});

test("judgeEndpoint: 404/HTML 视为端点不存在，4xx+JSON 视为存在", () => {
  assert.equal(judgeEndpoint({ status: 404, text: "" }).level, "missing");
  assert.equal(judgeEndpoint({ status: 405, text: "" }).level, "missing");
  assert.equal(judgeEndpoint({ status: 404, text: "<!DOCTYPE html>" }).level, "missing");
  assert.equal(judgeEndpoint({ status: 400, text: '{"code":400,"message":"bad"}' }).level, "exists");
  assert.equal(judgeEndpoint({ status: 401, text: '{"code":401}' }).level, "exists");
  assert.equal(judgeEndpoint({ status: 200, text: '{"code":200}' }).level, "exists");
  assert.match(judgeEndpoint({ status: 400, text: '{"message":"Captcha verification is required"}' }).note, /验证码/);
});

test("judgeChat: 401/400 存在，200 匿名可用，404 不存在", () => {
  assert.equal(judgeChat({ status: 401, text: "{}" }).level, "exists");
  assert.equal(judgeChat({ status: 400, text: "{}" }).level, "exists");
  assert.equal(judgeChat({ status: 200, text: "{}" }).level, "anonymous");
  assert.equal(judgeChat({ status: 404, text: "{}" }).level, "missing");
  assert.equal(judgeChat({ status: 0, error: "boom" }).level, "unreachable");
});

test("纯 HTML 的 200 不会被当成可用端点", () => {
  assert.equal(judgeEndpoint({ status: 200, text: "<!DOCTYPE html><html>app</html>" }).level, "missing");
});
