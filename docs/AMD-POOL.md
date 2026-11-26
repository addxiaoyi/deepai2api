# AMD 开发者云号池（`dcg.pool/amd@1`）

目标不是模型 Key，而是 **AMD Instinct MI300X 的 GPU 实例**（"嫖算力"）。
产物是"能起实验实例的账号登录态 + 积分/实例状态"。

- 控制台：`https://developer.amd.com.cn`（中国区，Vue SPA）
- 接口基址：`https://developer.amd.com.cn/api`（从 `assets/index-*.js` 实测）
- worker：`tools/amd-reg`（`-m amd_reg`）
- 池文件：`$DEEPAI_GATEWAY_DATA/amd-pool.json`
- 管理面：`/api/admin/amd/*`（**没有** `/amd/v1` 反代 —— GPU 不是 OpenAI 兼容面）

## 1. 实测到的端点

从 bundle（245 KB，46 个 `/api/` 路径）里挑出这条线用得上的：

| 用途 | 请求 |
| --- | --- |
| 账号概览（积分/额度） | `GET /api/User/GetCurrentUserDataSummary` |
| 积分流水（Devpoints） | `GET /api/User/GetUserPointsRecords` |
| 续期 | `POST /api/Auth/Refresh` |
| 阿里云智能验证 | `GET /api/Aliyun/GetEncryptedSceneId`、`POST /api/Aliyun/VerifyIntelligentCaptcha` |
| **起 GPU 实例** | `POST /api/Experiment/Launch` |
| 实例状态 | `GET /api/Experiment/Status` |
| 销毁实例（省钱） | `POST /api/Experiment/Remove` |
| 是否已有实例 | `GET /api/Experiment/HasInstance` |

## 2. 自动化边界（这条线只做能做的）

**注册不在这个 bundle 里**：AMD 账号走 `developer.amd.com` / `account.amd.com` 统一登录，
要手机号 + 阿里云智能验证，而且**额度是人工审批**（AMD 官网写明 "qualified developers / sole discretion"）。
所以：

| 环节 | 谁做 |
| --- | --- |
| 注册 AMD 账号 + 拿额度 | 人工（手机号 + 审批），或浏览器半自动 |
| 登录态（cookie / Bearer）→ 入池 | `amd_reg import` |
| 查积分 / 有实例没 | 自动 |
| 起 / 停 GPU 实例 | 自动（`/Experiment/Launch`、`/Experiment/Remove`） |
| 多号池化 + 导出 | 自动（与其它线同一套） |

如实说明：这不是"点一下送 100 小时"的站，**卡在人工审批**；能自动化的是"审批通过之后的所有操作"。

## 2.5 自动刷课（拿 Devpoints → 换 GPU 小时）

AMD 学院（Academy）的课程是 Devpoints 的主要来源。真实上报报文（来自 `assets/scorm-deliver-*.js`）：

```js
await ot({
  CourseId: r,                              // 课程
  CourseChildId: l,                         // 章节
  CourseChildMenuItemId: x,                  // 章节里的 SCORM 条目
  StudyTime: Math.max(1, Math.round(b)),      // 本次新增秒数
  CourseStudyTime: R,                        // 累计进度（秒）
})
```

**字段是 PascalCase**，`StudyTime` 是"本次增量"而不是总时长 —— 所以不需要真的播视频，
按增量循环上报即可。worker 的实现：

```bash
python -m amd_reg courses                       # 课程列表 + 进度
python -m amd_reg study --step 30 --target 3600 # 每门课每个条目刷到 3600s（默认 30s/次、1s 间隔）
python -m amd_reg study --course <id> --force   # 只刷某门 / 连完成的也重刷
python -m amd_reg exam --course <id>            # 看有没有考试（题目会打出来）
```

`study` 的完整动作：`StartCourseStudy` → 逐章节逐条目 `AddCourseStudyRecord` →
`WithdrawCourseStudy` → 再读 `GetCourseDetails` 确认 `StudyRate/StudyStatus` →
对比 `GetUserPointsRecords` 的 total，报告 `gained`。

速率建议：默认 `30s/次 + 1s 间隔`。别把 `--step` 调到 3600（一次上报一小时明显异常），
也别并发多课；一次一门、按章节顺序最像真人。

### 考试（已按前端逐字对齐）

契约来自 `assets/course-detail-*.js` 的 `quizDialog`：

```
GET  api/CourseExam/GetCourseExam?courseChildId=<child>
     -> { Data: { QuestionBankModels: [ { QuestionId, QuestionName, RightAnswerType,
                                         QuestionAnswerModels: [ { Option, OptionDetails } ] } ] } }
POST api/CourseExam/SetExamAnswer
     { CourseChildId, QuestionAnswerRequests: [ { OrderId, QuestionId, QuestionMsg,
                                                 RightAnswer, QuestionType, hasProblem } ] }
GET  api/CourseExam/GetAnswerResult?courseChildId=<child>
     -> { Data: { QuestionResults: [...] } }
```

要点：

- 考试挂在**子课程**上（`courseChildId`），不是课程 id；没给 `--child` 时自动取第一个章节。
- `RightAnswer` 是**逗号分隔的 Option 值**；`RightAnswerType`：0=单选、1=多选。
- 前端交卷前会校验"每题都有答案"，所以自动答题必须给每题都填值。

```bash
python -m amd_reg exam --course c-1                 # 看题（含选项）
python -m amd_reg exam --course c-1 --submit         # 按策略交一次（默认选第一个选项）
python -m amd_reg exam --course c-1 --auto           # 自动：交卷 → 读判卷 → 捡正确答案 → 回填重交
python -m amd_reg exam --course c-1 --auto --strategy all --rounds 3
```

`--auto` 的逻辑：先按策略交一次 → `GetAnswerResult` 里若带 `RightAnswer/CorrectAnswer/StandardAnswer`，
就把正确答案回填到对应题再交，最多 `--rounds` 轮；判卷不给正确答案时立即停手（不做无意义的重复交卷）。

## 2.6 算力券（Lab Voucher）与免费模型（Token Factory）

### 兑换算力券的端点（bundle 实测）

```
POST /api/User/GetLabVoucherExchangeRecords   {PageIndex,PageSize,Target}  兑换记录（含券码）
GET  /api/User/GetLabVoucherExchangeConfig                                兑换配置（积分/小时/上限/验证要求）
POST /api/User/ExchangeLabVoucher             {Amount,Count,Num}          兑换算力券
GET  /api/User/GetCreativeFactoryExchangeStatus / POST /api/User/ExchangeCreativeFactory
POST /api/User/SendLabExchangePhoneCode | ConfirmLabExchangePhoneCode     兑换前手机验证
POST /api/User/SendLabExchangeEmailCode | ConfirmLabExchangeEmailCode     兑换前邮箱验证
GET  /api/DevpointsReward/GetActivities | POST Quote | POST Redeem | POST Apply
```

```bash
python -m amd_reg vouchers            # 每号：配置 + 已换记录 + 券码 + 合计小时（写 out/vouchers.json）
python -m amd_reg exchange --amount 1 # 兑换
python -m amd_reg exchange --dry-run  # 只看要花多少积分/给多少小时
python -m amd_reg consolidate --master master@example.com --redeem   # 汇总 + 在主号集中兑换
```

### 累加到一个号（`consolidate`）

单账号额度是"按号"的，站点没有"转积分"接口；能真正累加的只有两条路，工具都覆盖：

1. **在主号上集中兑换**：`--master <email> --redeem` —— 用主号的 cookie 调 `ExchangeLabVoucher`
   （主号积分不够时先跑 `study` 攒）。
2. **收集券码**：`vouchers` / `consolidate` 会把每个号的 `VoucherCode` 抽出来写进
   `out/vouchers.json` 的 `codes[]`；如果站点支持"券码输入到任意账号"，就能在主号里逐个输入。

### 免费模型（Token Factory）

前端路由是 `/tokenfactory`，页面数据里带 `Access.BaseUrl.Value`（OpenAI 兼容基址）和
`Pricing.Items[]`（计费项）。数据接口不在首屏 bundle 里，所以用**候选探测 + 页面解析 + 缓存**：

```bash
python -m amd_reg models       # -> {baseUrl, items, from: "cache|GET /api/... |page-scan"}
```

探测顺序：`/TokenFactory/GetInfo` → `/TokenFactory/GetTokenFactoryInfo` → `/User/GetTokenFactoryInfo`
→ `/User/GetTokenFactory` → `/User/GetModelAccess` → `/TokenFactory/GetAccess` → `/User/GetFreeModelAccess`
→ 兜底扫 `/tokenfactory` 页面里的 `https://…amd.com` 链接；命中写 `out/tokenfactory.json`。

## 3. 用法

```bash
cd tools/amd-reg

# 1) cookie 从浏览器拿：登录 developer.amd.com.cn → DevTools → 任一 /api/ 请求 → 复制 Cookie 头
python -m amd_reg import --email me@example.com --cookie "SESSION=...; ..."
# 或者批量：每行 "email<TAB>cookie"
python -m amd_reg import --cookies-file cookies.txt --json

# 2) 看积分 / 实例
python -m amd_reg points
python -m amd_reg status
python -m amd_reg launch --email me@example.com
python -m amd_reg remove --email me@example.com      # 不用了就销毁，别烧额度
python -m amd_reg export --format keys
```

网关侧（同一套池/导出/面板）：

```bash
node src/amd-cli.mjs import cookies.txt
node src/amd-cli.mjs pool        # 账号 + 积分合计 + 有实例的数量
node src/amd-cli.mjs apply       # 写 data/keys/amd.{txt,json}
```

## 4. 接口

```
GET    /api/admin/amd/status
POST   /api/admin/amd/register              {count}        # 走 worker 的 import 模式
POST   /api/admin/amd/register/stop
GET    /api/admin/amd/pool?limit=200
POST   /api/admin/amd/pool/import           {email, apiKey(=cookie), credits?}
POST   /api/admin/amd/pool/export?format=keys|jsonl|csv|pool[&download=1]
POST   /api/admin/amd/apply
GET    /api/admin/amd/courses?email=       学院课程列表
POST   /api/admin/amd/study                {email?, course?, step?, target?, pause?, maxCalls?, force?} 自动刷课
POST   /api/admin/amd/register/stop        停掉正在跑的刷课任务
```

池视图里 cookie 只给预览（`session=…`），完整值只出现在导出文件里。

## 5. 免费额度到底有多少（据公开资料）

- AMD AI Developer Program：新账号 **$100 AMD Developer Cloud credits**（≈ MI300X 50 小时，$1.99/hr）
- 早期活动：25 小时赠金 / 中国区活动期 **100 小时专属算力**
- 中国区还有"Devpoints 兑换"：1 Devpoint = 1 小时

所以"多号"的价值在于**额度叠加**，但每个号都要人工过审批 —— 这才是这条线的真实瓶颈，
`npm run pending` 里记为 `amd-account` / `amd-approval`。

## 6. 验证

```bash
node scripts/check-loop.mjs
node --test tests/amd-routes.test.mjs         # 池/路由/无反代/不回显 cookie
python -m pytest tools/amd-reg/tests -q       # cookie 导入/积分/起停实例（MockTransport）
```
