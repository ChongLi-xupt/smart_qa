"""RAG few-shot 检索与业务术语表（P3-16，对标 Vanna 的训练语料思路）。

零外部依赖实现：
- 语料库 ``rag/examples.jsonl``：每行一个 JSON 对象，字段为
  ``question``（历史问题）、``sql``（人工核对过的正确 SQL）、
  ``notes``（可选：答案要点/易错点说明）。提问时按字符二元组
  相似度检索 Top-K 注入提示词，作为 few-shot 示例。
- 术语表 ``rag/glossary.md``：每行 ``术语: 定义``（# 开头为注释），
  如"活跃用户: 近 30 天有登录记录的用户"，随系统提示词注入。

语料/术语文件不存在或为空时全部功能自动关闭，不影响现有行为。
反馈闭环（P3-18）：点赞的回答沉淀到 feedback.jsonl，经人工核对后
可手工整理进 examples.jsonl，形成"提问 → 反馈 → 语料 → 更准"的循环。
"""

from __future__ import annotations

import json
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

logger = logging.getLogger("smart_qa.rag")

_DEFAULT_RAG_DIR = Path(__file__).resolve().parent / "rag"

# 相似度低于该阈值的示例不注入，避免无关示例误导模型
_MIN_SIMILARITY = 0.18


# ---------------------------------------------------------------------- #
# 加载
# ---------------------------------------------------------------------- #


def load_examples(path: Path | None = None) -> list[dict[str, str]]:
    """读取 JSONL 语料库；文件缺失或行损坏时跳过并记日志。"""
    file_path = path or _DEFAULT_RAG_DIR / "examples.jsonl"
    if not file_path.is_file():
        return []
    examples: list[dict[str, str]] = []
    for line_number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("语料 %s 第 %d 行 JSON 解析失败，已跳过", file_path.name, line_number)
            continue
        if item.get("question") and item.get("sql"):
            examples.append(item)
    return examples


def load_glossary(path: Path | None = None) -> dict[str, str]:
    """读取术语表（每行 ``术语: 定义``，# 开头为注释）。"""
    file_path = path or _DEFAULT_RAG_DIR / "glossary.md"
    if not file_path.is_file():
        return {}
    glossary: dict[str, str] = {}
    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line or line.startswith("#") or ":" not in line and "：" not in line:
            continue
        separator = ":" if ":" in line else "："
        term, definition = line.split(separator, 1)
        term, definition = term.strip(), definition.strip()
        if term and definition:
            glossary[term] = definition
    return glossary


# ---------------------------------------------------------------------- #
# 检索与格式化
# ---------------------------------------------------------------------- #


def _char_bigrams(text: str) -> set[str]:
    """字符二元组集合：无需分词即可度量中文/混合文本的重叠程度。"""
    compact = "".join((text or "").lower().split())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def similarity(query: str, candidate: str) -> float:
    """综合相似度：二元组 Jaccard 与序列比率的均值，取值 0~1。"""
    if not query or not candidate:
        return 0.0
    bigrams_a, bigrams_b = _char_bigrams(query), _char_bigrams(candidate)
    if not bigrams_a or not bigrams_b:
        return 0.0
    jaccard = len(bigrams_a & bigrams_b) / len(bigrams_a | bigrams_b)
    ratio = SequenceMatcher(None, query.lower(), candidate.lower()).ratio()
    return (jaccard + ratio) / 2


def retrieve_examples(
    question: str,
    examples: list[dict[str, str]] | None = None,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """检索与问题最相似的语料，返回带 score 的列表（低于阈值已过滤）。"""
    pool = load_examples() if examples is None else examples
    scored = [
        {**item, "score": similarity(question, str(item.get("question", "")))}
        for item in pool
    ]
    hits = [item for item in scored if item["score"] >= _MIN_SIMILARITY]
    hits.sort(key=lambda item: item["score"], reverse=True)
    return hits[:top_k]


def format_few_shot(hits: list[dict[str, Any]]) -> str:
    """把检索命中的示例格式化为提示词文本；无命中返回空串。"""
    if not hits:
        return ""
    lines = [
        "以下是与本问题相似的历史问答示例（SQL 已经过人工核对），可参考其写法，"
        "但仍必须以真实 schema 为准，不得照抄不存在的表或字段："
    ]
    for index, item in enumerate(hits, 1):
        lines.append(f"示例{index} 问题：{item['question']}")
        lines.append(f"参考 SQL：\n```sql\n{item['sql']}\n```")
        notes = str(item.get("notes") or "").strip()
        if notes:
            lines.append(f"要点：{notes}")
    return "\n".join(lines)


def format_glossary(glossary: dict[str, str] | None = None) -> str:
    """把术语表格式化为提示词文本；空表返回空串。"""
    terms = load_glossary() if glossary is None else glossary
    if not terms:
        return ""
    lines = ["业务术语表（遇到下列术语时按定义理解，不要按字面猜测）："]
    lines.extend(f"- {term}：{definition}" for term, definition in terms.items())
    return "\n".join(lines)
