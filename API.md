# 智慧问数系统 API 接口文档

- **服务版本**:v0.4.0(FastAPI)
- **文档更新日期**:2026-08-26
- **接口基址**:`http://<host>:<port>`
- **数据格式**:请求/响应均为 `application/json`(UTF-8);SSE 接口为 `text/event-stream`

---

## 1. 基础信息

### 1.1 接口前缀

| 前缀 | 说明 |
|---|---|
| `/api/v1` | 所有业务 API 的统一前缀,挂载在认证依赖之下 |
| `/`、`/healthz`、`/favicon.ico` | 页面与健康检查,不在 `/api/v1` 前缀内,**不受 API Key 认证约束** |

### 1.2 认证方式(API Key)

- 环境变量 `WEB_API_KEYS` **未配置(空)时,所有接口完全开放**,本地开发默认如此;
- 配置后(逗号分隔可配多个密钥),所有 `/api/v1/*` 请求**必须**携带请求头:

  ```
  X-API-Key: <密钥>
  ```

- 密钥比较使用 `hmac.compare_digest` 常量时间算法,防时序侧信道攻击;
- 未携带或错误时返回 `401`:

  ```json
  {"ok": false, "error": "缺少或错误的 API 密钥，请在请求头携带 X-API-Key。"}
  ```

- 前端页面遇到 401 会弹窗引导输入密钥并保存于浏览器 `localStorage`(key 为 `smartqa_api_key`),随后自动重试一次。

### 1.3 会话管理机制(签名 Cookie)

- 浏览器会话身份由 Cookie **`chat_session_id`** 承载,值为 itsdangerous `URLSafeTimedSerializer` 签名串(salt:`chat-session`),**并非直接暴露 session_id**;
- Cookie 安全属性:`HttpOnly`(禁 JS 读取)+ `SameSite=Lax`(抵御 CSRF)+ `Secure`(由 `SESSION_COOKIE_SECURE` 控制,HTTPS 部署时开启);
- 签名密钥 `SESSION_SECRET_KEY` 为**必填项**,长度不足 32 位时服务拒绝启动;
- 首次访问任何带会话依赖的接口时,后端自动生成 `uuid4().hex`(32 位 hex)会话 ID 并通过 `Set-Cookie` 下发;
- Cookie 缺失或签名无效(被篡改)时按无会话处理,自动新建会话,不报错;
- **注意**:会话历史默认存储在**服务进程内存**(`HISTORY_BACKEND=memory`),重启服务后丢失;切换 `HISTORY_BACKEND=redis` 可跨重启保留(受 TTL 限制,见 5.2)。

### 1.4 限流策略

滑动窗口限流(窗口 60 秒,按客户端 IP 计数,进程内存实现,多实例部署需自行替换为共享存储):

| 接口 | 环境变量 | 默认阈值 |
|---|---|---|
| `POST /api/v1/ask`、`POST /api/v1/ask_stream` | `RATE_LIMIT_ASK` | 20 次/分钟 |
| `POST /api/v1/new_chat` | `RATE_LIMIT_NEW_CHAT` | 10 次/分钟 |
| `POST /api/v1/feedback` | `RATE_LIMIT_FEEDBACK` | 30 次/分钟 |

IP 提取规则:优先取 `X-Forwarded-For` 首段,否则取直连 IP,均无则记为 `unknown`。超限返回 `429`:

```json
{"ok": false, "error": "请求过于频繁，请稍后再试。"}
```

### 1.5 通用约定

- **成功响应**业务字段直接展开(如 `{"ok": true, ...}`),**失败响应**统一结构:

  ```json
  {"ok": false, "error": "<对外友好文案>"}
  ```

- 所有响应头携带 **`X-Request-ID`**(8 位 hex),与访问日志中 `[request_id]` 对应,可用于问题追溯;
- 错误码总表:

  | 状态码 | 触发场景 | 对外文案 |
  |---|---|---|
  | 400 | 请求体校验失败(422 已统一转 400)/ 参数类业务错误 / 会话 ID 不合法 | `请求参数不合法（字段: 原因）。` / `会话 ID 不合法。` |
  | 401 | `WEB_API_KEYS` 已配置但未携带或密钥错误 | `缺少或错误的 API 密钥，请在请求头携带 X-API-Key。` |
  | 404 | 路径不存在 | `接口不存在。` |
  | 429 | 触发限流 | `请求过于频繁，请稍后再试。` |
  | 500 | 未预期异常(兜底) | `服务暂时不可用，请稍后重试。` |
  | 502 | LLM 调用超时/网络异常/限流 | `智能分析服务暂时不可用，请稍后重试。` |
  | 503 | 数据库不可达 / 配置缺失 | `数据库服务暂时不可用，请稍后重试。` / `服务配置异常，请联系管理员。` |

- 错误细节(含 SQL、表结构等内部信息)**只进服务端日志**,前端仅见脱敏文案。

---

## 2. 接口列表

### 2.1 页面访问

#### GET /

返回前端单页应用 `templates/index.html`。

- **响应**:`200`、`Content-Type: text/html`(静态文件,无模板变量)

#### GET /favicon.ico

浏览器自动请求的站点图标,返回空响应避免 404 噪音。

- **响应**:`204 No Content`

### 2.2 问答交互

#### POST /api/v1/ask

提交自然语言问题,携带当前会话历史调用 Agent,返回回答 / SQL / 数据 / 图表建议。

- **认证**:需要(`X-API-Key`,未配置 `WEB_API_KEYS` 时免)
- **限流**:`RATE_LIMIT_ASK`(默认 20 次/分钟)
- **请求体**(`AskRequest`):

  | 字段 | 类型 | 必填 | 校验规则 |
  |---|---|---|---|
  | `question` | string | 是 | strip 后非空;长度 ≤ 500 字 |
  | `user_id` | string | 否 | strip 后长度 ≤ 64 字;不传按匿名处理(向后兼容旧客户端) |

  `user_id` 为用户标识(前端自动生成或业务系统下发),首轮问答成功写入历史时把会话归属到该用户(归属首次写入即固定,不可被后续请求改挂);会话列表接口可据此按用户隔离展示(见 2.3)。

  校验失败示例 → `400`:

  ```json
  {"ok": false, "error": "请求参数不合法（question: 问题不能为空。）。"}
  ```

- **成功响应**(`200`,结构同 SSE `result` 事件的 payload):

  ```json
  {
    "ok": true,
    "question": "最近 7 天每天新增多少用户？",
    "answer": "根据查询结果，最近 7 天每天新增用户数如下……",
    "sql": "SELECT DATE(create_time) AS d, COUNT(*) FROM users WHERE ... GROUP BY d",
    "error": null,
    "data": {
      "columns": ["d", "user_count"],
      "column_aliases": {"d": "日期", "user_count": "用户数"},
      "rows": [["2026-08-12", 156], ["2026-08-13", 203]],
      "row_count": 7,
      "truncated": false
    },
    "chart": {
      "chartable": true,
      "chart_type": "bar",
      "reason": "时间趋势类问题，适合柱状图展示",
      "column_kinds": ["date", "number"]
    },
    "steps": [
      {"tool": "sql_db_schema", "input": {"table": "users"}, "output": "…（截断到 300 字）"}
    ]
  }
  ```

- **敏感拦截**(命中敏感词,`200` 但不执行查询、不写入历史):`answer` 为固定拒绝文案,`sql`/`data` 为空(见附录 6.1)
- **错误响应**:LLM 故障 `502`、数据库故障 `503`、限流 `429`,结构见 1.5

#### POST /api/v1/ask_stream

功能同 `/ask`,但以 **SSE 流式**实时推送工具执行步骤,最终 `result` 事件的 payload 与 `/ask` 响应体完全一致。

- **认证 / 限流**:同 `/ask`
- **请求体**:同 `/ask`(`AskRequest`)
- **响应头**:

  ```
  Content-Type: text/event-stream
  Cache-Control: no-cache
  X-Accel-Buffering: no        # 防反向代理缓冲破坏实时性
  ```

- **事件流**:`step_start` / `step_done` → `result`(或 `error`),格式见附录 6.2
- **注意**:认证失败、参数校验失败、限流发生在流开始**之前**,此时返回普通 JSON 错误响应(非 SSE);流开始后的异常以 `error` 事件下发后关闭连接,HTTP 状态仍为 200

### 2.3 会话管理

#### POST /api/v1/new_chat

创建全新会话并切换为当前会话(侧边栏「新对话」按钮)。

- **限流**:`RATE_LIMIT_NEW_CHAT`(默认 10 次/分钟)
- **请求体**:无
- **响应**(`200`):

  ```json
  {"ok": true, "session_id": "8a66064651f241349952c94ba97229fb"}
  ```

  同时通过 `Set-Cookie: chat_session_id=...` 下发新会话签名 Cookie。

#### GET /api/v1/sessions

历史会话列表(侧边栏),按最近更新倒序,附带当前活动会话 ID。

- **请求参数**:

  | 参数 | 位置 | 类型 | 说明 |
  |---|---|---|---|
  | `user_id` | query | string \| 不传 | 可选,长度 ≤ 64;携带时**仅返回该用户的会话**(多用户隔离展示),不传返回全部(兼容旧客户端与管理视角);当前会话取自已签名的 Cookie |

- **响应**(`200`):

  ```json
  {
    "ok": true,
    "current_session_id": "8a66064651f241349952c94ba97229fb",
    "sessions": [
      {
        "session_id": "8a66064651f241349952c94ba97229fb",
        "user_id": "u-m3k2ja-9x8w1p4q",
        "title": "最近 7 天每天新增多少用户？",
        "created_at": "2026-08-18T09:09:00.123456+00:00",
        "updated_at": "2026-08-18T09:10:00.654321+00:00",
        "message_count": 8
      }
    ]
  }
  ```

  - `user_id`:会话归属用户;提问时未携带用户标识的会话为空串(匿名)
  - `title`:首条用户问题前 30 字;无问答记录时显示 `(空会话)`
  - `message_count`:消息条数(1 轮问答 = 2 条)

#### GET /api/v1/sessions/{session_id}/messages

返回某会话的完整消息列表,供侧边栏点击回放。

- **路径参数**:

  | 参数 | 类型 | 校验 |
  |---|---|---|
  | `session_id` | string | 非空且长度 ≤ 64,否则 `400 {"ok": false, "error": "会话 ID 不合法。"}` |

- **响应**(`200`,最多返回 `HISTORY_MAX_REPLAY`(默认 50)条消息):

  ```json
  {
    "ok": true,
    "messages": [
      {"role": "human", "content": "一共有多少用户？", "ts": "2026-08-18T09:09:00.123456+00:00"},
      {
        "role": "assistant",
        "content": "共有 1,234 个用户。",
        "payload": { "ok": true, "question": "...", "answer": "...", "sql": "...", "error": null, "data": {...}, "chart": {...}, "steps": [...] },
        "ts": "2026-08-18T09:09:00.123456+00:00"
      }
    ]
  }
  ```

  - `human` 消息只含 `role` / `content` / `ts`;`assistant` 消息额外携带完整 `payload`(同 `/ask` 响应体,用于前端完整回放)

#### POST /api/v1/sessions/{session_id}/activate

把指定历史会话切换为当前活动会话(覆盖 Cookie)。

- **路径参数**:`session_id`(校验同 messages 接口)
- **请求体**:无
- **响应**(`200`):

  ```json
  {"ok": true, "session_id": "8a66064651f241349952c94ba97229fb"}
  ```

  同时更新 `Set-Cookie`。

#### DELETE /api/v1/sessions/{session_id}

删除指定会话。

- **路径参数**:`session_id`(校验同 messages 接口)
- **响应**(`200`):
  - 删除的是非当前会话:

    ```json
    {"ok": true}
    ```

  - 删除的是当前活动会话(后端顺带创建并切换到全新会话):

    ```json
    {"ok": true, "session_id": "<新会话ID>"}
    ```

### 2.4 反馈收集

#### POST /api/v1/feedback

提交回答评价(👍/👎),追加写入项目根目录 `feedback.jsonl`。

- **限流**:`RATE_LIMIT_FEEDBACK`(默认 30 次/分钟)
- **请求体**(`FeedbackRequest`):

  | 字段 | 类型 | 必填 | 校验规则 |
  |---|---|---|---|
  | `rating` | string | 是 | 只能为 `"up"` 或 `"down"` |
  | `question` | string | 是 | strip 后非空(落盘截断到 500 字) |
  | `sql` | string \| null | 否 | 落盘截断到 2000 字,空则存 null |
  | `comment` | string \| null | 否 | 落盘截断到 500 字,空则存 null |
  | `user_id` | string | 否 | strip 后长度 ≤ 64 字;不传按匿名处理 |

- **成功响应**(`200`):

  ```json
  {"ok": true}
  ```

- **落盘记录结构**(每行一个 JSON):

  ```json
  {"ts": "2026-08-18T09:11:00.000000+00:00", "session_id": "...", "user_id": "u-m3k2ja-9x8w1p4q", "rating": "up", "question": "...", "sql": "...", "comment": null}
  ```

### 2.5 健康检查

#### GET /healthz

数据库连通性(`SELECT 1`)+ LLM 配置状态检查。**无需 API Key**。

- **响应**:

  | 状态 | HTTP | 响应体 |
  |---|---|---|
  | 正常 | 200 | `{"status": "ok", "db": "ok", "llm_configured": true}` |
  | DB 正常但未配置 LLM | 200 | `{"status": "degraded", "db": "ok", "llm_configured": false}` |
  | DB 配置缺失 | 503 | `{"status": "error", "db": "unconfigured"}` |
  | DB 不可达 | 503 | `{"status": "error", "db": "unavailable"}` |

---

## 3. 数据模型

### 3.1 AskRequest(提问请求体)

```json
{
  "question": "string，必填，strip 后非空且 ≤ 500 字",
  "user_id": "string，可选，用户标识，≤ 64 字，不传按匿名处理"
}
```

### 3.2 FeedbackRequest(反馈请求体)

```json
{
  "rating": "string，必填，仅允许 \"up\" | \"down\"",
  "question": "string，必填，strip 后非空",
  "sql": "string | null，可选",
  "comment": "string | null，可选",
  "user_id": "string，可选，用户标识，≤ 64 字"
}
```

### 3.3 AskPayload(`/ask` 响应体,亦为 SSE `result` 事件 payload 与回放 `payload`)

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | 恒为 `true`(失败走错误结构,不回此体) |
| `question` | string | 用户原始问题 |
| `answer` | string | 回答正文(Markdown);敏感拒绝时为固定文案 |
| `sql` | string \| null | 最后一轮工具调用提取的 SQL;无则为 null |
| `error` | null | 成功恒为 null(兼容字段) |
| `data` | object | `{columns: [string], column_aliases: {string: string}, rows: [[any]], row_count: int, truncated: bool}`,无查询结果时为空结构;`column_aliases` 为列名中文别名表(见 6.4) |
| `chart` | object | 图表建议,见 3.4 |
| `steps` | array | 工具调用过程,见 3.5 |

### 3.4 ChartSuggestion(图表建议)

| 字段 | 类型 | 说明 |
|---|---|---|
| `chartable` | bool | 是否可绘图 |
| `chart_type` | string \| null | `"bar"` / `"line"` / `"pie"`,不可绘时为 null |
| `reason` | string | 推断理由 |
| `column_kinds` | array | 逐列类型推断(`date`/`number`/`category`),与列一一对应 |

### 3.5 StepSummary(工具调用步骤)

| 字段 | 类型 | 说明 |
|---|---|---|
| `tool` | string | 工具名(如 `sql_db_schema`、`sql_db_query`) |
| `input` | any | 工具输入参数 |
| `output` | string | 工具输出,**截断到 300 字**(超长补 ` …(已截断)`) |

### 3.6 SessionSummary(会话列表项)

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string | 32 位 hex |
| `user_id` | string | 会话归属用户;提问时未携带用户标识的会话为空串(匿名) |
| `title` | string | 首条用户问题前 30 字;无记录时为 `(空会话)` |
| `created_at` | string | 创建时间(ISO 8601, UTC) |
| `updated_at` | string | 最近更新时间(ISO 8601, UTC) |
| `message_count` | int | 消息总条数 |

### 3.7 HistoryMessage(回放消息)

| 字段 | 类型 | 说明 |
|---|---|---|
| `role` | string | `"human"` 或 `"assistant"` |
| `content` | string | 问题原文 / 回答正文 |
| `payload` | object | **仅 assistant 消息携带**,完整 `AskPayload`,供前端完整回放 |
| `ts` | string | 写入时间(ISO 8601, UTC) |

---

## 4. 安全规范

### 4.1 API Key 使用

- 生产环境务必配置 `WEB_API_KEYS`(逗号分隔可配多个,支持密钥轮换过渡期双密钥并存);
- 密钥经 `hmac.compare_digest` 常量时间比较,防时序攻击;
- 前端密钥仅存浏览器 `localStorage`,随每个 `/api/v1/*` 请求以 `X-API-Key` 头发送。

### 4.2 XSS 防护(前端)

- 所有动态内容(会话标题、SQL、表格数据、步骤输出)经 `escapeHtml` 转义后插入 DOM;
- LLM 输出 Markdown 经 `marked` 渲染后再过 `DOMPurify` 净化,防提示注入类 XSS;
- 后端回放的 `payload` 同样视为不可信输入,走同一套转义/净化链路。

### 4.3 限流阈值

默认值见 1.4 表;按 IP 滑动窗口计数。多实例部署时限流状态不共享,建议在网关层(如 Nginx)或共享存储(Redis)实现等效限流。

### 4.4 其他

- **会话 Cookie 签名**:`SESSION_SECRET_KEY` 至少 32 位随机串,缺失则拒绝启动;签名无效的 Cookie 按无会话处理,不信任客户端可改值;
- **错误脱敏**:异常 detail(可能含 SQL、表结构)只进日志;前端仅收统一文案;
- **敏感查询拦截**:命中敏感词的问题不进入 Agent、不执行 SQL、不写入历史(见 6.1);
- **数据库只读**:生产数据库账号应仅授 `SELECT`(README 中有授权示例)。

---

## 5. 部署说明

### 5.1 启动命令

```bash
# 本地开发(单进程)
python app.py                        # http://127.0.0.1:5000

# 生产(Linux,多 worker)
uvicorn app:app --host 0.0.0.0 --port 5000 --workers 2
```

要点:

- Agent 单次提问可达 10~60 秒,uvicorn 无请求超时限制,无需额外配置;
- **多 worker 前必须**将 `HISTORY_BACKEND` 切换为 `redis`,否则同一浏览器的请求落到不同 worker 会丢失会话上下文(内存后端不跨进程共享);
- 建议 systemd / supervisor 托管,日志输出 stdout 由其收集。

### 5.2 环境变量配置项

完整模板见 `.env.example`,关键项如下:

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| **数据库** | | |
| `DB_HOST` / `DB_PORT` | — / `3306` | MySQL 地址 |
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` | — | 只读账号凭据与库名 |
| `DB_SSL_DISABLED` | `true` | 是否禁用 SSL |
| `QUERY_TIMEOUT_MS` | `15000` | 只读查询会话级超时(毫秒) |
| `DB_CONNECT_TIMEOUT` | `10` | 建连超时(秒) |
| `SCHEMA_CACHE_TTL` | `300` | schema 元数据缓存时长(秒),0 禁用 |
| **大模型** | | |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | — / DeepSeek / `deepseek-chat` | LLM 凭据与模型 |
| `LLM_TEMPERATURE` / `LLM_TIMEOUT` | `0.0` / `60` | 温度 / 调用超时(秒) |
| **Web 服务** | | |
| `SESSION_SECRET_KEY` | **必填** | 会话 Cookie 签名密钥,≥32 位 |
| `SESSION_COOKIE_SECURE` | `false` | HTTPS 部署后置 `true` |
| `RATE_LIMIT_ASK` / `RATE_LIMIT_NEW_CHAT` / `RATE_LIMIT_FEEDBACK` | `20` / `10` / `30` | 每 IP 每分钟上限 |
| `WEB_API_KEYS` | 空(开放) | 逗号分隔 API 密钥;配置后强制 `X-API-Key` |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `5000` | `python app.py` 监听地址 |
| **会话历史存储** | | |
| `HISTORY_BACKEND` | `memory` | `memory`(进程内,重启丢失)/ `redis`(跨重启,多 worker 共享) |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接串 |
| `HISTORY_TTL_SECONDS` | `604800` | Redis 中会话过期时间(秒);激活/回放会话时自动续期 |
| `HISTORY_MAX_MESSAGES` | `6` | LLM 上下文最多携带的最近消息条数 |
| `HISTORY_MAX_REPLAY` | `50` | 每会话完整回放消息条数上限 |

---

## 6. 附录

### 6.1 敏感词拦截机制

- 用户问题命中以下任一关键词(**大小写不敏感**)时,直接拒绝、不进入 Agent、不生成任何 SQL、不写入会话历史,HTTP 200 返回:

  ```
  凭证/密钥类:密码、口令、密钥、秘钥、私钥、盐值、令牌、token、凭据、凭证、apikey、api key
  身份/联系类:身份证、手机号、电话号码、联系电话、座机号、邮箱、电邮、email、邮件地址
  ```

- 拦截响应示例(`/ask`,HTTP 200):

  ```json
  {
    "ok": true,
    "question": "查一下用户的密码",
    "answer": "出于数据安全与隐私保护要求，系统不会执行涉及密码、密钥、认证令牌、身份证号、手机号、邮箱地址等敏感隐私字段的查询。请调整您的问题，避免涉及上述敏感隐私数据后重试。",
    "sql": null,
    "error": null,
    "data": {"columns": [], "column_aliases": {}, "rows": [], "row_count": 0, "truncated": false},
    "chart": {"chartable": false, "chart_type": null, "reason": "没有可用的查询结果数据", "column_kinds": []},
    "steps": []
  }
  ```

- 该机制为**第一道防线**;系统提示词中同时约束 LLM 不得查询敏感字段(第二道防线);SQL 层另有只读校验护栏(不限于敏感字段)。

### 6.2 SSE 流式事件格式

`POST /api/v1/ask_stream` 的响应体为 SSE 流,每条事件形如:

```
data: <JSON>\n\n
```

事件类型与结构:

| 事件 | JSON 结构 | 说明 |
|---|---|---|
| `step_start` | `{"type": "step_start", "tool": "sql_db_schema"}` | Agent 声明调用某工具 |
| `step_done` | `{"type": "step_done", "tool": "sql_db_query", "output": "…"}` | 工具执行完成,输出**截断到 200 字** |
| `result` | `{"type": "result", "payload": {…}}` | 最终结果,`payload` 同 `/ask` 响应体(见 3.3),是流的最后一个正常事件 |
| `error` | `{"type": "error", "error": "对外文案"}` | 流中途异常,下发后关闭连接 |

完整示例:

```
data: {"type": "step_start", "tool": "sql_db_schema"}

data: {"type": "step_done", "tool": "sql_db_schema", "output": "{\"tables\": ...}"}

data: {"type": "step_start", "tool": "sql_db_query"}

data: {"type": "step_done", "tool": "sql_db_query", "output": "[{\"a\": 1}] …(已截断)"}

data: {"type": "result", "payload": {"ok": true, "question": "...", "answer": "...", ...}}
```

### 6.3 Token 用量与访问日志示例

- 每轮问答完成时记录(仅服务端日志,不回传前端):

  ```
  2026-08-18 09:09:10,226 INFO [smart_qa_web] [f666f1c1] 提问完成: tokens(in/out)=7577/443 工具调用=3次 Agent耗时=6141ms
  ```

- 访问日志(每条请求一行,含 request_id 与耗时):

  ```
  2026-08-18 09:08:51,503 INFO [smart_qa_web] [f0e04afd] GET / -> 200 (14ms)
  ```

- 限流拦截日志:

  ```
  限流拦截: 192.168.1.10 -> /api/v1/ask
  ```

### 6.4 列中文别名机制(column_aliases)

查询结果列名(如 `total_sales`、`order_count`)由后端在结果组装环节(`app._build_ask_payload`)自动注入中文别名,避免前端界面出现英文字段名。

- **响应字段**:`data.column_aliases`,对象结构 `{原始列名: 中文别名}`,**仅包含命中别名的列**;未命中的列不产生条目,前端回退原始列名(向后兼容不含该字段的历史会话 payload);
- **别名来源**(两层):
  - **Agent 上报**:查询执行成功后 Agent 调用 `report_column_aliases` 工具,结合问题意图、字段备注与聚合语义为每列上报中文展示名(如问销售额时 `pay_amount` 合计命名为"销售额"),最贴合场景;
  - **配置兜底**:`config/column_aliases.yaml`(YAML 子集格式,零新增依赖),两个小节:
    - `exact`:精确别名,列名完全一致即命中(大小写不敏感),如 `total_sales: 总销售额`;
    - `tokens`:分词词典,`exact` 未命中时把 snake_case 列名按 `_` 分词、逐词翻译拼接合成别名(需全部词命中才合成,如 `user_count` → `用户数`);
- **解析优先级**:Agent 上报别名 → 精确别名 → 分词合成 → 回退原始列名;Agent 上报值经校验(非空字符串、≤12 字、不含换行、必须属于结果列),非法值丢弃后回退配置;
- **前端渲染**:ECharts 图例 / 系列名 / X 轴名与数据表格列头一律优先使用别名;表头 `title` 属性保留原始字段名便于核对 SQL;图表标题为用户问题原文,不受影响;
- **热加载**:配置按文件 mtime 缓存,修改后无需重启服务;配置文件缺失时整体降级为原始列名,不影响现有行为。
