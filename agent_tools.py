"""供 LangChain Agent 调用的 MySQL 数据库工具。

P1 起，全部 SQL 安全校验（AST 检查、敏感字段、schema 真实性核对、
强制 LIMIT）收敛在 ``sql_guard.py`` 与 ``MySQLDatabase.execute_readonly``
/ ``check_readonly``；本模块的工具只做参数封装与结果格式化，
不再自行实现任何校验逻辑。
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.tools import BaseTool
from pydantic import ConfigDict, Field, create_model
from sqlalchemy.exc import SQLAlchemyError

from database import MySQLDatabase


class TablesListTool(BaseTool):
    """列出 MySQL 数据库中的普通表及其备注。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "sql_db_list_tables"
    description: str = (
        "列出 MySQL 数据库中的所有普通表名及表备注。"
        "当需要了解数据库中有哪些表或选择后续查询目标时调用。"
        "该工具不需要输入参数。"
    )
    db_manager: MySQLDatabase = Field(exclude=True, repr=False)

    def _run(self) -> str:
        """读取全部表信息，并返回适合大模型阅读的文本。"""
        try:
            tables_info = self.db_manager.get_tables()
        except SQLAlchemyError as exc:
            return f"列出数据库表时发生数据库错误: {exc}"

        if not tables_info:
            return "数据库中没有普通表。"

        # 使用列表收集后一次 join，避免表较多时反复创建越来越长的字符串。
        output_lines = [f"数据库中共有 {len(tables_info)} 个表："]
        for index, table_info in enumerate(tables_info, start=1):
            table_name = str(table_info.get("name") or "(未命名表)")
            raw_comment = table_info.get("comment")
            table_comment = (
                raw_comment.strip()
                if isinstance(raw_comment, str) and raw_comment.strip()
                else "(暂无描述)"
            )
            output_lines.extend(
                (
                    "",
                    f"{index}. 表名: {table_name}",
                    f"   描述: {table_comment}",
                )
            )

        return "\n".join(output_lines)
    """获取一个或多个 MySQL 数据库表的完整 schema 信息。"""
class TablesSchemaTool(BaseTool):


    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str = "sql_db_schema"
    description: str = (
        "获取一个或多个 MySQL 数据库表的 schema 结构，包括表备注、字段名称、"
        "字段类型、是否允许为空、默认值、主键、外键、索引和唯一约束。"
        "调用时必须通过 table_names 传入表名列表；可先调用 sql_db_list_tables "
        "获取准确的表名。"
    )
    db_manager: MySQLDatabase = Field(exclude=True, repr=False)

    def __init__(self, db_manager: MySQLDatabase) -> None:
        schema_args = create_model(
            "TableSchemaToolArgs",
            table_names=(
                list[str],
                Field(
                    ...,
                    min_length=1,
                    description="需要获取 schema 结构的数据库表名列表",
                ),
            ),
        )
        super().__init__(db_manager=db_manager, args_schema=schema_args)

    def _run(self, table_names: list[str]) -> str:
        """同步获取一个或多个表的 schema 信息。"""
        return self._get_schemas_text(table_names)

    async def _arun(self, table_names: list[str]) -> str:
        """在线程池中获取多表 schema，避免阻塞 asyncio 事件循环。"""
        return await asyncio.to_thread(self._get_schemas_text, table_names)

    def _get_schemas_text(self, table_names: list[str]) -> str:
        """清理、去重表名，并依次生成每张表的 schema 文本。"""
        normalized_names: list[str] = []
        seen_names: set[str] = set()
        for table_name in table_names:
            if not isinstance(table_name, str):
                continue
            normalized_name = table_name.strip()
            if normalized_name and normalized_name not in seen_names:
                normalized_names.append(normalized_name)
                seen_names.add(normalized_name)

        if not normalized_names:
            return "表名列表不能为空。"

        schema_sections = [
            self._get_schema_text(table_name) for table_name in normalized_names
        ]
        separator = "\n\n" + "=" * 80 + "\n\n"
        return separator.join(schema_sections)

    def _get_schema_text(self, table_name: str) -> str:
        """执行 schema 查询并格式化同步、异步调用共用的返回内容。"""
        normalized_name = table_name.strip()
        if not normalized_name:
            return "表名不能为空。"

        try:
            schema_info = self.db_manager.get_schema(table_name=normalized_name)
        except SQLAlchemyError as exc:
            return f"获取表 {normalized_name!r} 的 schema 时发生数据库错误: {exc}"

        output_lines = [
            f"表名: {schema_info.get('name') or normalized_name}",
            f"Schema: {schema_info.get('schema') or '-'}",
            f"表备注: {schema_info.get('comment') or '(暂无描述)'}",
            "",
            "字段信息:",
        ]

        columns = schema_info.get("columns") or []
        if not columns:
            output_lines.append("  无字段")
        for index, column in enumerate(columns, start=1):
            nullable = column.get("nullable")
            nullable_text = (
                "是" if nullable is True else "否" if nullable is False else "-"
            )
            default = column.get("default")
            autoincrement = column.get("autoincrement")
            if isinstance(autoincrement, bool):
                autoincrement_text = "是" if autoincrement else "否"
            else:
                autoincrement_text = (
                    str(autoincrement) if autoincrement is not None else "-"
                )

            output_lines.extend(
                (
                    f"  {index}. 字段名: {column.get('name') or '-'}",
                    f"     类型: {column.get('type') or '-'}",
                    f"     允许为空: {nullable_text}",
                    f"     默认值: {default if default is not None else '-'}",
                    f"     自动递增: {autoincrement_text}",
                    f"     字段备注: {column.get('comment') or '(暂无描述)'}",
                )
            )
            if column.get("computed") is not None:
                output_lines.append(f"     计算字段: {column['computed']}")

        primary_key = schema_info.get("primary_key") or {}
        primary_columns = ", ".join(primary_key.get("constrained_columns") or [])
        output_lines.extend(
            (
                "",
                "主键信息:",
                f"  名称: {primary_key.get('name') or '-'}",
                f"  字段: {primary_columns or '-'}",
                "",
                "外键信息:",
            )
        )

        foreign_keys = schema_info.get("foreign_keys") or []
        if not foreign_keys:
            output_lines.append("  无外键")
        for index, foreign_key in enumerate(foreign_keys, start=1):
            local_columns = ", ".join(
                foreign_key.get("constrained_columns") or []
            )
            target_columns = ", ".join(foreign_key.get("referred_columns") or [])
            target_schema = foreign_key.get("referred_schema")
            target_table = foreign_key.get("referred_table") or "-"
            target_name = (
                f"{target_schema}.{target_table}" if target_schema else target_table
            )
            output_lines.extend(
                (
                    f"  {index}. 名称: {foreign_key.get('name') or '-'}",
                    f"     本表字段: {local_columns or '-'}",
                    f"     引用位置: {target_name}({target_columns or '-'})",
                )
            )

        output_lines.extend(("", "索引信息:"))
        indexes = schema_info.get("indexes") or []
        if not indexes:
            output_lines.append("  无索引")
        for index, index_info in enumerate(indexes, start=1):
            index_columns = ", ".join(index_info.get("column_names") or [])
            output_lines.append(
                f"  {index}. {index_info.get('name') or '-'} | "
                f"字段: {index_columns or '-'} | "
                f"唯一: {'是' if index_info.get('unique') else '否'}"
            )

        output_lines.extend(("", "唯一约束:"))
        unique_constraints = schema_info.get("unique_constraints") or []
        if not unique_constraints:
            output_lines.append("  无唯一约束")
        for index, constraint in enumerate(unique_constraints, start=1):
            constraint_columns = ", ".join(constraint.get("column_names") or [])
            output_lines.append(
                f"  {index}. {constraint.get('name') or '-'} | "
                f"字段: {constraint_columns or '-'}"
            )

        return "\n".join(output_lines)

class SQLExecuteTool(BaseTool):
    """执行只读的 MySQL SELECT 查询并返回有限数量的结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "sql_db_execute"
    description: str = (
        "在 MySQL 数据库上执行一条只读 SELECT 查询并返回 JSON 格式结果。"
        "不允许 INSERT、UPDATE、DELETE、DDL、多语句查询或写文件操作。"
        "调用前应先通过 sql_db_list_tables 和 sql_db_schema 了解表结构。"
    )
    db_manager: MySQLDatabase = Field(exclude=True, repr=False)

    def __init__(self, db_manager: MySQLDatabase) -> None:
        query_args = create_model(
            "SQLExecuteToolArgs",
            query=(
                str,
                Field(..., min_length=1, description="需要执行的单条 SELECT 查询语句"),
            ),
            max_rows=(
                int,
                Field(
                    default=200,
                    ge=1,
                    le=1000,
                    description="最多返回的结果行数，默认 200,最大 1000",
                ),
            ),
        )
        super().__init__(db_manager=db_manager, args_schema=query_args)

    def _run(self, query: str, max_rows: int = 200) -> str:
        """同步执行只读查询。"""
        return self._execute_select(query=query, max_rows=max_rows)

    async def _arun(self, query: str, max_rows: int = 200) -> str:
        """在线程池中执行查询，避免阻塞 asyncio 事件循环。"""
        return await asyncio.to_thread(
            self._execute_select,
            query,
            max_rows,
        )

    def _execute_select(self, query: str, max_rows: int) -> str:
        """委托数据库层唯一的只读执行入口（含全部安全校验）。"""
        result = self.db_manager.execute_readonly(query, max_rows=max_rows)
        if not result.get("success"):
            return (
                f"{result.get('message') or '查询未能执行。'}\n"
                "请如实向用户说明查询未能成功及原因，"
                "严禁编造查询结果或谎称查询成功。"
            )
        response = {
            "columns": result["columns"],
            "rows": result["rows"],
            "row_count": result["row_count"],
            "truncated": result["truncated"],
            "note": result.get("note"),
        }
        return json.dumps(response, ensure_ascii=False, default=str)

class ColumnAliasesTool(BaseTool):
    """上报查询结果列的中文展示别名（仅用于前端渲染，不影响 SQL）。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "report_column_aliases"
    description: str = (
        "为最近一次成功查询的结果列上报中文展示别名，前端图表图例/坐标轴/"
        "表格列头将优先展示这些别名。aliases 为「结果列名 → 简短中文名」映射，"
        "需覆盖全部结果列；中文名不超过 12 字，应结合用户问题意图、字段备注与"
        "聚合语义命名（如 total_sales→总销售额；问销售额时用 pay_amount 合计"
        "可命名为销售额）。"
    )

    def __init__(self) -> None:
        alias_args = create_model(
            "ColumnAliasesToolArgs",
            aliases=(
                dict[str, str],
                Field(..., description="结果列名到中文展示名的映射"),
            ),
        )
        super().__init__(args_schema=alias_args)

    def _run(self, aliases: dict[str, str]) -> str:
        """记录列别名建议（实际消费在结果组装层从工具输入中提取）。"""
        return f"已记录 {len(aliases or {})} 个列的中文展示名，将用于前端图表与表格渲染。"


class SQLCheckTool(BaseTool):
    """校验 MySQL SELECT 查询的语法及数据库对象引用。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "sql_db_checker"
    description: str = (
        "使用 MySQL EXPLAIN 校验一条 SELECT 查询的语法，以及查询引用的表和字段"
        "是否可解析；不会返回该查询的业务数据。只接受单条 SELECT 查询。"
    )
    db_manager: MySQLDatabase = Field(exclude=True, repr=False)

    def __init__(self, db_manager: MySQLDatabase) -> None:
        checker_args = create_model(
            "SQLCheckToolArgs",
            query=(
                str,
                Field(..., min_length=1, description="需要校验的单条 SELECT 查询语句"),
            ),
        )
        super().__init__(db_manager=db_manager, args_schema=checker_args)

    def _run(self, query: str) -> str:
        """同步校验 SELECT 查询。"""
        return self._check_query(query)

    async def _arun(self, query: str) -> str:
        """在线程池中校验查询，避免阻塞 asyncio 事件循环。"""
        return await asyncio.to_thread(self._check_query, query)

    def _check_query(self, query: str) -> str:
        """委托数据库层唯一的只读校验入口，以 JSON 返回校验结果。"""
        result = self.db_manager.check_readonly(query)
        return json.dumps(result, ensure_ascii=False, default=str)

if __name__ == "__main__":
    import os

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if not os.getenv("DB_PASSWORD"):
        raise SystemExit(
            "请先复制 .env.example 为 .env 并填写只读账号的数据库连接信息。"
        )

    database = MySQLDatabase.from_connection_info(
        host=os.environ["DB_HOST"],
        username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        port=int(os.getenv("DB_PORT", "3306")),
        ssl_disabled=os.getenv("DB_SSL_DISABLED", "true").lower() == "true",
    )

    tool = TablesListTool(db_manager=database)
    result = tool.invoke({})
    print(result)

    schema_tool = TablesSchemaTool(db_manager=database)
    print(
        "\n"
        + schema_tool.invoke(
            {"table_names": ["user", "video_analysis"]}
        )
    )

    executor = SQLExecuteTool(db_manager=database)
    result = executor.invoke({
        "query": "SELECT id,username FROM user ORDER BY id DESC",
        "max_rows": 100,
    })
    print(result)

    checker = SQLCheckTool(db_manager=database)

    result_checker = checker.invoke({
        "query": "SELECT id username FROM user WHERE id > 10"
    })
    print(result_checker)

    result = executor.invoke({
        "query": "SELECT id, username FROM user WHERE id > 10",
        "max_rows": 100,
    })

    print(result)
