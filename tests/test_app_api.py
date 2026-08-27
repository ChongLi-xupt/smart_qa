"""FastAPI API 层冒烟测试（无需数据库与 LLM，使用 FastAPI TestClient）。

运行方式：``python tests/test_app_api.py``（或 pytest）。
覆盖 P0/P1：参数 schema 校验、API 版本化、错误脱敏、健康检查、请求追踪。
覆盖 P3：SSE 流式端点、用户反馈落盘、API Key 认证。
覆盖 P4：历史会话侧边栏（列表 / 回放 / 切换 / 删除）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SESSION_SECRET_KEY", "unit-test-secret-key-" + "x" * 32)

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402  需在环境变量就绪后导入

_client = TestClient(app.app)


def test_index_page():
    response = _client.get("/")
    assert response.status_code == 200


def test_ask_requires_json_object():
    response = _client.post("/api/v1/ask", json=["not", "a", "dict"])
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_ask_question_must_be_string():
    response = _client.post("/api/v1/ask", json={"question": 12345})
    assert response.status_code == 400


def test_ask_question_not_empty():
    response = _client.post("/api/v1/ask", json={"question": "   "})
    assert response.status_code == 400


def test_ask_question_length_limit():
    response = _client.post("/api/v1/ask", json={"question": "长" * 600})
    assert response.status_code == 400
    assert "过长" in response.json()["error"]


def test_sensitive_question_rejected_without_agent():
    # 敏感问题在 Agent 之前被拦截：不依赖任何外部服务即可返回
    response = _client.post("/api/v1/ask", json={"question": "查询所有用户的密码"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert "敏感" in payload["answer"] or "隐私" in payload["answer"]


def test_old_api_path_removed():
    response = _client.post("/api/ask", json={"question": "你好"})
    assert response.status_code == 404


def test_new_chat_endpoint():
    response = _client.post("/api/v1/new_chat")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session_id"]


def test_request_id_header_present():
    response = _client.get("/")
    assert response.headers.get("X-Request-ID")


def test_healthz_returns_json():
    # 无数据库配置时应返回 503 且结构完整（配置齐全时为 200）
    response = _client.get("/healthz")
    assert response.status_code in (200, 503)
    assert "status" in response.json()


# ------------------------------ P3：SSE 流式 ------------------------------ #


def test_ask_stream_sensitive_rejected_via_sse():
    # 敏感问题在进入 Agent 前被拦截，SSE 流应直接给出 result 事件
    response = _client.post("/api/v1/ask_stream", json={"question": "查询所有用户的密码"})
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("text/event-stream")
    text = response.text
    assert text.startswith("data: ")
    assert '"type": "result"' in text
    assert "敏感" in text or "隐私" in text


def test_ask_stream_validates_question():
    response = _client.post("/api/v1/ask_stream", json={"question": "   "})
    assert response.status_code == 400


# ------------------------------ P3：用户反馈 ------------------------------ #


def test_feedback_rejects_invalid_rating():
    response = _client.post("/api/v1/feedback", json={"rating": "good", "question": "测试"})
    assert response.status_code == 400


def test_feedback_requires_question():
    response = _client.post("/api/v1/feedback", json={"rating": "up", "question": "  "})
    assert response.status_code == 400


def test_feedback_recorded_to_file():
    import tempfile

    original = app._FEEDBACK_FILE
    with tempfile.TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "feedback.jsonl"
        app._FEEDBACK_FILE = target
        try:
            response = _client.post(
                "/api/v1/feedback",
                json={"rating": "up", "question": "一共有多少用户？", "sql": "SELECT COUNT(*) FROM user"},
            )
            assert response.status_code == 200
            assert response.json()["ok"] is True
            # 在临时目录清理前完成读取断言
            lines = target.read_text(encoding="utf-8").strip().splitlines()
        finally:
            app._FEEDBACK_FILE = original
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["rating"] == "up"
    assert record["question"] == "一共有多少用户？"


# ------------------------------ P3：API Key 认证 ------------------------------ #


def test_api_key_enforced_when_configured():
    original = app._API_KEYS
    app._API_KEYS = frozenset({"unit-test-key"})
    try:
        # 未携带密钥：/api/v1/* 一律 401
        response = _client.post("/api/v1/ask", json={"question": "你好"})
        assert response.status_code == 401
        # 错误密钥同样拒绝
        response = _client.post(
            "/api/v1/ask", json={"question": "你好"}, headers={"X-API-Key": "wrong"}
        )
        assert response.status_code == 401
        # 正确密钥放行（敏感拦截在 Agent 前，无需外部服务）
        response = _client.post(
            "/api/v1/ask",
            json={"question": "查询所有用户的密码"},
            headers={"X-API-Key": "unit-test-key"},
        )
        assert response.status_code == 200
        # 非 /api 路径（前端页面）不受密钥限制
        assert _client.get("/").status_code == 200
    finally:
        app._API_KEYS = original


# ------------------------------ P4：历史会话侧边栏 ------------------------------ #


def _seed_payload(question: str, answer: str, sql: str | None) -> dict:
    """构造一轮完整回答 payload（结构与 /ask 响应一致）。"""
    return {
        "ok": True,
        "question": question,
        "answer": answer,
        "sql": sql,
        "error": None,
        "data": {"columns": [], "rows": [], "row_count": 0, "truncated": False},
        "chart": {"chartable": False, "chart_type": None, "reason": "", "column_kinds": []},
        "steps": [],
    }


def _seed_session(session_id: str = "testsession123"):
    app._history_store.append(
        session_id, "一共有多少用户？", _seed_payload("一共有多少用户？", "共 100 个用户。", "SELECT COUNT(*) FROM user")
    )
    return session_id


def test_sessions_list_contains_seeded_session():
    session_id = _seed_session()
    try:
        response = _client.get("/api/v1/sessions")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["current_session_id"]
        ids = [s["session_id"] for s in payload["sessions"]]
        assert session_id in ids
        # 会话标题取自首条问题
        seeded = next(s for s in payload["sessions"] if s["session_id"] == session_id)
        assert seeded["title"] == "一共有多少用户？"
        assert seeded["message_count"] == 2
    finally:
        app._history_store.delete_session(session_id)


def test_session_messages_full_replay():
    session_id = _seed_session()
    try:
        response = _client.get(f"/api/v1/sessions/{session_id}/messages")
        assert response.status_code == 200
        messages = response.json()["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "human"
        assert messages[0]["content"] == "一共有多少用户？"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["payload"]["answer"] == "共 100 个用户。"
        assert messages[1]["payload"]["sql"] == "SELECT COUNT(*) FROM user"
    finally:
        app._history_store.delete_session(session_id)


def test_activate_switches_current_session():
    session_id = _seed_session()
    try:
        response = _client.post(f"/api/v1/sessions/{session_id}/activate")
        assert response.status_code == 200
        current = _client.get("/api/v1/sessions").json()["current_session_id"]
        assert current == session_id
    finally:
        app._history_store.delete_session(session_id)


def test_delete_session_removes_from_list():
    session_id = _seed_session()
    response = _client.delete(f"/api/v1/sessions/{session_id}")
    assert response.status_code == 200
    ids = [s["session_id"] for s in _client.get("/api/v1/sessions").json()["sessions"]]
    assert session_id not in ids


def test_delete_active_session_creates_new_one():
    session_id = _seed_session()
    try:
        _client.post(f"/api/v1/sessions/{session_id}/activate")
        response = _client.delete(f"/api/v1/sessions/{session_id}")
        assert response.status_code == 200
        payload = response.json()
        # 删除的是当前活动会话：响应携带新会话 ID
        assert payload["session_id"]
        assert payload["session_id"] != session_id
        current = _client.get("/api/v1/sessions").json()["current_session_id"]
        assert current == payload["session_id"]
    finally:
        app._history_store.delete_session(session_id)


def test_sessions_reject_invalid_session_id():
    response = _client.get("/api/v1/sessions/" + "x" * 100 + "/messages")
    assert response.status_code == 400


def test_sessions_filtered_by_user_id():
    # 不同用户的会话隔离展示：/sessions?user_id=xxx 仅返回该用户的会话
    app._history_store.append("user-a-session", "alice 的问题", _seed_payload("alice 的问题", "回答A", None), user_id="alice")
    app._history_store.append("user-b-session", "bob 的问题", _seed_payload("bob 的问题", "回答B", None), user_id="bob")
    try:
        response = _client.get("/api/v1/sessions", params={"user_id": "alice"})
        assert response.status_code == 200
        ids = [s["session_id"] for s in response.json()["sessions"]]
        assert "user-a-session" in ids
        assert "user-b-session" not in ids
        # 不携带 user_id 时返回全部（兼容旧客户端）
        all_ids = [s["session_id"] for s in _client.get("/api/v1/sessions").json()["sessions"]]
        assert "user-a-session" in all_ids and "user-b-session" in all_ids
    finally:
        app._history_store.delete_session("user-a-session")
        app._history_store.delete_session("user-b-session")


def test_ask_rejects_overlong_user_id():
    response = _client.post(
        "/api/v1/ask", json={"question": "你好", "user_id": "u" * 100}
    )
    assert response.status_code == 400
    assert "user_id" in response.json()["error"]


# ------------------------------ 列中文别名注入 ------------------------------ #


def test_build_ask_payload_includes_column_aliases():
    # 结果组装层应为英文列名注入中文别名，供前端图表/表头优先渲染
    result = {
        "answer": "总销售额 100 元。",
        "sql": "SELECT SUM(pay_amount) AS total_sales FROM orders",
        "data": {
            "columns": ["total_sales", "mystery_col"],
            "rows": [[100]],
            "row_count": 1,
            "truncated": False,
        },
        "intermediate_steps": [],
    }
    payload = app._build_ask_payload("销售额是多少？", result)
    aliases = payload["data"]["column_aliases"]
    assert aliases.get("total_sales") == "总销售额"
    # 无别名列不产生条目，前端回退原始列名（向后兼容）
    assert "mystery_col" not in aliases
    # 原有字段结构保持不变
    assert payload["data"]["columns"] == ["total_sales", "mystery_col"]
    assert payload["chart"]["chart_type"] == "bar"


def test_build_ask_payload_prefers_agent_aliases():
    # Agent 结合问题意图上报的别名优先于配置兜底
    result = {
        "answer": "销售额 100 元。",
        "sql": "SELECT SUM(pay_amount) AS total_sales FROM orders",
        "data": {
            "columns": ["total_sales"],
            "rows": [[100]],
            "row_count": 1,
            "truncated": False,
        },
        "suggested_column_aliases": {"total_sales": "销售额"},
        "intermediate_steps": [],
    }
    payload = app._build_ask_payload("分析一下销售额", result)
    assert payload["data"]["column_aliases"]["total_sales"] == "销售额"


def test_extract_column_aliases_from_steps():
    # 取最后一次上报；兼容 dict 与 JSON 字符串两种工具输入形态
    from smart_qa import SmartQA

    steps = [
        {"tool": "sql_db_execute", "input": {"query": "SELECT 1"}, "output": "{}"},
        {"tool": "report_column_aliases", "input": {"aliases": {"a": "甲"}}, "output": "ok"},
        {"tool": "report_column_aliases", "input": json.dumps({"aliases": {"b": "乙"}}), "output": "ok"},
        {"tool": "report_column_aliases", "input": "not-json", "output": "ok"},
    ]
    assert SmartQA._extract_column_aliases(steps) == {"b": "乙"}
    assert SmartQA._extract_column_aliases([]) == {}


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
