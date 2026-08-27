"""会话对话历史存储（P2/A4）：内存 / Redis 双后端。

- 内存后端（默认）：OrderedDict + LRU，单进程部署、本地开发使用；
- Redis 后端：多 worker / 多实例共享，支持水平扩展，历史按会话 TTL 过期。

每个会话保存完整消息列表（含每轮回答的完整 payload，供侧边栏回放）、
归属用户（user_id，提问时由前端携带，空串表示匿名）与会话元数据
（title / 时间戳）；会话列表可按 user_id 过滤，实现多用户隔离展示。
LLM 上下文只取最近 ``HISTORY_MAX_MESSAGES`` 条，完整回放受
``HISTORY_MAX_REPLAY`` 条数限制。

通过环境变量切换::

    HISTORY_BACKEND=memory|redis   （默认 memory）
    REDIS_URL=redis://127.0.0.1:6379/0
    HISTORY_TTL_SECONDS=7200       （Redis 中会话过期时间）
    HISTORY_MAX_MESSAGES=6         （LLM 上下文中最多保留的消息条数）
    HISTORY_MAX_REPLAY=50          （每会话完整回放的最大消息条数）

Redis 后端不可用（未安装 redis 包或连接失败）时自动降级为内存后端，
并记录告警日志，保证服务可用性优先。

消息结构（纯 JSON 可序列化）：
- human 消息：``{"role": "human", "content": 问题, "ts": ISO时间}``
- assistant 消息：``{"role": "assistant", "content": 回答文本, "payload": 完整响应体, "ts": ISO时间}``
"""

from __future__ import annotations

import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger("smart_qa_web")

_HISTORY_MAX_MESSAGES = int(os.getenv("HISTORY_MAX_MESSAGES", "6"))
_HISTORY_MAX_REPLAY = int(os.getenv("HISTORY_MAX_REPLAY", "50"))
_HISTORY_TTL_SECONDS = int(os.getenv("HISTORY_TTL_SECONDS", "7200"))
# 侧边栏会话标题 = 首条问题前 N 字
_TITLE_MAX_LENGTH = 30


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（含时区）。"""
    return datetime.now(timezone.utc).isoformat()


def _new_session_record() -> dict[str, Any]:
    """创建空的会话记录（title / 归属用户 / 时间戳 / 消息列表）。"""
    now = _now_iso()
    return {
        "title": "",
        "user_id": "",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def _payload_to_messages(payload: list[dict[str, Any]]) -> list[BaseMessage]:
    """把 role/content 消息结构还原为消息对象（未知 role 按 assistant 处理）。"""
    messages: list[BaseMessage] = []
    for item in payload:
        if item.get("role") == "human":
            messages.append(HumanMessage(content=str(item.get("content") or "")))
        else:
            messages.append(AIMessage(content=str(item.get("content") or "")))
    return messages


class HistoryStore(ABC):
    """会话历史存储抽象：实现自行保证线程安全。"""

    @abstractmethod
    def get(self, session_id: str) -> list[BaseMessage]:
        """返回该会话最近若干轮消息（LLM 上下文，不存在时返回空列表）。"""

    @abstractmethod
    def append(
        self,
        session_id: str,
        question: str,
        answer_payload: dict[str, Any],
        user_id: str = "",
    ) -> None:
        """把一轮问答（含完整回放 payload）追加到会话历史，并裁剪到上限；
        首次写入时把会话归属到 user_id（空串表示匿名）。"""

    @abstractmethod
    def list_sessions(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """返回会话元数据（含 session_id/user_id/title/时间/消息数），按最近更新倒序；
        传入 user_id 时仅返回该用户的会话，不传返回全部。"""

    @abstractmethod
    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """返回该会话的完整消息列表（供前端回放，不存在时返回空列表）。"""

    @abstractmethod
    def delete_session(self, session_id: str) -> None:
        """删除整个会话的历史。"""

    @abstractmethod
    def touch(self, session_id: str) -> None:
        """刷新会话的活跃度（Redis 后端续期 TTL，内存后端刷新 LRU 位置）；
        会话不存在时不做任何事。"""


class MemoryHistoryStore(HistoryStore):
    """进程内 OrderedDict + LRU 实现（迁移自 app.py 原逻辑）。"""

    def __init__(self, max_sessions: int = 100) -> None:
        self._sessions: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_sessions = max_sessions

    def _touch_session(self, session_id: str) -> dict[str, Any]:
        """取会话记录并刷新 LRU 位置（不存在时创建空记录）。"""
        record = self._sessions.get(session_id)
        if record is None:
            record = _new_session_record()
            self._sessions[session_id] = record
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return record

    def get(self, session_id: str) -> list[BaseMessage]:
        with self._lock:
            record = self._touch_session(session_id)
            return _payload_to_messages(record["messages"][-_HISTORY_MAX_MESSAGES:])

    def append(
        self,
        session_id: str,
        question: str,
        answer_payload: dict[str, Any],
        user_id: str = "",
    ) -> None:
        if not answer_payload.get("answer"):
            return
        now = _now_iso()
        with self._lock:
            record = self._touch_session(session_id)
            # 会话归属首次写入即固定，避免同一会话被后续请求改挂到他人名下
            if user_id and not record.get("user_id"):
                record["user_id"] = user_id
            messages = record["messages"]
            messages.append({"role": "human", "content": question, "ts": now})
            messages.append(
                {
                    "role": "assistant",
                    "content": answer_payload.get("answer") or "",
                    "payload": answer_payload,
                    "ts": now,
                }
            )
            messages[:] = messages[-_HISTORY_MAX_REPLAY:]
            if not record["title"] and question:
                record["title"] = question[:_TITLE_MAX_LENGTH]
            record["updated_at"] = now

    def list_sessions(self, user_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sessions = []
            for session_id, record in reversed(self._sessions.items()):
                if user_id is not None and record.get("user_id", "") != user_id:
                    continue
                sessions.append(
                    {
                        "session_id": session_id,
                        "user_id": record.get("user_id", ""),
                        "title": record["title"] or "(空会话)",
                        "created_at": record["created_at"],
                        "updated_at": record["updated_at"],
                        "message_count": len(record["messages"]),
                    }
                )
            return sessions

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return []
            self._sessions.move_to_end(session_id)
            return list(record["messages"])

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def touch(self, session_id: str) -> None:
        """刷新 LRU 位置：让最近激活/回放的会话优先保留，避免被逐出。"""
        with self._lock:
            if session_id in self._sessions:
                self._sessions.move_to_end(session_id)


class RedisHistoryStore(HistoryStore):
    """Redis 实现：每个会话一个 JSON 字符串键，带 TTL 自动过期。"""

    _KEY_PREFIX = "smart_qa:history:"

    def __init__(self, redis_url: str) -> None:
        import redis  # 延迟导入：仅 Redis 后端需要该依赖

        # protocol=2（RESP2）：redis-py 5+ 默认 RESP3，握手时会发送 HELLO 命令，
        # Redis < 6.0（如 Windows 上常见的 3.x/5.x 发行版）不支持 HELLO 会报
        # "unknown command 'HELLO'"；本项目只用 get/set/scan/delete 等基础命令，
        # RESP2 完全够用，且新旧服务器均兼容。
        self._client = redis.Redis.from_url(
            redis_url, decode_responses=True, socket_connect_timeout=3, protocol=2
        )
        self._client.ping()  # 立即验证连通性，失败由工厂捕获并降级

    def _key(self, session_id: str) -> str:
        return f"{self._KEY_PREFIX}{session_id}"

    def _load(self, session_id: str) -> dict[str, Any] | None:
        """读取并解析会话记录；不存在或解析失败时返回 None。"""
        raw = self._client.get(self._key(session_id))
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("会话 %s 历史解析失败，按空历史处理", session_id)
            return None
        if not isinstance(record, dict) or not isinstance(record.get("messages"), list):
            logger.warning("会话 %s 历史结构异常，按空历史处理", session_id)
            return None
        return record

    def _save(self, session_id: str, record: dict[str, Any]) -> None:
        self._client.set(
            self._key(session_id),
            json.dumps(record, ensure_ascii=False),
            ex=_HISTORY_TTL_SECONDS,
        )

    def get(self, session_id: str) -> list[BaseMessage]:
        record = self._load(session_id)
        if record is None:
            return []
        return _payload_to_messages(record["messages"][-_HISTORY_MAX_MESSAGES:])

    def append(
        self,
        session_id: str,
        question: str,
        answer_payload: dict[str, Any],
        user_id: str = "",
    ) -> None:
        if not answer_payload.get("answer"):
            return
        now = _now_iso()
        record = self._load(session_id)
        if record is None:
            record = _new_session_record()
        # 会话归属首次写入即固定，避免同一会话被后续请求改挂到他人名下
        if user_id and not record.get("user_id"):
            record["user_id"] = user_id
        messages = record["messages"]
        messages.append({"role": "human", "content": question, "ts": now})
        messages.append(
            {
                "role": "assistant",
                "content": answer_payload.get("answer") or "",
                "payload": answer_payload,
                "ts": now,
            }
        )
        messages[:] = messages[-_HISTORY_MAX_REPLAY:]
        if not record["title"] and question:
            record["title"] = question[:_TITLE_MAX_LENGTH]
        record["updated_at"] = now
        self._save(session_id, record)

    def list_sessions(self, user_id: str | None = None) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for key in self._client.scan_iter(match=f"{self._KEY_PREFIX}*"):
            session_id = str(key)[len(self._KEY_PREFIX):]
            record = self._load(session_id)
            if record is None:
                continue
            if user_id is not None and record.get("user_id", "") != user_id:
                continue
            sessions.append(
                {
                    "session_id": session_id,
                    "user_id": record.get("user_id", ""),
                    "title": record.get("title") or "(空会话)",
                    "created_at": record.get("created_at") or "",
                    "updated_at": record.get("updated_at") or "",
                    "message_count": len(record["messages"]),
                }
            )
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        return sessions

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        record = self._load(session_id)
        if record is None:
            return []
        return list(record["messages"])

    def delete_session(self, session_id: str) -> None:
        self._client.delete(self._key(session_id))

    def touch(self, session_id: str) -> None:
        """续期 TTL：EXPIRE 对不存在的键返回 0 且不会创建新键，天然安全。"""
        self._client.expire(self._key(session_id), _HISTORY_TTL_SECONDS)


def create_history_store() -> HistoryStore:
    """按 HISTORY_BACKEND 环境变量创建存储；Redis 不可用时降级为内存。"""
    backend = os.getenv("HISTORY_BACKEND", "memory").strip().lower()
    if backend == "redis":
        redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        try:
            return RedisHistoryStore(redis_url)
        except Exception as exc:  # noqa: BLE001 - 降级优先于可用性
            logger.warning("Redis 历史存储不可用（%s），降级为进程内存存储", exc)
    return MemoryHistoryStore()
