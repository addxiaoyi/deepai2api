# vendored 第三方代码

| 目录 | 来源 | 许可 | 用途 |
| --- | --- | --- | --- |
| `tools/captcha-solver/` | [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver) | CC BY-NC 4.0（**仅非商业**） | 本地 Turnstile 求解服务，被 `src/captcha.mjs` 与各 worker 调用 |
| `docs/GITEEAI-POOL.md` 描述的协议 | [giteeai2api](https://github.com/)（用户提供的压缩包） | MIT | 我们按同一套 OpenAI Images 形状实现了 `giteeai` 线，没有直接拷贝 Go 代码 |

改动约定：

- `tools/captcha-solver/` 只做**最小改动**（去掉 `.git`、`__pycache__`），协议不变；
  网关侧只依赖 `GET /turnstile` + `GET /result` 两个端点。
- 升级这个目录时同步跑 `node --test tests/captcha.test.mjs`。
