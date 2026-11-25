"""zo_reg —— zo.computer 批量注册 worker（协议 + 指纹浏览器双引擎）。

事件契约与网关闭 `src/provider-register.mjs` 对齐（NDJSON，一行一个）：

    {"type":"log","level":"info","message":"…"}
    {"type":"start","count":N,"concurrency":C,"mail_provider":"imap","engine":"auto","captcha":"none","anti_ban":{…}}
    {"type":"attempt","email":"…","domain":"…","provider":"zo","engine":"protocol","ip":"…"}
    {"type":"retry","round":2,"count":3}
    {"type":"backoff","kind":"rate_limited","seconds":27.4,"email":"…"}
    {"type":"domain","domains":[{"domain":"x.com","ok":3,"bad":0,"health":1.0}]}
    {"type":"account","account":{…},"domain":"x.com","engine":"protocol"}
    {"type":"pushed","email":"…"} / {"type":"push_error","email":"…","message":"…"}
    {"type":"failed","index":0,"email":"…","kind":"email_taken","reason":"…"}
    {"type":"done","summary":{"created":3,"failed":1,"seconds":41.2}}

与其它 provider 的 worker 完全独立：包名 `zo_reg`、输出目录 `--out-dir`（网关给
`data/zo-reg/`）、环境变量前缀 `ZO_`，不会读写 moonai / novita / allmodels 的任何文件。
"""

__version__ = "0.1.0"

PROVIDER_ID = "zo"