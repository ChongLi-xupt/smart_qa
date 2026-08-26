"""查询结果列的中文别名映射（配置化，零新增依赖）。

配置文件 ``config/column_aliases.yaml`` 采用 YAML 子集格式（见文件内注释）：

- ``exact``：精确别名，列名 → 中文别名（大小写不敏感命中）；
- ``tokens``：分词词典，exact 未命中时把 snake_case 列名按 ``_`` 分词、
  逐词翻译拼接合成别名（需全部词命中才合成，避免半翻译误导）。

解析优先级：精确别名 → 分词合成 → 回退原始列名（不产生别名条目）。
配置文件缺失或个别行损坏时自动降级（空映射 / 跳过该行），不影响现有
行为；配置修改后按文件 mtime 自动热加载，无需重启服务。

结果组装层（``app._build_ask_payload``）调用 ``merge_column_aliases`` 合并
两层别名：Agent 经 ``report_column_aliases`` 工具上报的建议优先（结合问题
意图，最贴合场景），配置别名为确定性兜底；合并结果注入
``data.column_aliases``，前端图表图例 / 坐标轴 / 系列名与数据表格列头
优先渲染别名，无别名时回退原始列名（兼容不含该字段的历史会话 payload）。
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("smart_qa.column_aliases")

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "column_aliases.yaml"

# 路径 -> (mtime, 解析结果) 缓存，避免每次请求重复解析配置；
# mtime 变化时自动失效，实现配置热加载。
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, tuple[dict[str, str], dict[str, str]]]] = {}


def _unquote(text: str) -> str:
    """去掉成对的包裹引号（支持 ``"COUNT(*)": 记录数`` 这类带引号的键/值）。"""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _parse_config_text(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """解析 YAML 子集：顶层 ``小节:`` 行 + 缩进的 ``键: 值`` 行。

    返回 ``(exact, tokens)`` 两个映射；无法识别的行记日志后跳过。
    """
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():  # 顶层小节，如 "exact:"
            if stripped.endswith(":"):
                current = sections.setdefault(stripped[:-1].strip(), {})
            else:
                logger.warning("column_aliases 第 %d 行非「小节:」格式，已跳过: %s", lineno, stripped)
                current = None
            continue
        if current is None:
            continue
        if ":" not in stripped and "：" not in stripped:
            logger.warning("column_aliases 第 %d 行非「键: 值」格式，已跳过: %s", lineno, stripped)
            continue
        separator = ":" if ":" in stripped else "："
        key, value = stripped.split(separator, 1)
        key, value = _unquote(key.strip()), _unquote(value.strip())
        if key and value:
            current[key] = value
    return sections.get("exact", {}), sections.get("tokens", {})


def _load_config(path: Path | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """读取配置（mtime 缓存）；文件缺失/不可读时返回空映射，整体降级。"""
    file_path = path or _DEFAULT_CONFIG_PATH
    key = str(file_path)
    try:
        mtime = file_path.stat().st_mtime
    except OSError:
        return {}, {}
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        parsed = _parse_config_text(file_path.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("column_aliases 配置读取失败: %s", file_path)
        return {}, {}
    with _cache_lock:
        _cache[key] = (mtime, parsed)
    return parsed


def _synthesize_alias(column: str, tokens: dict[str, str]) -> str | None:
    """snake_case 列名分词逐词翻译合成别名；任一词未命中返回 None。"""
    lowered = column.strip().lower()
    if not lowered or not lowered.isascii():
        return None  # 中文/混合列名本身已可读，不做合成
    parts = [part for part in re.split(r"[_\s-]+", lowered) if part]
    if not parts:
        return None
    translated = [tokens.get(part) for part in parts]
    if any(item is None for item in translated):
        return None
    alias = "".join(item for item in translated if item)
    return alias or None


def resolve_column_aliases(
    columns: list[str], config_path: Path | None = None
) -> dict[str, str]:
    """为列名列表生成中文别名表（仅包含命中别名的列）。

    优先级：exact 精确命中（大小写不敏感）→ tokens 分词合成 → 不产生条目
    （前端回退原始列名）。返回 ``{原始列名: 中文别名}``。
    """
    exact, tokens = _load_config(config_path)
    exact_lower = {key.lower(): value for key, value in exact.items()}
    aliases: dict[str, str] = {}
    for column in columns:
        name = str(column)
        alias = exact.get(name) or exact_lower.get(name.lower())
        if alias is None and tokens:
            alias = _synthesize_alias(name, tokens)
        if alias and alias != name:
            aliases[name] = alias
    return aliases


# Agent 上报别名的单值长度上限：过长会挤占图表图例/表头布局
_MAX_ALIAS_LENGTH = 12


def _clean_alias(value: Any) -> str | None:
    """校验单个别名值：非空字符串、长度受限、不含换行；不合法返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _MAX_ALIAS_LENGTH or "\n" in text:
        return None
    return text


def merge_column_aliases(
    columns: list[str],
    suggested: dict[str, str] | None = None,
    config_path: Path | None = None,
) -> dict[str, str]:
    """合并列别名：Agent 建议优先 → 配置别名兜底 → 不产生条目（回退原始列名）。

    Agent（report_column_aliases 工具）结合问题意图给出的展示名最贴合场景
    （如问销售额时 pay_amount 合计命名为"销售额"），但可能遗漏或越界，
    故以配置文件为确定性兜底；两者都未命中的列不产生条目。
    """
    merged = resolve_column_aliases(columns, config_path)
    if not suggested:
        return merged
    suggested_lower = {str(key).lower(): value for key, value in suggested.items()}
    for column in columns:
        name = str(column)
        raw = suggested.get(name)
        if raw is None:
            raw = suggested_lower.get(name.lower())
        alias = _clean_alias(raw)
        if alias and alias != name:
            merged[name] = alias
    return merged
