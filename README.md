# Loomy2API

把 **Loomy（讯飞桌面 AI 助理）** 的账号登录态，反向代理成 **OpenAI 兼容 API**，并附上一整套「号池运营」能力：
自动取号注册、自动跑任务养积分、模型上下线监控、Web 控制台。

> 仅用于自用与研究：请使用你自己拥有或已获授权的账号。项目不内置任何账号、session、手机号或平台密钥。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [功能总览](#功能总览)
- [架构](#架构)
- [上游协议还原](#上游协议还原)
- [快速开始](#快速开始)
- [控制台](#控制台)
- [对外接口](#对外接口)
- [接码平台自动入池](#接码平台自动入池)
- [实测数据](#实测数据)
- [配置项](#配置项)
- [已知限制](#已知限制)
- [免责声明](#免责声明)

---

## 它解决什么问题

Loomy 桌面端（Electron 套壳 opencode）里有一个名为 `imodel` 的 provider，直接连讯飞网关
`https://loomyad.xunfei.cn/api/v1`，用的是**登录 session 当凭据**。也就是说：只要拿到 session，
就能在任意程序里调用这套模型。

本项目做三件事：

1. **反代**：把 session 包装成标准 `/v1/chat/completions`，任何 OpenAI 客户端（含 DSH、Cherry Studio、Lobe、Claude Code 等）都能接；
2. **号池**：多账号轮换、失效自动切换、积分/模型状态可视化；
3. **养号**：对接接码平台自动注册新号入池，并自动完成「每日积分 + 8 项新手任务」，让号池持续产出额度。

---

## 功能总览

| 模块 | 能力 |
| :-- | :-- |
| 网关 | `POST /v1/chat/completions`（流式 SSE / 非流式）、`GET /v1/models`（含上下文/最大输出/倍率） |
| 号池 | 多账号、启停、积分刷新、失效标记、失败换号、**503/504 自动切兜底模型** |
| 路由策略 | `balance`（积分最多优先）/ `lru`（最久未用优先）/ `round_robin`（轮询），配 **账号冷却** |
| 设备标识 | **每个账号首次出现时生成独立 `devid` + `campus_device_id`**，可单号重绑/批量补齐 |
| 接码入池 | 平台取号 → 请求 Loomy 验证码 → 轮询取码 → 登录 → 入池 → 释放/拉黑；并发多路撒号 |
| **本地注册器** | `loomy_registrar_gui.py`：在**自己电脑**上跑注册（本地网页界面），结果**导出 txt**，再粘到服务端面板导入 |
| 批量导入 | `POST /admin/api/accounts/import` + 面板「批量导入 session」：校验 / 去重 / 自动生成独立设备标识 |
| 任务中心 | 每日积分 `first-login` + 8 项新手任务领取 + **回读确认**（`earned`/`unclaimed`） |
| 模型监控 | 首次出现 / 最近出现 / 上线 / 下线 / 倍率 / 单模型首包测活 |
| 控制台 | 密码登录（cookie）、账号管理、模型表、任务表、接码配置、日志 |

---

## 架构

```text
                        ┌──────────────────────────────┐
OpenAI 客户端  ────────▶│  Loomy2API (FastAPI :4556)   │
(DSH / SDK / curl)      │                              │
                        │  ① 鉴权 api_keys             │
                        │  ② 号池选号（随机/失效跳过） │
                        │  ③ 补头转发 + SSE 透传       │
                        │  ④ 失败分类：session→换号    │
                        │             provider→兜底模型│
                        └───────────┬──────────────────┘
                                    │  Authorization: Bearer <session>
                                    │  token: <session>
                                    │  traceparent / loomy-version
                                    ▼
                    https://loomyad.xunfei.cn/api/v1
                    （讯飞模型网关，provider: venus）
```

配套：

```text
nginx  ──▶  Loomy2API :4556   （面板密码登录；/v1 用 API Key）
                   │
                   ├─ 接码平台 API（取号/取码/释放/拉黑）
                   ├─ account.xfinfr.com（短信发码/登录，HMAC-SHA1 签名）
                   └─ /points/first-login + /onboarding/tasks（每日积分与新手任务）
```

---

## 上游协议还原

从官方桌面端（`Loomy-0.9.x-arm64.dmg` → `app.asar`）还原出的关键事实：

| 项 | 值 | 来源 |
| :-- | :-- | :-- |
| provider | `imodel` | `resources/opencode-home-template/opencode.json` |
| baseURL | `https://loomyad.xunfei.cn/api/v1` | `electron/bundled-resources.js` 由 `pointsConfig.baseUrl` 拼 `/api/v1` |
| 认证 | 登录 session：`Authorization: Bearer <s>` **和** `token: <s>` | `useSessionAuth: true` |
| 必带头 | `traceparent`（缺失会挂到超时）、`loomy-version`、`User-Agent: Loomy\|Desktop\|Electron\|macOS` | `electron/utils/request-headers.js` |
| 对话 | `POST /api/v1/chat/completions`（OpenAI 兼容，SSE） | `electron/llm/llm-completion.js` |
| 模型 | `GET /api/v1/models` → `{data:[{id,name,limit:{context,output},reasoning_efforts,…}]}` | `electron/model-service.js` |
| 积分 | `GET /api/v1/points/balance`、`POST /api/v1/points/first-login` | `electron/points-service.js` |
| 任务 | `GET/POST /api/v1/onboarding/tasks[/complete]` | 同上 |

> `traceparent` 是硬需求：实测缺失时上游会一直挂着直到超时。

---

## 快速开始

```bash
git clone <this-repo> loomy2api && cd loomy2api
./install.sh                      # 建 venv、装依赖、生成 config.json（600）
./.venv/bin/python app.py         # 默认 127.0.0.1:4556
```

首次启动会自动生成面板密码并打印：

```bash
grep 面板密码 logs/service.log
```

打开 `http://127.0.0.1:4556/` → 输入面板密码 → 「添加账号」（短信登录或粘贴 session）→ 复制「调用」区块里的 curl。

systemd（推荐）：

```bash
sudo install -m 644 .loomy2api.service.generated /etc/systemd/system/loomy2api.service
sudo systemctl daemon-reload && sudo systemctl enable --now loomy2api
```

nginx 两种入口（二选一，见 `nginx-8793.conf` / `nginx-path.conf`）：

- 独立端口：`http://<host>:8793/v1/...`
- 复用 80 端口路径：`http://<host>/loomy/v1/...`（需要 `X-Forwarded-Prefix /loomy`，否则登录跳转会丢前缀）

---

## 号池调度

三种策略在面板「设置 → 路由策略」切换，冷却与重试同页可配：

| 策略 | 选号规则 |
| :-- | :-- |
| `balance`（默认） | 可用积分最多的号优先，同分按最久未用 |
| `lru` | 最久没有被使用过的号优先 |
| `round_robin` | 按顺序轮转，指针持久在内存 |

冷却规则：

- 触发条件：`401/403/402/429`，或上游返回 session 失效（`100002`/「登录已失效」）；
- 冷却期内该号从候选里剔除（`cooldown_seconds`，默认 300 秒）；
- 冷却中会在面板显示倒计时与原因，可手动「解除冷却」；
- **全池都在冷却时不空转**：会硬试一次，避免整体不可用；
- 上游 `503/504`（provider 侧）**不会**冷却账号，只切兜底模型。

已实测（服务器上跑的单元验证）：

```text
balance      : ['B(20000)', 'C(9000)', 'A(5000)']       ✓
lru          : ['A(最旧)', 'C', 'B(最新)']              ✓
round_robin  : A,B,C → B,C,A → C,A,B → A,B,C           ✓
冷却中的坏号 : 三种策略下都被正确剔除                  ✓
坏号实测     : session=deadbeef → 记录「登录已失效」并冷却 45s ✓
```

## 设备标识

每个账号在首次出现（短信登录 / 粘贴 session / 启动时补齐）时，生成一套**独立**的设备标识：

```
devid            = web-<16 hex>                 # 请求 envelope 的 devid
campus_device_id = loomy-campus-<uuid4>         # 积分/活动类接口的设备维度标识
ua/modelid/version = 客户端原值（Loomy|Desktop|Electron|macOS / Web / 1.0.0）
traceid          = 每次请求随机（属于请求头，不属于身份）
```

用途与边界：

- 让同一批号在上游侧**看起来是不同设备**，而不是全部 `devid=web`；
- 该标识会随请求一起发送（`X-Loomy-Device-ID` 头 + 账号服务 envelope 的 `devid` + `points/first-login` 的 `campus_device_id`）；
- **不改变请求是否被接受**——它影响的是账号之间的关联度，不是额度或能力；
- 面板账号表可直接看到每个号的 `devid`，支持**单号「换设备」**与**批量补设备标识**；
- `identity_mode=client` 可退回完全照客户端的行为（`devid=web`），保守场景用。

已验证：存量 21 个账号启动时自动补齐，`devid` 21/21 唯一；单个重绑返回新 `devid` 与 `campus_device_id`；带新头的真实对话与任务查询均正常。

## 控制台

`GET /` 或 `/admin`，应用内密码登录（cookie `loomy2api_admin`，30 天），不依赖浏览器 Basic Auth
——内嵌浏览器（Electron）不弹原生登录框，用 Basic Auth 会直接 401。

| 区块 | 内容 |
| :-- | :-- |
| 账号池 | 备注/手机/登录态/积分/最近错误；刷新积分、停用、重置、删除 |
| 添加账号 | 短信登录（手机号 → 验证码）或粘贴 session |
| 接码平台 | 平台地址/账号/密码/项目 ID/路径/并发；测试取号、开始入池、停止、实时日志 |
| 任务中心 | 每账号任务进度 + 单项领取 + 一键全部 + 回读确认 |
| 模型清单 | 状态（上线/下线）、上下文、最大输出、能力、倍率、首见/末见、单模型测活（首包耗时） |
| 调用 | Base URL / API Key / curl 示例 / 快速测活（只取首包） |
| 设置 | 每日签到时间、积分刷新间隔、默认模型、兜底模型、网关 API Key、面板密码 |
| 日志 | `usage.log` / `daily.log` 尾部 |

---

## 对外接口

| 方法 | 路径 | 说明 |
| :-- | :-- | :-- |
| POST | `/v1/chat/completions` | OpenAI 兼容；`stream:true` 走 SSE 透传；支持 `reasoning_effort`、`enable_thinking` |
| GET | `/v1/models` | 模型列表（`imodel/xxx`） |
| GET | `/v1/models?detail=1` | 附带 `context` / `max_output` / `reasoning` / `tool_call` |
| GET | `/healthz` | 健康检查 |
| GET | `/admin/api/state` | 面板状态 |
| GET | `/admin/api/models?refresh=1` | 模型目录（含下线） |
| POST | `/admin/api/accounts/{id}/cooldown/clear` | 手动解除某账号冷却 |
| POST | `/admin/api/accounts/import` | 批量导入 session（校验/去重/生成设备标识） |
| POST | `/admin/api/accounts/{id}/identity/rebind` | 给单个账号换一套设备标识 |
| POST | `/admin/api/identity/rebind-all` | 给所有缺标识的账号补一套（不覆盖已有） |
| POST | `/admin/api/models/{id}/probe` | 单模型首包测活 |
| GET | `/admin/api/tasks` · POST `.../run` · POST `.../claim` | 任务中心 |
| GET | `/admin/api/pool` · POST `.../config` `/test` `/start` `/stop` · GET `.../balance` | 接码平台 |
| POST | `/admin/api/test` | 快速测活（只等首包） |

调用示例：

```bash
curl -N http://127.0.0.1:4556/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"imodel/qwen3.8-flash","stream":true,
       "reasoning_effort":"xhigh","max_tokens":4096,
       "messages":[{"role":"user","content":"你好"}]}'
```

---

## 接码平台自动入池

```
取号(POST 平台) → Loomy 发码 → 轮询取码 → 提交登录 → 入池 → 释放号码
                                        └ 超时/失败 → 拉黑该号，换下一个号重试
```

适配层是 `sms_pool.py`，路径与字段全可配，**平台账号密码由你自己在面板里填写，代码里没有任何密钥**。
默认按国内接码平台通用形态（示例为「他信 / api.my531.com」）：

```
登录  GET {base}/Login/?username={username}&password={password}&type=json
取号  GET {base}/GetPhone/?token={token}&id={project}&type=json
取码  GET {base}/GetMsg/?token={token}&id={project}&phone={phone}&type=json
释放  GET {base}/Cancel/?token={token}&id={project}&phone={phone}&type=json
拉黑  GET {base}/Addblack/?token={token}&id={project}&phone={phone}&type=json
```

响应兼容三类：JSON（任意层级的 `phone`/`code`/`sms`）、管道文本（`1|137…`、`0|错误`）、自然语言
（`【讯飞】验证码为：123456`）。`\uXXXX` 转义会先剔除，避免把转义里的数字误判成验证码。

策略参数（面板可改）：

| 字段 | 说明 |
| :-- | :-- |
| `concurrency` | 并发路数（上限 20）。**实测并发撒号命中率远高于串行** |
| `code_timeout` | 单个号码等码秒数（短一点，不来码立刻换） |
| `attempts` | 单账号最多消耗几个号码 |
| `block_prefixes` | 屏蔽号段（如 `192` 广电、`162`/`167` 虚拟号段实测收不到讯飞短信） |
| `prefer_prefixes` | 优先号段（实测 `1709524` 段命中率高） |
| `blacklist_on_fail` | 失败是否拉黑 |

---

## 本地注册器（推荐做法）

把「注册」和「跑服务」拆开：**注册在你自己的电脑上跑**，只把生成的 session 导出成文件，
再粘到服务端面板导入。好处是服务端只有**单一写入者**（不会出现脚本和守护进程互相覆盖配置），
接码平台的账号密码也完全不进服务器。

```bash
python3 loomy_registrar_gui.py                     # 打开本地界面 http://127.0.0.1:8899
python3 loomy_registrar_gui.py --cli 5 --out a.txt # 无界面注册 5 个并导出 a.txt
```

零依赖（纯标准库，任何 Python 3.8+ 都能跑）。**脚本不含任何账号密码**：接码平台地址 / 项目 ID /
账号 / 密码全部由使用者在界面里填，只保存在本机 `registrar.json`（已在 `.gitignore`）。

界面分四块：

| 区块 | 说明 |
| :-- | :-- |
| 接码平台 | 平台地址 / 项目 ID / 平台账号 / 平台密码（只存本机，脚本里为空） |
| 注册参数 | 数量、并发、等码秒数、换号次数、备注前缀、屏蔽号段 |
| 结果 | 实时表格 + 日志 |
| 导出结果 | 一键下载 `loomy_sessions_<时间>.txt`，每行 `备注\|session`；也可「复制全部」 |

把导出的文件内容贴到服务端面板「批量导入 session」，或直接调接口：

```bash
curl -X POST http://<host>/loomy/admin/api/accounts/import \
  -H "x-admin-key: <面板密码>" -H "Content-Type: application/json" \
  -d '{"text":"主号|session1\n备用|session2","validate":true,"note_prefix":"本地"}'
# → {"imported":2,"duplicated":0,"invalid":0,"total_accounts":23,...}
```

导入行为：逐条调用上游 `/points/balance` 校验、重复 session / 重复手机号自动跳过、
每条新账号自动生成独立 `devid` + `campus_device_id`、失败原因逐条返回。

## 面板内建接码（可选）

如果不想用本地注册器，服务端面板也内建了同一条流水线：
「接码平台」区块填 **平台地址 / 项目 ID / 平台账号 / 平台密码** → 保存 → 填数量 → 开始取号入池。
密码只在面板里存进 `config.json`（600 权限），**代码和脚本里没有任何内置账号密码**。

## 实测数据

> 以下均为实机测量，测试机：Ubuntu 26.04 / Python 3.14，上游 `loomyad.xunfei.cn`。

### 首包耗时（同一模型、同一句「回复OK」、stream）

| 路径 | DeepSeek-V4.1-Flash | qwen3.8-flash |
| :-- | --: | --: |
| 直连上游（绕开所有网关） | 58.6s | 94.7s |
| 本项目网关 | 37.2s | 57.3s |
| 另一个同类网关（balance 策略） | 66.8s | 24.9s |

**结论：首包由上游排队决定，波动（±30s）远大于网关之间的差异。换网关不会更快。**

### 思考档位（qwen3.8-flash，`reasoning_effort`）

| 档位 | 结果 | 首包 |
| :-- | :-- | --: |
| `none` | OK | 81.4s |
| `low` | OK | 99.6s |
| `medium` | OK | 58.6s |
| `high` | OK | 80.0s |
| `xhigh` | OK | 69.6s |

档位与耗时无单调关系——排队噪音大于档位差异。要最高思考就用 `xhigh`，但 **`max_tokens` 必须给足**
（思考 token 也算在额度内，给 512 会出现 `finish_reason=length` 且 `content` 为空）。

### 模型能力（`GET /api/v1/models`）

| 模型 | 上下文 | 最大输出 | 倍率 |
| :-- | --: | --: | :-- |
| `spark-x2.6` | 1,048,576 | 131,072 | x2.5 |
| `DeepSeek-V4.1-Flash` | 1,048,576 | 384,000 | x2.0 |
| `deepseek-v4-flash-1017` | 1,048,576 | 384,000 | x2.0 |
| `GLM-5.3-Flash` | 1,048,576 | 131,072 | x0.8 |
| `qwen3.8-flash` | 1,000,000 | 131,072 | x0.8 |
| `mimo-v2.5` | 1,024,000 | 128,000 | x3.3 |
| `doubao-seedream-5-lite` | 128,000 | 131,072 | — |
| `Hy-Image-3.5-preview` | 100,000 | 未标注 | — |
| `qwen-image-3.0-pro` | 未标注 | 未标注 | — |
| `auto` | 未标注 | 未标注 | — |

（同一模型不同账号返回的上限可能不同；面板以最近一次拉取为准，并记录上下线历史。）

### 上游错误

| 现象 | 含义 | 处理 |
| :-- | :-- | :-- |
| `503 no_provider_error, provider_name=venus` | 讯飞内部 venus 推理集群无空闲实例 | 不换号（换号没用），切兜底模型 |
| `504 openresty Gateway Time-out` | 讯飞前端网关约 60s 超时，慢模型贴边 | 同上；客户端 timeout 要 ≥180s |
| `{"code":"100002","desc":"缺少 token"}` | session 失效 | 标记账号失效并换号 |

---

## 配置项

`config.json`（首次启动自动从 `config.example.json` 复制，权限 600，已在 `.gitignore`）

| 字段 | 说明 |
| :-- | :-- |
| `listen` | 监听地址，默认 `127.0.0.1:4556`（只对本机 nginx 开放） |
| `api_keys` | 客户端使用的 key；空数组=不校验 |
| `panel_password` | 控制台密码；留空则启动时自动生成并打印 |
| `default_model` / `fallback_model` | 默认模型；上游 503/504 时自动切兜底 |
| `daily_time` / `daily_enabled` | 每日任务时间（Asia/Shanghai）与开关 |
| `balance_interval_min` | 积分与模型列表刷新间隔 |
| `strategy` | 选号策略：`balance`（默认）/ `lru` / `round_robin` |
| `cooldown_seconds` | 账号故障冷却秒数（默认 300）：401/403/402/429 或 session 失效时触发，冷却期内跳过 |
| `identity_mode` | 设备标识模式：`per_account`（默认，每号独立）/ `client`（照客户端原样 `devid=web`） |
| `max_retries` | 换号重试次数（默认 2）；仅对会话/额度类错误重试，503/504 直接切兜底模型 |
| `account_access_key_id` / `_secret` | 讯飞账号服务签名 AK/SK，留空用客户端内置那对 |
| `sms_pool` | 接码平台配置（见上节） |
| `accounts[]` | 号池（运行时写入，含 session，敏感） |

---

## 已知限制

1. **只有 OpenAI 协议**：没有 `/v1/messages`（Anthropic）、`/v1/embeddings`、`/v1/images/generations`。
2. **session 需手工续期**：约 14 天过期，面板会标红；没有做密码登录自动续期。
3. 设备标识是**每账号独立**的（`web-<16hex>`），但仍是本地生成、非真实设备指纹；需要更保守的行为可切 `identity_mode=client`。
4. **速度受上游支配**：首包 7~100 秒随机波动，网关层无法优化（见实测数据）。
5. 接码号存在「收不到短信」的号段（192 等），需要靠屏蔽号段 + 并发重试。

## 免责声明

本项目与科大讯飞无任何从属关系，仅为了与自己账号的互操作性、供个人使用。
`app.py` 里的签名常量（`DEFAULT_ACCESS_KEY_ID` / `DEFAULT_ACCESS_KEY_SECRET`）是官方客户端每个安装包
都会带的客户端常量，只用于和账号服务（`account.xfinfr.com`）通信，可在 `config.json` 里替换成你自己
客户端的值。

请勿用它滥用、转售或压垮上游服务。

完整声明（含英文版）见 [NOTICE.md](NOTICE.md)。

---

## License

MIT，见 [LICENSE](LICENSE)。
