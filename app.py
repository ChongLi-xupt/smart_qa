"""智慧问数 Web 服务：FastAPI 后端 + ECharts 前端渲染。

接口说明：
    GET  /                  前端页面（templates/index.html）
    POST /api/v1/ask        提交自然语言问题，返回回答 / SQL / 数据 / 图表建议
    POST /api/v1/ask_stream 同上，但以 SSE 流式推送工具步骤与最终结果（P3-15）
    POST /api/v1/new_chat   创建全新会话并切换（侧边栏「新对话」）
    POST /api/v1/feedback   提交回答评价（👍/👎），沉淀到 feedback.jsonl（P3-18）
    GET  /api/v1/sessions            历史会话列表（侧边栏）
    GET  /api/v1/sessions/{id}/messages   某会话完整消息回放
    POST /api/v1/sessions/{id}/activate   切换当前活动会话
    DELETE /api/v1/sessions/{id}          删除会话
    GET  /healthz           健康检查（数据库连通性 + LLM 配置状态）

配置方式与 smart_qa.py 保持一致，通过项目根目录 .env 或环境变量读取：
    LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
    DB_HOST / DB_USER / DB_PASSWORD / DB_NAME / DB_PORT
    QUERY_TIMEOUT_MS（只读查询会话级超时，默认 15000）
    SESSION_SECRET_KEY（必填，≥ 32 位）/ SESSION_COOKIE_SECURE
    RATE_LIMIT_ASK / RATE_LIMIT_NEW_CHAT / RATE_LIMIT_FEEDBACK（每 IP 每分钟上限）
    WEB_API_KEYS（可选，逗号分隔；设置后 /api/v1/* 需携带 X-API-Key，P3-19）

启动::

    python app.py                              # 本地开发
    uvicorn app:app --host 0.0.0.0 --port 5000 --workers 2   # 生产（Linux）
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from chart_builder import recommend_chart
from chat_store import create_history_store
from column_aliases import merge_column_aliases
from qa_exceptions import ParameterError, SmartQAError
from smart_qa import SmartQA, _config_from_env

# ---------------------------------------------------------------------- #
# 日志：request_id 全链路追踪
# ---------------------------------------------------------------------- #

# 请求上下文（替代 Flask g）：纯 ASGI 中间件在请求期设置，日志过滤器读取
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    """为每条日志附加当前请求的 request_id（非请求上下文显示 '-'）。"""

    def filter(self, record):
        record.request_id = _request_id_var.get()
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s",
)
logger = logging.getLogger("smart_qa_web")
# 格式串含 %(request_id)s，而第三方日志不带该字段。
# logger 级过滤器只作用于本 logger 创建的记录，对子 logger 传播上来的
# 记录无效；handler 级过滤器才会对到达该 handler 的每条记录生效，
# 因此必须挂到根 handler，否则启动/第三方日志会 KeyError 崩溃。
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RequestIdFilter())


class RequestContextMiddleware:
    """纯 ASGI 中间件：绑定 request_id、写 X-Request-ID 响应头、记录访问日志。

    不用 BaseHTTPMiddleware：它会缓冲请求/响应体，破坏 SSE 实时推送。
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:8]
        token = _request_id_var.set(request_id)
        started = time.monotonic()
        status_codes: list[int] = []
        try:

            async def send_with_request_id(message):
                if message["type"] == "http.response.start":
                    status_codes.append(message["status"])
                    MutableHeaders(scope=message)["X-Request-ID"] = request_id
                await send(message)

            await self._app(scope, receive, send_with_request_id)
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "%s %s -> %s (%dms)",
                scope.get("method"),
                scope.get("path"),
                status_codes[0] if status_codes else "-",
                duration_ms,
            )
            _request_id_var.reset(token)


# ---------------------------------------------------------------------- #
# 应用初始化
# ---------------------------------------------------------------------- #

app = FastAPI(title="智慧问数", version="0.4.0")
app.add_middleware(RequestContextMiddleware)

# session cookie 签名密钥必须从环境变量 / .env 注入，源码中不再保留默认值，
# 避免签名可被伪造。
_secret_key = os.getenv("SESSION_SECRET_KEY", "")
if len(_secret_key) < 32:
    raise RuntimeError(
        "未设置 SESSION_SECRET_KEY（或长度不足 32 位），拒绝启动。\n"
        "请在 .env 中配置，生成方式：\n"
        '    python -c "import secrets; print(secrets.token_hex(32))"'
    )
_session_serializer = URLSafeTimedSerializer(_secret_key, salt="chat-session")

_SESSION_COOKIE_NAME = "chat_session_id"

# Agent 及其依赖均可安全并发复用：SQLAlchemy 连接池线程安全，
# ChatOpenAI 无共享可变状态，编译后的 LangGraph 图可并发 invoke，
# SmartQA.ask 每次调用内部新建消息列表与 config。因此不设全局串行锁，
# 仅在懒加载初始化处保留细粒度锁。
_qa: SmartQA | None = None
_init_lock = threading.Lock()

# 会话对话历史外置为可插拔存储（P2/A4）：默认进程内存，
# HISTORY_BACKEND=redis 时切 Redis，支持多 worker 共享。
_history_store = create_history_store()


def get_qa() -> SmartQA:
    """懒加载 SmartQA 实例（首次请求时初始化，读取环境变量配置）。"""
    global _qa
    if _qa is None:
        with _init_lock:
            if _qa is None:
                db_config, llm_config = _config_from_env()
                _qa = SmartQA(db_config, llm_config)
    return _qa


# ---------------------------------------------------------------------- #
# 会话 cookie：itsdangerous 签名，HttpOnly + SameSite=Lax
# ---------------------------------------------------------------------- #


def _cookie_flags() -> dict[str, Any]:
    """会话 cookie 安全配置：
    - httponly：禁止 JS 读取 cookie；
    - samesite=Lax：抵御大部分跨站请求伪造；
    - secure：部署到 HTTPS 后在 .env 中设为 true，本地 http 调试保持 false
      （否则浏览器不会在明文连接上下发 cookie）。"""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    }


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=_session_serializer.dumps(session_id),
        **_cookie_flags(),
    )


def _read_session_cookie(request: Request) -> str | None:
    """读取并校验签名 cookie 中的会话 ID（缺失/伪造时返回 None）。"""
    raw = request.cookies.get(_SESSION_COOKIE_NAME)
    if not raw:
        return None
    try:
        session_id = _session_serializer.loads(raw)
    except BadSignature:
        return None
    return session_id if isinstance(session_id, str) and session_id else None


def get_session_id(request: Request, response: Response) -> str:
    """取当前浏览器的会话 ID，不存在则创建（写入签名 cookie）。"""
    session_id = _read_session_cookie(request)
    if not session_id:
        session_id = uuid.uuid4().hex
        _set_session_cookie(response, session_id)
    return session_id


def _get_history(session_id: str):
    """取会话历史快照副本（由存储后端保证线程安全与 LRU/TTL）。"""
    return _history_store.get(session_id)


def _summarize_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """精简工具调用过程，避免把超长输出传给前端。"""
    summarized = []
    for step in steps:
        output = step.get("output")
        if not isinstance(output, str):
            output = str(output)
        if len(output) > 300:
            output = output[:300] + " …(已截断)"
        summarized.append(
            {
                "tool": step.get("tool"),
                "input": step.get("input"),
                "output": output,
            }
        )
    return summarized


# ---------------------------------------------------------------------- #
# 接口限流：按客户端 IP 的滑动窗口计数（线程安全，无外部依赖）。
# 多实例部署时建议替换为共享存储限流。
# ---------------------------------------------------------------------- #


class RateLimiter:
    """每 ``window_seconds`` 秒内，同一 IP 最多允许 ``max_calls`` 次请求。"""

    def __init__(self, max_calls: int, window_seconds: int = 60) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.max_calls:
                return False
            hits.append(now)
            # 防止 IP 记录无限增长：字典过大时清理已无请求的 key
            if len(self._hits) > 10000:
                for stale_key in [k for k, q in self._hits.items() if not q]:
                    self._hits.pop(stale_key, None)
            return True


_ask_limiter = RateLimiter(
    max_calls=int(os.getenv("RATE_LIMIT_ASK", "20")), window_seconds=60
)
_new_chat_limiter = RateLimiter(
    max_calls=int(os.getenv("RATE_LIMIT_NEW_CHAT", "10")), window_seconds=60
)
_feedback_limiter = RateLimiter(
    max_calls=int(os.getenv("RATE_LIMIT_FEEDBACK", "30")), window_seconds=60
)


def make_rate_limit_dependency(limiter: RateLimiter):
    """把限流器包装为 FastAPI 依赖（超限抛 429，由全局 handler 转结构化文案）。"""

    def _rate_limit(request: Request) -> None:
        forwarded = request.headers.get("X-Forwarded-For", "")
        client_host = request.client.host if request.client else ""
        client_ip = forwarded.split(",")[0].strip() or client_host or "unknown"
        if not limiter.allow(client_ip):
            logger.warning("限流拦截: %s -> %s", client_ip, request.url.path)
            raise StarletteHTTPException(
                status_code=429, detail="请求过于频繁，请稍后再试。"
            )

    return _rate_limit


_ask_rate_limit = make_rate_limit_dependency(_ask_limiter)
_new_chat_rate_limit = make_rate_limit_dependency(_new_chat_limiter)
_feedback_rate_limit = make_rate_limit_dependency(_feedback_limiter)


# ---------------------------------------------------------------------- #
# 认证（P3-19）：WEB_API_KEYS 未配置时完全开放（本地开发体验不变）；
# 配置后（逗号分隔多个）/api/v1/* 一律要求 X-API-Key 请求头。
# 企业内后续可在此基础上接入 SSO/OAuth2 网关。
# ---------------------------------------------------------------------- #

_API_KEYS = frozenset(
    key.strip() for key in os.getenv("WEB_API_KEYS", "").split(",") if key.strip()
)


def require_api_key(request: Request) -> None:
    """API Key 认证依赖：密钥比较必须常量时间，防时序侧信道攻击。"""
    if not _API_KEYS:
        return None
    provided = request.headers.get("X-API-Key", "")
    if any(hmac.compare_digest(provided, valid) for valid in _API_KEYS):
        return None
    raise StarletteHTTPException(
        status_code=401, detail="缺少或错误的 API 密钥，请在请求头携带 X-API-Key。"
    )


# ---------------------------------------------------------------------- #
# 用户反馈（P3-18）：追加写入 feedback.jsonl，经人工核对后可回流 rag/examples.jsonl
# ---------------------------------------------------------------------- #

_FEEDBACK_FILE = Path(__file__).resolve().parent / "feedback.jsonl"
_feedback_lock = threading.Lock()

# 问题输入长度上限：防止超长文本消耗 LLM token
_MAX_QUESTION_LENGTH = 500
_MAX_SESSION_ID_LENGTH = 64


# ---------------------------------------------------------------------- #
# 请求体模型：schema 校验替代手工解析，校验错误统一转 400 结构化响应
# ---------------------------------------------------------------------- #


class AskRequest(BaseModel):
    """POST /ask 与 /ask_stream 的请求体。"""

    question: str

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空。")
        if len(value) > _MAX_QUESTION_LENGTH:
            raise ValueError(
                f"问题过长（上限 {_MAX_QUESTION_LENGTH} 字），请精简后重试。"
            )
        return value


class FeedbackRequest(BaseModel):
    """POST /feedback 的请求体。"""

    rating: str
    question: str = ""
    sql: str | None = None
    comment: str | None = None

    @field_validator("rating")
    @classmethod
    def _validate_rating(cls, value: str) -> str:
        if value not in ("up", "down"):
            raise ValueError("rating 必须是 up 或 down。")
        return value

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question 不能为空。")
        return value


def _check_session_id(session_id: str) -> str:
    """会话 ID 只接受短字符串（内部生成的是 32 位 hex）。"""
    if not session_id or len(session_id) > _MAX_SESSION_ID_LENGTH:
        raise ParameterError("会话 ID 不合法。")
    return session_id


# ---------------------------------------------------------------------- #
# 分层异常处理：细节只进服务端日志，前端只看到统一友好文案
# ---------------------------------------------------------------------- #


@app.exception_handler(SmartQAError)
async def _handle_qa_error(request: Request, exc: SmartQAError) -> JSONResponse:
    logger.error("%s: %s", type(exc).__name__, exc.detail)
    # 参数类错误的 detail 是为用户准备的校验提示（不含内部信息），直接展示；
    # 其余异常的 detail 可能含 SQL/表结构，只进日志，前端用统一文案。
    message = exc.public_message
    if isinstance(exc, ParameterError) and exc.detail:
        message = exc.detail
    return JSONResponse(status_code=exc.http_code, content={"ok": False, "error": message})


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # FastAPI 默认 422，这里统一转 400 + 结构化文案，与前端/测试约定保持一致
    message = "请求参数不合法。"
    errors = exc.errors()
    if errors:
        first = errors[0]
        location = ".".join(
            str(part) for part in (first.get("loc") or []) if part != "body"
        )
        detail = str(first.get("msg") or "").replace("Value error, ", "")
        if location and detail:
            message = f"请求参数不合法（{location}: {detail}）。"
    return JSONResponse(status_code=400, content={"ok": False, "error": message})


@app.exception_handler(StarletteHTTPException)
async def _handle_http_exception(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    # 401/404/405/429 等框架级 HTTP 异常统一为结构化响应
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if exc.status_code == 404:
        detail = "接口不存在。"
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": detail})


@app.exception_handler(Exception)
async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未预期异常")
    return JSONResponse(
        status_code=500, content={"ok": False, "error": "服务暂时不可用，请稍后重试。"}
    )


# ---------------------------------------------------------------------- #
# 页面与健康检查
# ---------------------------------------------------------------------- #

_INDEX_HTML = Path(__file__).resolve().parent / "templates" / "index.html"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """前端单页应用（无模板变量，直接返回静态 HTML）。"""
    return FileResponse(_INDEX_HTML, media_type="text/html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """浏览器自动请求的站点图标：空响应避免 404 噪音。"""
    return Response(status_code=204)


@app.get("/healthz", response_model=None)
def healthz() -> JSONResponse | dict[str, Any]:
    """健康检查：数据库连通性 + LLM 配置状态。"""
    detail: dict[str, Any] = {"status": "ok"}
    try:
        database = get_qa().database
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        detail["db"] = "ok"
    except SmartQAError as exc:
        logger.error("健康检查失败（配置）: %s", exc.detail)
        return JSONResponse(
            status_code=503, content={"status": "error", "db": "unconfigured"}
        )
    except Exception:  # noqa: BLE001 - 健康检查需兜底
        logger.exception("健康检查: 数据库不可达")
        return JSONResponse(
            status_code=503, content={"status": "error", "db": "unavailable"}
        )

    detail["llm_configured"] = bool(os.getenv("LLM_API_KEY"))
    if not detail["llm_configured"]:
        detail["status"] = "degraded"
    return detail


# ---------------------------------------------------------------------- #
# API 路由：统一挂在 api_router 下（认证依赖一次声明全部生效）
# ---------------------------------------------------------------------- #

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


def _finalize_result(session_id: str, question: str, result: dict[str, Any]) -> None:
    """提问完成后的公共收尾：写会话历史（完整 payload）+ token/耗时日志。"""
    # 成功的回答写入会话历史：完整 payload 供侧边栏回放，
    # LLM 上下文由存储层按 HISTORY_MAX_MESSAGES 截取
    if result.get("error") is None and not result.get("rejected"):
        _history_store.append(session_id, question, _build_ask_payload(question, result))

    # token 用量与耗时只进服务端日志，便于成本核算与性能分析
    # （敏感拒绝路径不经过 Agent，无 usage 字段时跳过）
    usage = result.get("usage")
    if usage:
        logger.info(
            "提问完成: tokens(in/out)=%s/%s 工具调用=%s次 Agent耗时=%sms",
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("tool_calls"),
            result.get("duration_ms"),
        )


def _build_ask_payload(question: str, result: dict[str, Any]) -> dict[str, Any]:
    """组装 /ask 与 /ask_stream 共用的最终响应体（数据 + 图表 + 精简步骤）。

    data.column_aliases 为列名中文别名表：Agent 上报建议优先、
    config/column_aliases.yaml 配置兜底；前端图表图例/坐标轴/表头
    优先渲染别名，无别名时回退原始列名。
    """
    data = result.get("data") or {}
    columns = data.get("columns") or []
    rows = data.get("rows") or []
    return {
        "ok": True,
        "question": question,
        "answer": result.get("answer") or "",
        "sql": result.get("sql"),
        "error": None,
        "data": {
            "columns": columns,
            "column_aliases": merge_column_aliases(
                columns, result.get("suggested_column_aliases")
            ),
            "rows": rows,
            "row_count": data.get("row_count", len(rows)),
            "truncated": data.get("truncated", False),
        },
        "chart": recommend_chart(question, columns, rows),
        "steps": _summarize_steps(result.get("intermediate_steps") or []),
    }


def _sse(event: dict[str, Any]) -> str:
    """把事件序列化为一条 SSE 消息（``data: <json>\\n\\n``）。"""
    return "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"


@api_router.post("/ask", dependencies=[Depends(_ask_rate_limit)])
def ask(
    payload: AskRequest,
    session_id: str = Depends(get_session_id),
) -> dict[str, Any]:
    """接收自然语言问题，携带当前会话历史调用 Agent 并返回结构化结果。"""
    question = payload.question
    history = _get_history(session_id)

    # 分层异常（配置/LLM/数据库/参数）由全局 exception_handler 统一处理
    result = get_qa().ask(question, chat_history=history)
    _finalize_result(session_id, question, result)
    return _build_ask_payload(question, result)


@api_router.post("/ask_stream", dependencies=[Depends(_ask_rate_limit)])
def ask_stream(
    payload: AskRequest,
    session_id: str = Depends(get_session_id),
) -> StreamingResponse:
    """SSE 流式提问（P3-15）：实时推送工具步骤，最终 result 事件结构同 /ask。

    事件流：step_start / step_done → result（payload 同 /ask 响应体）；
    异常时补发 error 事件后关闭。客户端 POST 后逐块读取响应体，
    每条事件形如 ``data: <JSON>\\n\\n``。
    """
    question = payload.question
    history = _get_history(session_id)

    def generate():
        try:
            for event in get_qa().ask_stream(question, chat_history=history):
                if event.get("type") == "result":
                    result = event["result"]
                    _finalize_result(session_id, question, result)
                    yield _sse(
                        {"type": "result", "payload": _build_ask_payload(question, result)}
                    )
                else:
                    yield _sse(event)
        except SmartQAError as exc:
            logger.error("ask_stream %s: %s", type(exc).__name__, exc.detail)
            message = exc.public_message
            if isinstance(exc, ParameterError) and exc.detail:
                message = exc.detail
            yield _sse({"type": "error", "error": message})
        except Exception:  # noqa: BLE001 - 流式响应中错误必须转为事件下发
            logger.exception("ask_stream 未预期异常")
            yield _sse({"type": "error", "error": "服务暂时不可用，请稍后重试。"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 防代理缓冲破坏实时性
        },
    )


@api_router.post("/new_chat", dependencies=[Depends(_new_chat_rate_limit)])
def new_chat(response: Response) -> dict[str, Any]:
    """创建全新会话并切换（侧边栏「新对话」）。"""
    session_id = uuid.uuid4().hex
    _set_session_cookie(response, session_id)
    return {"ok": True, "session_id": session_id}


@api_router.post("/feedback", dependencies=[Depends(_feedback_rate_limit)])
def feedback(
    payload: FeedbackRequest,
    session_id: str = Depends(get_session_id),
) -> dict[str, Any]:
    """收集回答评价并沉淀到 feedback.jsonl（P3-18 反馈闭环）。

    点赞样本经人工核对后可整理进 rag/examples.jsonl 成为 few-shot 语料，
    点踩样本用于改进提示词与 schema 标注，形成训练闭环。
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "rating": payload.rating,
        "question": payload.question[:_MAX_QUESTION_LENGTH],
        "sql": (payload.sql or "")[:2000] or None,
        "comment": (payload.comment or "")[:500] or None,
    }
    line = json.dumps(record, ensure_ascii=False)
    with _feedback_lock:
        with _FEEDBACK_FILE.open("a", encoding="utf-8") as feedback_file:
            feedback_file.write(line + "\n")
    logger.info("收到反馈: %s -> %s", payload.rating, payload.question[:50])
    return {"ok": True}


@api_router.get("/sessions")
def list_sessions(
    request: Request,
    session_id: str = Depends(get_session_id),
) -> dict[str, Any]:
    """历史会话列表（侧边栏）：按最近更新倒序，附带当前活动会话 ID。"""
    return {
        "ok": True,
        "current_session_id": session_id,
        "sessions": _history_store.list_sessions(),
    }


@api_router.get("/sessions/{session_id}/messages")
def get_session_messages(session_id: str) -> dict[str, Any]:
    """某会话的完整消息列表（供侧边栏点击回放）。"""
    _check_session_id(session_id)
    _history_store.touch(session_id)  # 回放即视为活跃，续期 TTL（Redis 后端）
    return {"ok": True, "messages": _history_store.get_messages(session_id)}


@api_router.post("/sessions/{session_id}/activate")
def activate_session(session_id: str, response: Response) -> dict[str, Any]:
    """把指定历史会话切换为当前活动会话（覆盖 cookie）。"""
    _check_session_id(session_id)
    _set_session_cookie(response, session_id)
    _history_store.touch(session_id)  # 激活即视为活跃，续期 TTL（Redis 后端）
    return {"ok": True, "session_id": session_id}


@api_router.delete("/sessions/{session_id}")
def delete_session(
    session_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """删除会话；删除的是当前活动会话时顺带切换为全新会话。"""
    _check_session_id(session_id)
    _history_store.delete_session(session_id)
    result: dict[str, Any] = {"ok": True}
    if _read_session_cookie(request) == session_id:
        new_id = uuid.uuid4().hex
        _set_session_cookie(response, new_id)
        result["session_id"] = new_id
    return result


app.include_router(api_router)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "5000"))
    print(f"智慧问数 Web 服务启动中: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
