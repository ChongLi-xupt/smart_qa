"""黄金问题评测脚本（P3-17）：对固定问题集做回归，防止提示词/工具改动导致准确率回退。

用法::

    python eval_golden.py                       # 使用默认黄金集
    python eval_golden.py --threshold 0.8       # 通过率低于阈值时退出码为 1

前置条件：项目根目录 .env 已配置 DB_* 与 LLM_API_KEY（需要真实的数据库与
大模型）。配置缺失时打印说明并以退出码 0 结束（CI 中安全跳过）。

每条黄金集的校验规则：
    - expect_rejected=true：必须命中敏感拦截（rejected=True），不进入 Agent；
    - 其余条目：提问不得抛异常，且提取出的 SQL 须包含全部 expect_tables
      （大小写不敏感）。条目可带 note 说明考察点。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

_GOLDEN_FILE = Path(__file__).resolve().parent / "tests" / "golden" / "golden_questions.jsonl"


def _load_golden(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(json.loads(line))
    return entries


def _env_ready() -> bool:
    api_key = os.getenv("LLM_API_KEY", "")
    return bool(os.getenv("DB_HOST") and os.getenv("DB_PASSWORD") and api_key
                and not api_key.startswith("sk-xxxxxx"))


def _check_entry(qa, entry: dict) -> tuple[bool, str]:
    """执行单条黄金问题并校验，返回 (是否通过, 说明)。"""
    question = entry["question"]
    try:
        result = qa.ask(question)
    except Exception as exc:  # noqa: BLE001 - 评测需捕获一切失败
        return False, f"提问抛出异常: {type(exc).__name__}"

    if entry.get("expect_rejected"):
        if result.get("rejected"):
            return True, "敏感问题已正确拦截"
        return False, "敏感问题未被拦截"

    sql = result.get("sql") or ""
    if not sql:
        return False, "未生成 SQL（可能被拒绝或执行失败）"
    lowered = sql.lower()
    missing = [t for t in entry.get("expect_tables", []) if t.lower() not in lowered]
    if missing:
        return False, f"SQL 未引用期望的表: {', '.join(missing)}"
    return True, "SQL 通过表引用校验"


def main() -> int:
    parser = argparse.ArgumentParser(description="黄金问题评测")
    parser.add_argument("--golden", type=Path, default=_GOLDEN_FILE)
    parser.add_argument("--threshold", type=float, default=0.8)
    args = parser.parse_args()

    entries = _load_golden(args.golden)
    print(f"黄金集: {args.golden.name}，共 {len(entries)} 条")

    if not _env_ready():
        print("未检测到可用的 DB_*/LLM_API_KEY 配置，跳过评测（退出码 0）。")
        return 0

    from smart_qa import SmartQA, _config_from_env

    db_config, llm_config = _config_from_env()
    qa = SmartQA(db_config, llm_config)

    passed = 0
    try:
        for index, entry in enumerate(entries, 1):
            ok, detail = _check_entry(qa, entry)
            passed += 1 if ok else 0
            mark = "PASS" if ok else "FAIL"
            print(f"[{mark}] {index}. {entry['question']} —— {detail}")
    finally:
        qa.close()

    rate = passed / len(entries) if entries else 1.0
    print(f"\n通过率: {passed}/{len(entries)} = {rate:.0%}（阈值 {args.threshold:.0%}）")
    return 0 if rate >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
