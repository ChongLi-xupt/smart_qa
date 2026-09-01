"""使用 SQLAlchemy 读取 MySQL 数据库元数据，并提供唯一的只读查询入口。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from schema_cache import SchemaCache
from sql_guard import contains_sensitive_field, prepare_select


def _schema_cache_ttl() -> int:
    """读取 schema 缓存 TTL（秒）：SCHEMA_CACHE_TTL，默认 300，0 表示禁用。"""
    try:
        return max(0, int(os.getenv("SCHEMA_CACHE_TTL", "300")))
    except ValueError:
        return 300


class MySQLDatabase:
    """MySQL 数据库连接及 schema 元数据读取器。

    推荐通过 :meth:`from_connection_info` 创建实例，这样用户名和密码中的
    特殊字符会被安全处理。也可以直接传入 SQLAlchemy 数据库 URL，例如::

        mysql+pymysql://user:password@127.0.0.1:3306/database_name
    """

    def __init__(
        self,
        database_url: str | URL | None = None,
        *,
        host: str | None = None,
        username: str | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        port: int = 3306,
        charset: str = "utf8mb4",
        connect_timeout: int = 10,
        ssl_disabled: bool = False,
        engine_options: Mapping[str, Any] | None = None,
    ) -> None:
        """创建数据库读取器。

        可以传入 ``database_url``，也可以直接传入 ``host``、``username``
        （或 ``user``）、``password`` 和 ``database``。
        """
        if username is not None and user is not None and username != user:
            raise ValueError("username 和 user 不能设置为不同的值")
        resolved_username = username if username is not None else user

        connection_fields = (host, resolved_username, password, database)
        if database_url is not None and any(value is not None for value in connection_fields):
            raise ValueError("database_url 不能与 host/username/password/database 同时传入")

        if database_url is None:
            required_values = {
                "host": host,
                "username": resolved_username,
                "database": database,
            }
            empty_names = [
                name for name, value in required_values.items() if value is None or value == ""
            ]
            if empty_names:
                raise ValueError(f"以下连接参数不能为空: {', '.join(empty_names)}")
            if not 1 <= port <= 65535:
                raise ValueError("port 必须在 1 到 65535 之间")

            database_url = URL.create(
                drivername="mysql+pymysql",
                username=resolved_username,
                password=password or "",
                host=host,
                port=port,
                database=database,
                query={"charset": charset},
            )
        elif isinstance(database_url, str) and not database_url.strip():
            raise ValueError("database_url 不能为空")

        connect_args: dict[str, Any] = {"connect_timeout": connect_timeout}
        if ssl_disabled:
            connect_args["ssl_disabled"] = True
        options: dict[str, Any] = {
            "pool_pre_ping": True,
            "connect_args": connect_args,
        }
        if engine_options:
            supplied_options = dict(engine_options)
            supplied_connect_args = supplied_options.pop("connect_args", None)
            options.update(supplied_options)
            if supplied_connect_args:
                options["connect_args"].update(supplied_connect_args)

        if ssl_disabled:
            # PyMySQL 只要收到 ssl/ssl_* 配置就可能创建 TLS 上下文；明确禁用时
            # 清除这些配置，避免再次进入 SSL handshake。
            for option_name in tuple(options["connect_args"]):
                if option_name == "ssl" or (
                    option_name.startswith("ssl_") and option_name != "ssl_disabled"
                ):
                    options["connect_args"].pop(option_name)
            options["connect_args"]["ssl_disabled"] = True

        self._engine: Engine = create_engine(database_url, **options)

        # schema 元数据 TTL 缓存（P2/D2）：表列表/字段/schema 低频变化，
        # 缓存后工具调用与 SQL 防幻觉核对不再每次查 information_schema。
        ttl = _schema_cache_ttl()
        self._schema_cache: SchemaCache | None = (
            SchemaCache(self, ttl=ttl) if ttl > 0 else None
        )

    @classmethod
    def from_connection_info(
        cls,
        host: str,
        username: str,
        password: str,
        database: str,
        *,
        port: int = 3306,
        charset: str = "utf8mb4",
        connect_timeout: int = 10,
        ssl_disabled: bool = False,
        engine_options: Mapping[str, Any] | None = None,
    ) -> "MySQLDatabase":
        """根据地址、用户名、密码和数据库名创建读取器。"""
        required_values = {
            "host": host,
            "username": username,
            "database": database,
        }
        empty_names = [name for name, value in required_values.items() if not value]
        if empty_names:
            raise ValueError(f"以下连接参数不能为空: {', '.join(empty_names)}")
        if not 1 <= port <= 65535:
            raise ValueError("port 必须在 1 到 65535 之间")

        return cls(
            host=host,
            username=username,
            password=password,
            port=port,
            database=database,
            charset=charset,
            connect_timeout=connect_timeout,
            ssl_disabled=ssl_disabled,
            engine_options=engine_options,
        )

    @property
    def engine(self) -> Engine:
        """返回底层 SQLAlchemy Engine。"""
        return self._engine

    def connect(self) -> "MySQLDatabase":
        """建立一次真实连接以验证数据库可访问，并返回当前实例。"""
        with self._engine.connect():
            pass
        return self

    def close(self) -> None:
        """释放连接池中的全部数据库连接，并清空 schema 缓存。"""
        if self._schema_cache is not None:
            self._schema_cache.invalidate()
        self._engine.dispose()

    def __enter__(self) -> "MySQLDatabase":
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_tables(self, schema: str | None = None) -> list[dict[str, Any]]:
        """返回所有普通表的名称及备注信息（TTL 内命中缓存）。"""
        if schema is None and self._schema_cache is not None:
            return self._schema_cache.get_tables()
        return self._fetch_tables(schema)

    def _fetch_tables(self, schema: str | None = None) -> list[dict[str, Any]]:
        """直查 information_schema 的原始实现（供 SchemaCache 刷新调用）。

        SchemaCache 必须调用本方法而非 get_tables，否则缓存分发会
        反向重入缓存自身，造成同线程锁死锁。
        """
        inspector = inspect(self._engine)
        tables: list[dict[str, Any]] = []
        for table_name in inspector.get_table_names(schema=schema):
            comment_info = inspector.get_table_comment(table_name, schema=schema)
            tables.append(
                {
                    "name": table_name,
                    "comment": comment_info.get("text"),
                }
            )
        return tables

    def get_column_names(
        self,
        table_name: str | None = None,
        schema: str | None = None,
    ) -> dict[str, list[str]] | list[str]:
        """返回指定表或数据库中每个表的所有字段名称（TTL 内命中缓存）。"""
        if schema is None and self._schema_cache is not None:
            return self._schema_cache.get_column_names(table_name)
        return self._fetch_column_names(table_name, schema)

    def _fetch_column_names(
        self,
        table_name: str | None = None,
        schema: str | None = None,
    ) -> dict[str, list[str]] | list[str]:
        """直查字段的原始实现（供 SchemaCache 刷新/回退调用）。"""
        inspector = inspect(self._engine)
        if table_name is not None:
            self._ensure_table_exists(inspector, table_name, schema)
            return [
                column["name"]
                for column in inspector.get_columns(table_name, schema=schema)
            ]

        return {
            name: [
                column["name"]
                for column in inspector.get_columns(name, schema=schema)
            ]
            for name in inspector.get_table_names(schema=schema)
        }

    def get_schema(
        self,
        table_name: str | None = None,
        schema: str | None = None,
    ) -> dict[str, Any]:
        """返回指定表或数据库全部表的 JSON 友好 schema 结构（TTL 内命中缓存）。"""
        if schema is None and self._schema_cache is not None:
            return self._schema_cache.get_schema(table_name)
        return self._fetch_schema(table_name, schema)

    def _fetch_schema(
        self,
        table_name: str | None = None,
        schema: str | None = None,
    ) -> dict[str, Any]:
        """直查 schema 的原始实现（供 SchemaCache 回退调用）。"""
        inspector = inspect(self._engine)
        if table_name is not None:
            self._ensure_table_exists(inspector, table_name, schema)
            return self._read_table_schema(inspector, table_name, schema)

        return {
            name: self._read_table_schema(inspector, name, schema)
            for name in inspector.get_table_names(schema=schema)
        }

    def get_relationships(self) -> list[dict[str, Any]]:
        """返回全库外键关系列表（默认 schema，TTL 内命中缓存）。"""
        if self._schema_cache is not None:
            return self._schema_cache.get_relationships()
        return self._fetch_relationships()

    def _fetch_relationships(self) -> list[dict[str, Any]]:
        """直查外键关系的原始实现（供 SchemaCache 刷新/回退调用）。"""
        inspector = inspect(self._engine)
        relationships: list[dict[str, Any]] = []
        for table_name in inspector.get_table_names():
            for foreign_key in inspector.get_foreign_keys(table_name):
                referred_table = foreign_key.get("referred_table")
                if not referred_table:
                    continue
                relationships.append(
                    {
                        "source_table": table_name,
                        "constrained_columns": list(
                            foreign_key.get("constrained_columns") or []
                        ),
                        "referred_table": referred_table,
                        "referred_columns": list(
                            foreign_key.get("referred_columns") or []
                        ),
                    }
                )
        return relationships

    def get_join_suggestions(self, table_name: str) -> list[str]:
        """基于外键关系为指定表生成可直接使用的 JOIN 子句。

        双向匹配外键（本表引用他表、他表引用本表都会命中），
        直接给出关联条件，避免大模型在多表查询中猜测关联字段。
        数据库异常或列信息缺失时返回空列表，不影响 schema 输出主流程。
        """
        try:
            relationships = self.get_relationships()
        except SQLAlchemyError:
            return []

        lowered = (table_name or "").strip().lower()
        suggestions: list[str] = []
        for relationship in relationships:
            source_table = str(relationship.get("source_table") or "")
            referred_table = str(relationship.get("referred_table") or "")
            source_columns = list(relationship.get("constrained_columns") or [])
            referred_columns = list(relationship.get("referred_columns") or [])
            if (
                not source_table
                or not referred_table
                or not source_columns
                or len(source_columns) != len(referred_columns)
            ):
                continue
            if source_table.lower() == lowered:
                other_table = referred_table
            elif referred_table.lower() == lowered:
                other_table = source_table
            else:
                continue
            conditions = " AND ".join(
                f"{source_table}.{source_column} = {referred_table}.{referred_column}"
                for source_column, referred_column in zip(source_columns, referred_columns)
            )
            suggestions.append(f"JOIN {other_table} ON {conditions}")
        return suggestions

    def execute_readonly(
        self,
        query: str,
        max_rows: int = 200,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """唯一的只读查询执行入口，最多返回 ``max_rows`` 行结果。

        全部安全校验收敛在 :func:`sql_guard.prepare_select`：
        AST 只读校验 → 敏感字段拦截 → schema 真实性核对 → 强制 LIMIT；
        执行时另外叠加会话级 ``max_execution_time`` 超时（MySQL 5.7.8+，
        仅对 SELECT 生效），防止慢查询长期占用连接。
        """
        safe_sql, error = prepare_select(query, self)
        if error is not None:
            return {
                "success": False,
                "message": error,
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
            }

        timeout = int(timeout_ms or os.getenv("QUERY_TIMEOUT_MS", "15000"))
        try:
            with self._engine.connect() as connection:
                if timeout > 0:
                    connection.execute(
                        text("SET SESSION max_execution_time = :timeout"),
                        {"timeout": timeout},
                    )
                result = connection.execute(text(safe_sql))
                column_names = list(result.keys())
                fetched_rows = result.fetchmany(max_rows + 1)
        except SQLAlchemyError as exc:
            return {
                "success": False,
                "message": f"执行查询失败: {exc}",
                "columns": [],
                "rows": [],
                "row_count": 0,
                "truncated": False,
            }

        truncated = len(fetched_rows) > max_rows
        rows = fetched_rows[:max_rows]

        # 结果层兜底：SELECT * 等写法可能带出敏感列，返回前自动过滤
        sensitive_columns = {
            name for name in column_names if contains_sensitive_field(str(name))
        }
        note = None
        if sensitive_columns:
            keep_indexes = [
                i for i, name in enumerate(column_names) if name not in sensitive_columns
            ]
            column_names = [column_names[i] for i in keep_indexes]
            rows = [[row[i] for i in keep_indexes] for row in rows]
            note = (
                "结果中检测到敏感隐私字段（"
                + ", ".join(sorted(map(str, sensitive_columns)))
                + "），已自动过滤，不予返回。"
            )

        return {
            "success": True,
            "message": (
                f"查询成功，仅返回前 {max_rows} 行。"
                if truncated
                else "查询成功。"
            ),
            "columns": column_names,
            "rows": [
                [self._to_json_value(value) for value in row] for row in rows
            ],
            "row_count": len(rows),
            "truncated": truncated,
            "note": note,
        }

    def check_readonly(self, query: str) -> dict[str, Any]:
        """唯一的只读查询校验入口：EXPLAIN 验证语法与对象引用。

        与 :meth:`execute_readonly` 共用同一套 prepare_select 校验，
        保证"校验通过"与"可执行"的标准完全一致；不返回业务数据。
        """
        safe_sql, error = prepare_select(query, self)
        if error is not None:
            return {"valid": False, "message": error, "explain": None}

        try:
            with self._engine.connect() as connection:
                result = connection.execute(text(f"EXPLAIN {safe_sql}"))
                explain_columns = list(result.keys())
                explain_rows = result.fetchall()
        except SQLAlchemyError as exc:
            return {
                "valid": False,
                "message": f"SQL 校验失败: {exc}",
                "explain": None,
            }

        return {
            "valid": True,
            "message": "SQL 语法以及引用的表和字段校验通过。",
            "explain": {
                "columns": explain_columns,
                "rows": [
                    [self._to_json_value(value) for value in row]
                    for row in explain_rows
                ],
            },
        }

    def print_database_info(self, schema: str | None = None) -> None:
        """按照表名遍历并打印数据库中每张表的完整结构信息。"""
        all_tables = self.get_schema(schema=schema)
        if not all_tables:
            print("数据库中没有普通表。")
            return

        for table_number, (table_name, table_info) in enumerate(
            all_tables.items(), start=1
        ):
            print("=" * 80)
            print(f"表 {table_number}: {table_name}")
            print(f"Schema: {table_info.get('schema') or '-'}")
            print(f"表备注: {table_info.get('comment') or '-'}")

            print("\n字段信息:")
            columns = table_info.get("columns", [])
            if not columns:
                print("  无字段")
            for column_number, column in enumerate(columns, start=1):
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
                print(f"  字段 {column_number}: {column['name']}")
                print(f"    类型: {column['type']}")
                print(f"    允许为空: {nullable_text}")
                print(f"    默认值: {default if default is not None else '-'}")
                print(f"    自动递增: {autoincrement_text}")
                print(f"    字段备注: {column.get('comment') or '-'}")
                if column.get("computed") is not None:
                    print(f"    计算字段: {column['computed']}")

            primary_key = table_info.get("primary_key") or {}
            primary_key_columns = primary_key.get("constrained_columns") or []
            print("\n主键信息:")
            print(f"  主键名称: {primary_key.get('name') or '-'}")
            print(f"  主键字段: {', '.join(primary_key_columns) or '-'}")

            print("\n外键信息:")
            foreign_keys = table_info.get("foreign_keys") or []
            if not foreign_keys:
                print("  无外键")
            for foreign_key_number, foreign_key in enumerate(foreign_keys, start=1):
                local_columns = ", ".join(
                    foreign_key.get("constrained_columns") or []
                )
                target_columns = ", ".join(
                    foreign_key.get("referred_columns") or []
                )
                target_schema = foreign_key.get("referred_schema")
                target_table = foreign_key.get("referred_table") or "-"
                target_name = (
                    f"{target_schema}.{target_table}" if target_schema else target_table
                )
                print(
                    f"  外键 {foreign_key_number}: "
                    f"{foreign_key.get('name') or '-'}"
                )
                print(f"    本表字段: {local_columns or '-'}")
                print(f"    引用位置: {target_name}({target_columns or '-'})")

            print("\n索引信息:")
            indexes = table_info.get("indexes") or []
            if not indexes:
                print("  无索引")
            for index_number, index in enumerate(indexes, start=1):
                index_columns = ", ".join(index.get("column_names") or [])
                print(
                    f"  索引 {index_number}: {index.get('name') or '-'} | "
                    f"字段: {index_columns or '-'} | "
                    f"唯一: {'是' if index.get('unique') else '否'}"
                )

            print("\n唯一约束:")
            unique_constraints = table_info.get("unique_constraints") or []
            if not unique_constraints:
                print("  无唯一约束")
            for constraint_number, constraint in enumerate(
                unique_constraints, start=1
            ):
                constraint_columns = ", ".join(
                    constraint.get("column_names") or []
                )
                print(
                    f"  约束 {constraint_number}: "
                    f"{constraint.get('name') or '-'} | "
                    f"字段: {constraint_columns or '-'}"
                )

            print()

    @staticmethod
    def _ensure_table_exists(inspector: Any, table_name: str, schema: str | None) -> None:
        if not inspector.has_table(table_name, schema=schema):
            qualified_name = f"{schema}.{table_name}" if schema else table_name
            raise NoSuchTableError(qualified_name)

    @classmethod
    def _read_table_schema(
        cls,
        inspector: Any,
        table_name: str,
        schema: str | None,
    ) -> dict[str, Any]:
        columns = []
        for column in inspector.get_columns(table_name, schema=schema):
            columns.append(
                {
                    "name": column["name"],
                    "type": str(column["type"]),
                    "nullable": column.get("nullable"),
                    "default": cls._to_json_value(column.get("default")),
                    "autoincrement": column.get("autoincrement"),
                    "comment": column.get("comment"),
                    "computed": cls._to_json_value(column.get("computed")),
                }
            )

        return {
            "name": table_name,
            "schema": schema or inspector.default_schema_name,
            "comment": inspector.get_table_comment(table_name, schema=schema).get("text"),
            "columns": columns,
            "primary_key": cls._to_json_value(
                inspector.get_pk_constraint(table_name, schema=schema)
            ),
            "foreign_keys": cls._to_json_value(
                inspector.get_foreign_keys(table_name, schema=schema)
            ),
            "indexes": cls._to_json_value(
                inspector.get_indexes(table_name, schema=schema)
            ),
            "unique_constraints": cls._to_json_value(
                inspector.get_unique_constraints(table_name, schema=schema)
            ),
        }

    @classmethod
    def _to_json_value(cls, value: Any) -> Any:
        """把 SQLAlchemy 方言对象转换成适合 JSON 序列化的值。"""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {str(key): cls._to_json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._to_json_value(item) for item in value]
        return str(value)


if __name__ == "__main__":
    with MySQLDatabase(
        host="mysql.example.com",
        user="user",
        password="password",
        database="example_db",
        port=3306,
        ssl_disabled=True,

    ) as database:
        database.print_database_info()
