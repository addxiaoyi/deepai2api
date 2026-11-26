# pollinations-reg — Pollinations AI

- 基址：`https://gen.pollinations.ai`　模型 API：`https://gen.pollinations.ai/v1`
- Key 前缀：`（匿名，不需要 Key）`
- 说明：匿名线：不需要注册，按 IP 限速（10 RPM / 500 RPD）

共用实现：`tools/linekit`（注册 / 发 Key / 号池 / 导出）。本目录只放站点事实。

```bash
pip install -r requirements.txt
python -m pollinations_reg doctor          # 先用它确认注册/发 Key 端点还对不对
python -m pollinations_reg run -n 3 --mail-provider imap --mail-domain example.com
python -m pollinations_reg keys
```

!!! 注意：注册与发 Key 的具体路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
    不对就改 `site.py` 里的 `signup_path` / `key_candidates`，不用动逻辑。
