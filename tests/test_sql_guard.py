"""sql_guard 安全护栏回归测试。

运行方式：``python tests/test_sql_guard.py``（或 pytest）。
覆盖 P1 收敛后的唯一校验入口：AST 只读校验、强制 LIMIT、敏感字段检测。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sql_guard import (
    _extract_table_sequence,
    ensure_limit,
    find_sensitive_fields,
    guard_select,
    prepare_select,
)

# ---------------------------------------------------------------------- #
# guard_select：AST 级只读校验
# ---------------------------------------------------------------------- #


def test_guard_allows_normal_select():
    ok, message = guard_select("SELECT id, username FROM user WHERE id = 1")
    assert ok, message


def test_guard_allows_union():
    ok, message = guard_select("SELECT id FROM user UNION ALL SELECT id FROM admin_log")
    assert ok, message


def test_guard_allows_string_literal_with_keywords():
    # 字符串字面量中的危险词不应触发拒绝（旧正则实现的误杀场景）
    ok, message = guard_select("SELECT name FROM note WHERE content = 'a;b delete drop'")
    assert ok, message


def test_guard_rejects_dml_and_ddl():
    dangerous = [
        "INSERT INTO user (id) VALUES (1)",
        "UPDATE user SET username = 'x'",
        "DELETE FROM user",
        "DROP TABLE user",
        "CREATE TABLE evil (id INT)",
        "ALTER TABLE user ADD COLUMN x INT",
    ]
    for sql in dangerous:
        ok, message = guard_select(sql)
        assert not ok, f"应当拒绝: {sql}"


def test_guard_rejects_multi_statement():
    ok, _ = guard_select("SELECT 1; DROP TABLE user")
    assert not ok


def test_guard_rejects_select_into_outfile():
    ok, _ = guard_select("SELECT * INTO OUTFILE '/tmp/x' FROM user")
    assert not ok


def test_guard_rejects_locking():
    assert not guard_select("SELECT * FROM user FOR UPDATE")[0]
    assert not guard_select("SELECT * FROM user LOCK IN SHARE MODE")[0]


def test_guard_rejects_empty():
    assert not guard_select("   ")[0]


# ---------------------------------------------------------------------- #
# ensure_limit：强制 LIMIT 上限
# ---------------------------------------------------------------------- #


def test_ensure_limit_appends_when_missing():
    result = ensure_limit("SELECT id FROM user", hard_cap=1000)
    assert "LIMIT 1000" in result.upper()


def test_ensure_limit_caps_excessive_limit():
    result = ensure_limit("SELECT id FROM user LIMIT 5000", hard_cap=1000)
    assert "5000" not in result
    assert "LIMIT 1000" in result.upper()


def test_ensure_limit_keeps_smaller_limit():
    result = ensure_limit("SELECT id FROM user LIMIT 10", hard_cap=1000)
    assert "LIMIT 10" in result.upper()


# ---------------------------------------------------------------------- #
# find_sensitive_fields：敏感字段检测
# ---------------------------------------------------------------------- #


def test_sensitive_detects_plain_and_quoted():
    assert find_sensitive_fields("SELECT user_password FROM user")
    assert find_sensitive_fields("SELECT `phone_number` FROM user")
    assert find_sensitive_fields("SELECT 手机号 FROM user")


def test_sensitive_ignores_literal_content():
    # 值里出现 password 一词不构成敏感字段引用
    assert not find_sensitive_fields("SELECT note FROM t WHERE note = 'my password'")


def test_sensitive_no_false_positive_on_hotel():
    assert not find_sensitive_fields("SELECT hotel_name FROM hotel")


# ---------------------------------------------------------------------- #
# prepare_select：唯一准备入口（不连库时跳过 schema 核对）
# ---------------------------------------------------------------------- #


def test_extract_table_sequence_multi_join_chain():
    """多表 JOIN 链：第一个 ON 条件之后继续 JOIN 的表不能丢失。"""
    from_clause = (
        " orders o\n"
        "JOIN users u ON o.user_id = u.id\n"
        "JOIN products p ON o.product_id = p.id\n"
    )
    refs = _extract_table_sequence(from_clause)
    assert [table for table, _alias in refs] == ["orders", "users", "products"]
    assert refs[2] == ("products", "p")  # ON 条件内的标识符不再污染别名


def test_extract_table_sequence_comma_joined_tables():
    """逗号连接的多表也能被完整提取。"""
    from_clause = " users u, products p "
    refs = _extract_table_sequence(from_clause)
    assert refs == [("users", "u"), ("products", "p")]


def test_extract_table_sequence_on_clause_then_join():
    """ON 条件中出现的字段名（如 group/order 前缀）不会截断后续 JOIN。"""
    from_clause = (
        " orders o JOIN users u ON o.user_id = u.id "
        "LEFT JOIN products p ON p.id = o.product_id "
    )
    refs = _extract_table_sequence(from_clause)
    assert [table for table, _alias in refs] == ["orders", "users", "products"]


def test_extract_table_sequence_using_clause():
    """USING 连接条件内的字段名不参与表名提取。"""
    from_clause = " orders o JOIN users u USING (user_id) "
    refs = _extract_table_sequence(from_clause)
    assert [table for table, _alias in refs] == ["orders", "users"]


def test_prepare_rejects_dangerous_sql():
    safe_sql, error = prepare_select("DELETE FROM user")
    assert safe_sql is None and error


def test_prepare_rejects_sensitive_field():
    safe_sql, error = prepare_select("SELECT password FROM user")
    assert safe_sql is None and "敏感" in (error or "")


def test_prepare_appends_limit():
    safe_sql, error = prepare_select("SELECT id FROM user", db_manager=None)
    assert error is None
    assert "LIMIT" in (safe_sql or "").upper()


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
