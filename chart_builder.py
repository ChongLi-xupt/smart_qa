"""根据查询结果数据与用户问题，自动推断最适合的图表类型。

推断规则（按优先级）：
1. 没有数值列 → 不绘图（纯文本/明细类结果）；
2. 存在日期/时间列作为维度 → 折线图（趋势）；
3. 问题包含"占比/比例/百分比/构成/份额"等关键词且类别数适中 → 饼图；
4. 其余"类别 + 数值"组合 → 柱状图。

返回结构::

    {
        "chartable": True,          # 数据是否可绘图
        "chart_type": "bar",        # bar / line / pie / None
        "reason": "...",            # 推断理由（便于前端提示）
        "column_kinds": [...],      # 各列类型（number/date/category），前端直接用，
                                     # 无需再重复推断（P3-18）
    }
"""

from __future__ import annotations

import re
from typing import Any

# 匹配常见日期形态：2026、2026-08、2026-08-01、2026/8/1、2026年8月、含时间部分等
_DATE_PATTERN = re.compile(
    r"^\d{4}([-/年]\d{1,2}([-/月]\d{1,2}日?)?)?"
    r"([ T]?\d{1,2}:\d{2}(:\d{2})?)?$"
)

# 命中这些关键词时，倾向用饼图展示占比关系
_PIE_KEYWORDS = ("占比", "比例", "百分比", "构成", "份额", "各占", "比重")

# 饼图适合的类别数量范围
_PIE_MIN_ROWS = 2
_PIE_MAX_ROWS = 10


def _is_number(value: Any) -> bool:
    """判断单个值是否为数值（含可转数值的字符串）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return False
        try:
            float(text)
            return True
        except ValueError:
            return False
    return False


def _is_date_like(value: Any) -> bool:
    """判断单个值是否形如日期/时间。"""
    return isinstance(value, str) and bool(_DATE_PATTERN.match(value.strip()))


def classify_columns(columns: list[str], rows: list[list[Any]]) -> list[str]:
    """对每一列做类型推断，返回与 columns 等长的类型列表。

    类型取值：``number`` / ``date`` / ``category``。
    以非空值为准判断；全为空的列按 category 处理。
    """
    kinds: list[str] = []
    for index in range(len(columns)):
        values = [
            row[index]
            for row in rows
            if index < len(row) and row[index] is not None and str(row[index]).strip() != ""
        ]
        if not values:
            kinds.append("category")
        elif all(_is_number(value) for value in values):
            kinds.append("number")
        elif all(_is_date_like(value) for value in values):
            kinds.append("date")
        else:
            kinds.append("category")
    return kinds


def recommend_chart(
    question: str,
    columns: list[str],
    rows: list[list[Any]],
) -> dict[str, Any]:
    """根据问题与查询结果推断图表类型。

    Args:
        question: 用户的自然语言问题，用于识别"占比"类意图。
        columns:  查询结果的列名列表。
        rows:     查询结果的行数据。

    Returns:
        含 chartable / chart_type / reason 的字典。
    """
    if not columns or not rows:
        return {
            "chartable": False,
            "chart_type": None,
            "reason": "没有可用的查询结果数据",
            "column_kinds": [],
        }

    kinds = classify_columns(columns, rows)
    number_count = kinds.count("number")
    if number_count == 0:
        return {
            "chartable": False,
            "chart_type": None,
            "reason": "结果中没有数值列，不适合绘图",
            "column_kinds": kinds,
        }

    has_date = "date" in kinds
    has_category = "category" in kinds

    # 趋势类：日期维度 + 数值 → 折线图
    if has_date:
        return {
            "chartable": True,
            "chart_type": "line",
            "reason": "结果含日期维度，适合展示趋势",
            "column_kinds": kinds,
        }

    # 单个数值（如"共 128 个用户"）无需绘图
    if len(rows) == 1 and number_count == 1 and not has_category:
        return {
            "chartable": False,
            "chart_type": None,
            "reason": "结果为单个数值，无需绘图",
            "column_kinds": kinds,
        }

    # 占比类：问题关键词命中且类别数量适中 → 饼图
    if (
        has_category
        and _PIE_MIN_ROWS <= len(rows) <= _PIE_MAX_ROWS
        and any(keyword in (question or "") for keyword in _PIE_KEYWORDS)
    ):
        return {
            "chartable": True,
            "chart_type": "pie",
            "reason": "问题关注占比/构成，适合饼图",
            "column_kinds": kinds,
        }

    return {
        "chartable": True,
        "chart_type": "bar",
        "reason": "类别与数值对比，适合柱状图",
        "column_kinds": kinds,
    }
