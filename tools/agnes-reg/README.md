# agnes-reg — Agnes AI

- 基址：`https://api.agnes-ai.cn`　模型 API：`https://api.agnes-ai.cn/v1`
- Key 前缀：`sk-`
- 说明：无限期免费不限 token（约 20 RPM）；注册/发 Key 端点由 doctor 校验

共用实现：`tools/linekit`（注册 / 发 Key / 号池 / 导出）。本目录只放站点事实。

```bash
pip install -r requirements.txt
python -m agnes_reg doctor          # 先用它确认注册/发 Key 端点还对不对
python -m agnes_reg run -n 3 --mail-provider imap --mail-domain example.com
python -m agnes_reg keys
```

!!! 注意：注册与发 Key 的具体路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
    不对就改 `site.py` 里的 `signup_path` / `key_candidates`，不用动逻辑。
