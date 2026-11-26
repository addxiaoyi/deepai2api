# opencode-zen-reg — OpenCode Zen

- 基址：`https://opencode.ai`　模型 API：`https://opencode.ai/zen/v1`
- Key 前缀：`sk-`　免费后缀：`-free`
- 说明：30 RPM / 500 RPD；免费模型带 -free 后缀

共用实现：`tools/linekit`（注册 / 发 Key / 号池 / 导出）。本目录只放站点事实。

```bash
pip install -r requirements.txt
python -m opencode_zen_reg doctor          # 先用它确认注册/发 Key 端点还对不对
python -m opencode_zen_reg run -n 3 --mail-provider imap --mail-domain example.com
python -m opencode_zen_reg keys
```

!!! 注意：注册与发 Key 的具体路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
    不对就改 `site.py` 里的 `signup_path` / `key_candidates`，不用动逻辑。
