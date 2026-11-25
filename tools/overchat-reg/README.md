# overchat-reg（逆向留档，不是产品模块）

这个目录里只有一个骨架包和一批从站点上下载的 JS chunk，**没有**协议实现、runner、
CLI，也没有登记进网关的 `LINES`（见 `docs/LOOP.md`）。保留它是因为 `_raw/` 里的
chunk 是后续要补协议时的原始材料，删了就得重新抓。

现状：

```
overchat_reg/errors.py     与其它 worker 同构的错误分类
overchat_reg/net.py        proxy-aware httpx 客户端 + UA
_raw/*.js                  站点前端 chunk（.gitignore 里 tools/*/_raw/ 已忽略）
```

要把它变成第七条线，按 `docs/LOOP.md` §8 的七步走，最小集是：

1. `protocol.py`（注册/登录/建 key 的真实端点，从 `_raw/` 里挖）
2. `runner.py` + `store.py` + `push.py` + `cli.py` + `__main__.py`
3. `src/overchat-{pool,register,relay}.mjs`（或复用 `provider-relay`）
4. `src/server.mjs` 挂 `/api/admin/overchat/*` 与 `/overchat/v1/*`
5. `docs/OVERCHAT-POOL.md` + `tests/overchat-*.test.mjs`
6. 把 `overchat-reg/tests/test_offline.py` 补上
7. 在 `scripts/check-loop.mjs` 的 `LINES` 里加一行，并把这里的 `EXEMPT` 条目删掉

在此之前，`npm run loop` 会把它列在"已声明豁免"里，而不是当成缺口。
