# unorouter-reg — UnoRouter

- 基址：`https://unorouter.com`　模型 API：`https://api.unorouter.com/v1`
- Key 前缀：`sk-`　免费后缀：`:free`
- 说明：190+ :free 模型；单模型 1 RPM，靠模型轮换；连接不稳要重试

共用实现：`tools/linekit`（注册 / 发 Key / 号池 / 导出）。本目录只放站点事实。

```bash
pip install -r requirements.txt
python -m unorouter_reg doctor          # 先用它确认注册/发 Key 端点还对不对
python -m unorouter_reg run -n 3 --mail-provider imap --mail-domain example.com
python -m unorouter_reg keys
```

!!! 注意：注册与发 Key 的具体路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
    不对就改 `site.py` 里的 `signup_path` / `key_candidates`，不用动逻辑。
