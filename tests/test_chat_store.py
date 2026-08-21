"""chat_store 会话历史存储单元测试（内存后端 + Redis 降级路径）。

运行方式：``python tests/test_chat_store.py``（或 pytest）。
覆盖：完整消息持久化、会话列表、LLM 上下文裁剪、回放条数裁剪、
删除会话、Redis 不可用时自动降级。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat_store  # noqa: E402
from chat_store import (  # noqa: E402
    MemoryHistoryStore,
    create_history_store,
)

_PAYLOAD = {
    "ok": True,
    "question": "问题",
    "answer": "回答",
    "sql": None,
    "error": None,
    "data": {"columns": [], "rows": [], "row_count": 0, "truncated": False},
    "chart": {},
    "steps": [],
}


def _make_store() -> MemoryHistoryStore:
    return MemoryHistoryStore(max_sessions=10)


def test_append_and_get_messages_roundtrip():
    store = _make_store()
    store.append("s1", "一共有多少用户？", _PAYLOAD)
    messages = store.get_messages("s1")
    assert len(messages) == 2
    assert messages[0] == {
        "role": "human",
        "content": "一共有多少用户？",
        "ts": messages[0]["ts"],
    }
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "回答"
    assert messages[1]["payload"] == _PAYLOAD
    assert messages[0]["ts"] == messages[1]["ts"]


def test_empty_answer_not_stored():
    store = _make_store()
    store.append("s1", "问题", {**_PAYLOAD, "answer": ""})
    assert store.get_messages("s1") == []
    assert store.list_sessions() == []


def test_llm_context_trimmed_to_max_messages():
    store = _make_store()
    for index in range(chat_store._HISTORY_MAX_MESSAGES + 2):
        store.append("s1", f"问题{index}", {**_PAYLOAD, "answer": f"回答{index}"})
    context = store.get("s1")
    # LLM 上下文只保留最近 HISTORY_MAX_MESSAGES 条（= 最近 3 轮），最老的几轮被裁剪
    assert len(context) == chat_store._HISTORY_MAX_MESSAGES
    assert context[0].content == f"问题{chat_store._HISTORY_MAX_MESSAGES - 1}"
    assert context[-1].content == f"回答{chat_store._HISTORY_MAX_MESSAGES + 1}"
    # 完整回放不受上下文裁剪影响
    assert len(store.get_messages("s1")) == (chat_store._HISTORY_MAX_MESSAGES + 2) * 2


def test_list_sessions_metadata():
    store = _make_store()
    store.append("s1", "第一个问题", _PAYLOAD)
    store.append("s2", "第二个问题", _PAYLOAD)
    sessions = store.list_sessions()
    assert [s["session_id"] for s in sessions] == ["s2", "s1"]  # 按最近更新倒序
    assert sessions[0]["title"] == "第二个问题"
    assert sessions[0]["message_count"] == 2
    assert sessions[0]["created_at"]
    assert sessions[0]["updated_at"]


def test_title_truncated_to_30_chars():
    store = _make_store()
    store.append("s1", "很" * 100, _PAYLOAD)
    assert len(store.list_sessions()[0]["title"]) == chat_store._TITLE_MAX_LENGTH


def test_delete_session():
    store = _make_store()
    store.append("s1", "问题", _PAYLOAD)
    store.delete_session("s1")
    assert store.get_messages("s1") == []
    assert store.list_sessions() == []


def test_missing_session_returns_empty():
    store = _make_store()
    assert store.get_messages("nope") == []
    assert store.get("nope") == []


def test_lru_eviction_caps_sessions():
    store = MemoryHistoryStore(max_sessions=3)
    for index in range(5):
        store.append(f"s{index}", f"问题{index}", _PAYLOAD)
    sessions = store.list_sessions()
    assert len(sessions) == 3
    # 最老的 s0 / s1 被逐出，留下 s2 / s3 / s4
    ids = {s["session_id"] for s in sessions}
    assert ids == {"s2", "s3", "s4"}


def test_touch_refreshes_lru_order():
    store = MemoryHistoryStore(max_sessions=3)
    for index in range(3):
        store.append(f"s{index}", f"问题{index}", _PAYLOAD)
    store.touch("s0")  # s0 重新活跃
    store.append("s3", "问题3", _PAYLOAD)  # 触发逐出：应逐出最不活跃的 s1
    ids = {s["session_id"] for s in store.list_sessions()}
    assert ids == {"s0", "s2", "s3"}


def test_touch_missing_session_is_noop():
    store = _make_store()
    store.touch("nope")  # 不存在的会话：不抛错也不创建
    assert store.list_sessions() == []


def test_create_history_store_falls_back_to_memory_when_redis_unavailable():
    original_backend = os.environ.get("HISTORY_BACKEND")
    original_url = os.environ.get("REDIS_URL")
    os.environ["HISTORY_BACKEND"] = "redis"
    os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"  # 不可达端口，连接快速失败
    try:
        store = create_history_store()
        assert isinstance(store, MemoryHistoryStore)
    finally:
        if original_backend is None:
            os.environ.pop("HISTORY_BACKEND", None)
        else:
            os.environ["HISTORY_BACKEND"] = original_backend
        if original_url is None:
            os.environ.pop("REDIS_URL", None)
        else:
            os.environ["REDIS_URL"] = original_url


if __name__ == "__main__":
    import sys

    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n共 {failures} 个失败" if failures else "\n全部通过")
    sys.exit(1 if failures else 0)
