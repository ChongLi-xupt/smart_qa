"""列中文别名映射测试（配置解析 / 解析优先级 / 热加载 / 降级）。

运行方式：``python tests/test_column_aliases.py``（或 pytest）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from column_aliases import merge_column_aliases, resolve_column_aliases  # noqa: E402

_CONFIG_TEMPLATE = """\
# 测试用配置（# 开头为注释）
exact:
  total_sales: 总销售额
  order_count: 订单总数
  "COUNT(*)": 记录数
tokens:
  user: 用户
  users: 用户
  count: 数
  sales: 销售额
  month: 月份
  monthly: 每月
  new: 新增
"""


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_exact_alias():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "aliases.yaml"
        _write(cfg, _CONFIG_TEMPLATE)
        aliases = resolve_column_aliases(["total_sales", "order_count"], cfg)
    assert aliases == {"total_sales": "总销售额", "order_count": "订单总数"}


def test_exact_case_insensitive():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "aliases.yaml"
        _write(cfg, _CONFIG_TEMPLATE)
        aliases = resolve_column_aliases(["TOTAL_SALES"], cfg)
    assert aliases == {"TOTAL_SALES": "总销售额"}


def test_quoted_key():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "aliases.yaml"
        _write(cfg, _CONFIG_TEMPLATE)
        aliases = resolve_column_aliases(["COUNT(*)"], cfg)
    assert aliases == {"COUNT(*)": "记录数"}


def test_token_synthesis():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "aliases.yaml"
        _write(cfg, _CONFIG_TEMPLATE)
        aliases = resolve_column_aliases(["user_count", "monthly_sales", "new_users"], cfg)
    assert aliases == {
        "user_count": "用户数",
        "monthly_sales": "每月销售额",
        "new_users": "新增用户",
    }


def test_exact_priority_over_tokens():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "aliases.yaml"
        _write(cfg, _CONFIG_TEMPLATE)
        # order_count 按 tokens 会合成「订单?」（order 未收录），exact 优先生效
        aliases = resolve_column_aliases(["order_count"], cfg)
    assert aliases == {"order_count": "订单总数"}


def test_fallback_original_name():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "aliases.yaml"
        _write(cfg, _CONFIG_TEMPLATE)
        # 未知词无法合成、中文列名不参与合成：均不产生别名条目
        aliases = resolve_column_aliases(["foobar", "user_xyz", "销售额"], cfg)
    assert aliases == {}


def test_missing_config_degrades():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        aliases = resolve_column_aliases(["total_sales"], Path(tmp) / "nope.yaml")
    assert aliases == {}


def test_default_config_covers_common_fields():
    # 项目自带配置应覆盖界面上常见英文列名（截图场景回归）
    aliases = resolve_column_aliases(["total_sales", "order_count", "user_count"])
    assert aliases["total_sales"] == "总销售额"
    assert aliases["order_count"] == "订单总数"
    assert aliases["user_count"] == "用户数"


def test_config_hot_reload():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "aliases.yaml"
        _write(cfg, "exact:\n  a: 甲\n")
        assert resolve_column_aliases(["a"], cfg) == {"a": "甲"}
        _write(cfg, "exact:\n  a: 乙\n")
        # 推进 mtime 触发缓存失效（同一时刻写入时 mtime 可能相同）
        os.utime(cfg, (os.path.getatime(cfg), os.path.getmtime(cfg) + 5))
        assert resolve_column_aliases(["a"], cfg) == {"a": "乙"}


# ------------------------------ Agent 建议合并 ------------------------------ #


def test_merge_agent_priority_over_config():
    # Agent 结合问题意图的命名优先于配置（如问销售额时 pay_amount→销售额）
    merged = merge_column_aliases(
        ["total_sales", "pay_amount"],
        {"pay_amount": "销售额"},
    )
    assert merged["pay_amount"] == "销售额"
    # 未上报的列仍由配置兜底
    assert merged["total_sales"] == "总销售额"


def test_merge_config_fallback_without_suggested():
    merged = merge_column_aliases(["total_sales"], None)
    assert merged == {"total_sales": "总销售额"}


def test_merge_invalid_suggested_filtered():
    # 超长/空值/非字符串/不在结果列中的建议一律过滤，回退配置
    merged = merge_column_aliases(
        ["total_sales", "order_count", "weird_col"],
        {
            "total_sales": "这是一个超过十二字的别名啊啊啊啊",
            "order_count": "   ",
            "weird_col": 123,
            "not_in_result": "杂项",
        },
    )
    assert merged["total_sales"] == "总销售额"
    assert merged["order_count"] == "订单总数"
    assert "weird_col" not in merged
    assert "not_in_result" not in merged


def test_merge_suggested_case_insensitive_key():
    merged = merge_column_aliases(["total_sales"], {"TOTAL_SALES": "销售额"})
    assert merged["total_sales"] == "销售额"


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
