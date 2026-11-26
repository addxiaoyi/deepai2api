# cerebras-reg — Cerebras Cloud

- 基址：`https://cloud.cerebras.ai`　模型 API：`https://api.cerebras.ai/v1`
- Key 前缀：`csk-`
- 说明：30 RPM / 14400 RPD / 60k TPM；Key 在 console 签发，端点由 doctor 校验

共用实现：`tools/linekit`（注册 / 发 Key / 号池 / 导出）。本目录只放站点事实。

```bash
pip install -r requirements.txt
python -m cerebras_reg doctor          # 先用它确认注册/发 Key 端点还对不对
python -m cerebras_reg run -n 3 --mail-provider imap --mail-domain example.com
python -m cerebras_reg keys
```

!!! 注意：注册与发 Key 的具体路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
    不对就改 `site.py` 里的 `signup_path` / `key_candidates`，不用动逻辑。
