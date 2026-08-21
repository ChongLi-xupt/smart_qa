"""黄金问题集回归（P3-17）。

两组测试：
    1. 文件格式校验 —— 恒跑，不依赖任何外部服务，防止人工编辑 JSONL 引入语法错误；
    2. 敏感条目拦截回归 —— 敏感拦截发生在 LLM/数据库之前，经 FastAPI TestClient
       即可验证，同样无需真实环境。

完整准确率评测（需要真实 DB + LLM）由 ``python eval_golden.py`` 执行，
环境缺失时自动以退出码 0 跳过。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_GOLDEN_FILE = Path(__file__).resolve().parents[1] / "tests" / "golden" / "golden_questions.jsonl"


def _load_entries() -> list[dict]:
    entries = []
    for line in _GOLDEN_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(json.loads(line))
    return entries


def test_golden_file_not_empty():
    assert _load_entries(), "黄金集不能为空"


def test_golden_entry_schema():
    for entry in _load_entries():
        question = entry.get("question")
        assert isinstance(question, str) and question.strip(), f"question 必须是非空字符串: {entry}"
        has_reject = entry.get("expect_rejected") is True
        tables = entry.get("expect_tables")
        has_tables = isinstance(tables, list) and bool(tables) and all(
            isinstance(t, str) and t.strip() for t in tables
        )
        assert has_reject or has_tables, f"条目须带 expect_rejected 或 expect_tables: {entry}"


def test_golden_contains_sensitive_samples():
    # 黄金集应持续覆盖敏感拦截场景，防止回归盲区
    assert any(e.get("expect_rejected") for e in _load_entries())


def test_golden_sensitive_entries_rejected():
    os.environ.setdefault("SESSION_SECRET_KEY", "unit-test-secret-key-" + "x" * 32)
    from fastapi.testclient import TestClient  # noqa: E402

    import app  # noqa: E402  需在环境变量就绪后导入

    client = TestClient(app.app)
    sensitive = [e for e in _load_entries() if e.get("expect_rejected")]
    for entry in sensitive:
        response = client.post("/api/v1/ask", json={"question": entry["question"]})
        assert response.status_code == 200, entry["question"]
        payload = response.json()
        assert payload["ok"] is True, entry["question"]
        assert "敏感" in payload["answer"] or "隐私" in payload["answer"], entry["question"]


if __name__ == "__main__":
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
