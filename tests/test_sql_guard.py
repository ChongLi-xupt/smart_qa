"""sql_guard 安全护栏回归测试。

运行方式：``python tests/test_sql_guard.py``（或 pytest）。
覆盖 P1 收敛后的唯一校验入口：AST 只读校验、强制 LIMIT、敏感字段检测。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sql_guard import (
    ensure_limit,
    find_sensitive_fields,
    guard_select,
    prepare_select,
    validate_sql_schema,
)


class FakeSchemaDB:
    """假数据库管理器：只提供 validate_sql_schema 需要的两个接口。"""

    _TABLES = {
        "users": ["id", "name", "city"],
        "orders": ["id", "user_id", "amount"],
        "products": ["id", "category", "price"],
    }

    def get_tables(self):
        return [{"name": name} for name in self._TABLES]

    def get_column_names(self, table_name):
        return list(self._TABLES[table_name])

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


def test_validate_rejects_unknown_table():
    """引用不存在的表时直接拒绝。"""
    error = validate_sql_schema("SELECT id FROM ghost_table", FakeSchemaDB())
    assert error and "不存在的表" in error


def test_validate_rejects_unknown_qualified_column():
    """限定列引用编造字段时，错误信息应列出真实字段。"""
    error = validate_sql_schema(
        "SELECT users.nick_name FROM users", FakeSchemaDB()
    )
    assert error and "nick_name" in error and "实际字段" in error
    assert "name" in error  # 真实字段清单在场，便于模型修正


def test_validate_allows_join_chain_with_aliases():
    """多表 JOIN 链 + 别名的合法查询应通过。"""
    sql = (
        "SELECT u.name, SUM(o.amount) AS total "
        "FROM users u JOIN orders o ON u.id = o.user_id "
        "JOIN products p ON o.id = p.id "
        "GROUP BY u.name ORDER BY total DESC"
    )
    assert validate_sql_schema(sql, FakeSchemaDB()) is None


def test_validate_supports_cte():
    """CTE 名称不当作物理表，其内部引用的真实字段正常核对。"""
    sql = (
        "WITH big_orders AS (SELECT user_id, amount FROM orders WHERE amount > 100) "
        "SELECT COUNT(*) AS cnt FROM big_orders"
    )
    assert validate_sql_schema(sql, FakeSchemaDB()) is None
    bad = (
        "WITH big_orders AS (SELECT user_id, fee FROM orders) "
        "SELECT COUNT(*) FROM big_orders"
    )
    error = validate_sql_schema(bad, FakeSchemaDB())
    assert error and "fee" in error


def test_validate_supports_subquery_alias():
    """派生表别名不核对，子查询内部的真实字段仍被核对。"""
    sql = "SELECT t.user_id FROM (SELECT user_id FROM orders) t"
    assert validate_sql_schema(sql, FakeSchemaDB()) is None
    bad = "SELECT t.user_id FROM (SELECT buyer_id FROM orders) t"
    error = validate_sql_schema(bad, FakeSchemaDB())
    assert error and "buyer_id" in error


def test_validate_flags_ambiguous_bare_column():
    """多表同名的裸列引用应在执行前被检出歧义。"""
    sql = "SELECT id FROM users JOIN orders ON users.id = orders.user_id"
    error = validate_sql_schema(sql, FakeSchemaDB())
    assert error and "多个表" in error


def test_validate_allows_order_by_output_alias():
    """ORDER BY/GROUP BY 引用 AS 输出别名不应被误判为编造字段。"""
    sql = (
        "SELECT city, COUNT(*) AS user_count FROM users "
        "GROUP BY city ORDER BY user_count DESC LIMIT 1"
    )
    assert validate_sql_schema(sql, FakeSchemaDB()) is None


def test_validate_supports_union():
    """UNION 两侧的表与字段都应被核对。"""
    ok_sql = "SELECT id FROM users UNION ALL SELECT id FROM products"
    assert validate_sql_schema(ok_sql, FakeSchemaDB()) is None
    bad_sql = "SELECT id FROM users UNION ALL SELECT serial FROM products"
    error = validate_sql_schema(bad_sql, FakeSchemaDB())
    assert error and "serial" in error


def test_validate_skips_unparseable_sql():
    """无法解析的 SQL 不核对，留给 EXPLAIN 给出更准确的错误。"""
    assert validate_sql_schema("SELECT id FROM (", FakeSchemaDB()) is None


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
