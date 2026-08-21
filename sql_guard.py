"""SQL 只读安全护栏：全部安全校验的唯一入口。

P1 收敛说明：原先 ``agent_tools`` 与 ``MySQLDatabase`` 中各有一套
正则校验实现，规则已经分叉。现在所有校验统一收敛到本模块：

- :func:`guard_select`          sqlglot AST 级校验（单语句、仅 SELECT、无危险结构）
- :func:`ensure_limit`          强制 LIMIT 上限，防止全量返回
- :func:`find_sensitive_fields` 敏感隐私字段检测（SQL 层）
- :func:`validate_sql_schema`   表/字段真实性核对（防大模型编造）
- :func:`prepare_select`        串联以上全部检查，输出可直接执行的安全 SQL

``MySQLDatabase.execute_readonly`` / ``check_readonly`` 与四个 Agent 工具
都必须经由 :func:`prepare_select` 进入数据库，不得自行拼接执行。
"""

from __future__ import annotations

import re

import sqlglot
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp

# ---------------------------------------------------------------------- #
# AST 级只读校验（替代原先分散的正则关键字检查）
# ---------------------------------------------------------------------- #

# 危险语句节点类型名单：按名称从 sqlglot.exp 解析，兼容不同 sqlglot 版本
# （个别节点类型在旧版本中可能不存在，缺失时自动跳过）。
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Drop", "Create", "Alter",
    "Command", "Use", "Set", "Transaction", "Commit", "Rollback",
    "Merge", "Grant",
)
_FORBIDDEN_NODE_TYPES = tuple(
    node_type
    for node_type in (getattr(exp, name, None) for name in _FORBIDDEN_NODE_NAMES)
    if node_type is not None
)

# 加锁子句：AST 表达因方言而异，用剥离字面量/注释后的正则兜底判断。
_LOCK_PATTERN = re.compile(r"(?is)\bFOR\s+UPDATE\b|\bLOCK\s+IN\s+SHARE\s+MODE\b")


def guard_select(sql: str) -> tuple[bool, str]:
    """AST 级校验：仅允许单条 SELECT（含 UNION），拒绝一切危险结构。

    返回 ``(通过与否, 错误说明)``。相比正则关键字检查，AST 校验不受
    注释、字符串字面量、大小写混排等绕过手法影响。
    """
    normalized = sql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if not normalized:
        return False, "SQL 查询语句不能为空。"

    try:
        statements = sqlglot.parse(normalized, read="mysql")
    except sqlglot.errors.ParseError as exc:
        return False, f"SQL 语法解析失败: {exc}"

    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        return False, "一次只允许提交一条 SQL 查询。"

    statement = statements[0]
    if not isinstance(statement, (exp.Select, exp.Union)):
        return False, "只允许执行 SELECT 查询。"

    for node_type in _FORBIDDEN_NODE_TYPES:
        if statement.find(node_type) is not None:
            return False, f"禁止的 SQL 操作: {node_type.__name__}"
    # SELECT ... INTO OUTFILE / DUMPFILE / 变量
    if statement.find(exp.Into) is not None:
        return False, "禁止使用 SELECT ... INTO 写文件或变量。"

    if _LOCK_PATTERN.search(_SQL_STRIP_PATTERN.sub(" ", normalized)):
        return False, "禁止在查询中使用加锁操作。"

    return True, ""


def ensure_limit(sql: str, hard_cap: int = 1000) -> str:
    """为查询强制 LIMIT 上限：缺失时追加，已有但超过 hard_cap 时收紧。

    解析失败时原样返回（调用方应先用 :func:`guard_select` 校验）。
    """
    try:
        statement = sqlglot.parse_one(sql, read="mysql")
    except sqlglot.errors.ParseError:
        return sql

    try:
        limit = statement.args.get("limit")
        if limit is None:
            statement = statement.limit(hard_cap)
        else:
            current = int(limit.expression.this)
            if current > hard_cap:
                statement = statement.limit(hard_cap)
    except (AttributeError, TypeError, ValueError):
        return sql
    return statement.sql(dialect="mysql")


# ---------------------------------------------------------------------- #
# 敏感隐私字段检测
# ---------------------------------------------------------------------- #

# 敏感隐私数据的字段名关键字。标识符会按下划线与驼峰拆分为词元后做
# 大小写不敏感的整词匹配，避免误伤 hotel 这类包含 tel 子串的普通词汇。
SENSITIVE_FIELD_KEYWORDS = frozenset(
    {
        # 凭证 / 密钥类
        "password", "passwd", "pwd", "passphrase",
        "secret", "secrets", "token", "tokens",
        "credential", "credentials", "salt",
        # 个人身份 / 联系方式类
        "idcard", "identity",
        "phone", "mobile", "tel", "telephone", "cellphone",
        "email", "mail",
    }
)

# 拆分后会被切断的复合关键字，需要在拼接形式下再做子串匹配
# （如 id_card 拆分后为 id/card，拼接回 idcard 才能命中）。
_SENSITIVE_COMPOUND_KEYWORDS = ("idcard", "idno", "apikey", "privatekey", "authkey")

# 中文字段名直接对 SQL 文本做子串匹配。
SENSITIVE_FIELD_CN_KEYWORDS = ("密码", "口令", "密钥", "私钥", "令牌", "身份证", "手机号", "邮箱")

# 去除字符串字面量与注释：既不扫描字面量内容（避免把值里的
# "password" 一词误判为字段），也防止通过注释隐藏危险内容。
_SQL_STRIP_PATTERN = re.compile(
    r"""'(?:''|\\.|[^'])*'|"(?:""|\\.|[^"])*"|--[^\r\n]*|#[^\r\n]*|/\*.*?\*/""",
    re.DOTALL,
)

_CAMEL_CASE_PATTERN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+")


def _identifier_tokens(identifier: str) -> list[str]:
    """把标识符按下划线与驼峰规则拆分为小写词元列表。"""
    tokens: list[str] = []
    for part in re.split(r"[_\W]+", identifier):
        tokens.extend(token.lower() for token in _CAMEL_CASE_PATTERN.findall(part))
    return tokens


def contains_sensitive_field(identifier: str) -> bool:
    """判断单个字段名/表名是否命中敏感隐私关键字。"""
    if any(keyword in identifier for keyword in SENSITIVE_FIELD_CN_KEYWORDS):
        return True
    tokens = _identifier_tokens(identifier)
    if any(token in SENSITIVE_FIELD_KEYWORDS for token in tokens):
        return True
    joined = "".join(tokens)
    return any(compound in joined for compound in _SENSITIVE_COMPOUND_KEYWORDS)


def find_sensitive_fields(sql_text: str) -> list[str]:
    """扫描 SQL 中引用的标识符，返回命中的敏感隐私字段列表。

    仅扫描去除字符串字面量和注释后的代码，因此
    ``WHERE note = 'my password'`` 这类值中出现的关键字不会被误判。
    """
    sql_code = _SQL_STRIP_PATTERN.sub(" ", sql_text)

    identifiers = [match.group(1) for match in re.finditer(r"`([^`]+)`", sql_code)]
    no_backtick_code = re.sub(r"`[^`]*`", " ", sql_code)
    identifiers.extend(
        match.group(0) for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", no_backtick_code)
    )

    found: list[str] = []
    seen: set[str] = set()

    # 中文标识符无法被拉丁字母正则提取，直接对去除字面量后的
    # SQL 文本做子串匹配（中文关键字不会出现在 SQL 保留字中）。
    for keyword in SENSITIVE_FIELD_CN_KEYWORDS:
        if keyword in sql_code:
            found.append(keyword)
            seen.add(keyword)

    for identifier in identifiers:
        if contains_sensitive_field(identifier) and identifier not in seen:
            seen.add(identifier)
            found.append(identifier)
    return found


def _sensitive_reject_message(fields: list[str]) -> str:
    """生成统一的敏感字段安全拒绝文案。"""
    return (
        "查询涉及敏感隐私字段（" + ", ".join(fields) + "），"
        "出于数据安全与隐私保护要求，系统已拒绝执行。"
        "请避免查询密码、密钥、认证令牌、身份证号、手机号、邮箱地址等敏感隐私数据。"
    )


# ---------------------------------------------------------------------- #
# SQL 与真实 schema 一致性核对（防止大模型编造不存在的表/字段）
# ---------------------------------------------------------------------- #

# MySQL 常用关键字与内置常量。凡是命中这份名单的裸标识符都不是表名/字段名，
# 直接跳过核对，避免把 COUNT、DESC 等误判为"编造的字段"。
_SQL_RESERVED_WORDS = frozenset(
    {
        "select", "from", "where", "group", "by", "order", "having", "limit",
        "offset", "join", "inner", "left", "right", "full", "outer", "cross",
        "on", "using", "as", "and", "or", "not", "in", "is", "null", "like",
        "between", "exists", "case", "when", "then", "else", "end", "asc",
        "desc", "distinct", "union", "all", "any", "some", "true", "false",
        "if", "ifnull", "nullif", "coalesce", "cast", "convert", "interval",
        "date", "time", "datetime", "timestamp", "year", "month", "day",
        "hour", "minute", "second", "count", "sum", "avg", "min", "max",
        "row_number", "rank", "dense_rank", "over", "partition", "with",
        "recursive", "div", "mod", "char", "varchar", "int", "integer",
        "bigint", "decimal", "float", "double", "text", "blob", "binary",
        "unsigned", "signed", "charset", "collate", "escape", "for", "both",
        "leading", "trailing", "regexp", "rlike", "sounds", "separator",
        "current_date", "current_time", "current_timestamp", "now",
        "curdate", "curtime", "utc_date", "utc_time", "utc_timestamp",
        "date_format", "date_add", "date_sub", "datediff", "timestampdiff",
        "unix_timestamp", "from_unixtime", "str_to_date", "extract",
        "last_day", "concat", "concat_ws", "substring", "substr", "length",
        "trim", "ltrim", "rtrim", "upper", "lower", "replace", "round",
        "floor", "ceil", "ceiling", "abs", "power", "sqrt", "greatest",
        "least", "format", "group_concat", "std", "stddev", "variance",
        "values", "dual", "window", "rows", "range", "preceding", "following",
        "unbounded", "current", "first", "last",
    }
)

# SELECT 语句中表引用位置的关键词（JOIN 家族与逗号之外的结构词），
# 解析 FROM/JOIN 子句时需要排除。
# 注意：on/using 不在其中——它们标志着 JOIN 条件开始，由状态机单独处理，
# 否则会把 ON 之后继续 JOIN 的表误判为子句结束而丢失。
_TABLE_CONTEXT_STOP_WORDS = frozenset(
    {
        "where", "group", "order", "having", "limit", "offset", "union",
        "set", "window",
    }
)

_JOIN_KEYWORDS = frozenset(
    {"join", "inner", "left", "right", "full", "outer", "cross", "straight_join"}
)

# 常见表别名前缀（如 s/r/u/t/o/a + 数字）。别名本身不是数据库对象，
# 但限定列引用（如 s.create_time）需要借助它定位到真实表。
_ALIAS_PATTERN = re.compile(r"^[a-z]{1,3}\d*$")

# 限定列引用的两段拆分：`alias`.`col` / alias.col / `alias`.col 等。
_QUALIFIED_REF_PATTERN = re.compile(
    r"`?([\w\u4e00-\u9fff]+)`?\s*\.\s*`?([\w\u4e00-\u9fff]+)`?"
)


def _extract_table_sequence(from_clause: str) -> list[tuple[str, str | None]]:
    """从 FROM/JOIN 子句文本中提取 ``(表名, 别名)`` 序列。

    支持逗号连接的多表与 JOIN 链；ON/USING 条件内的标识符不参与
    提取，条件结束后遇到下一个 JOIN 关键字继续收集后续表。
    子查询内部的表引用无法可靠归属别名，解析到括号即停止。
    """
    tokens: list[str] = []
    for raw in re.split(r"[\s()]+", from_clause.replace(",", " , ")):
        token = raw.strip("`").strip()
        if token:
            tokens.append(token)

    references: list[tuple[str, str | None]] = []
    expect_table = True
    pending_join = False
    skipping_on = False  # 位于 ON/USING 条件内部，忽略直到下一个 JOIN
    for token in tokens:
        lowered = token.lower()
        if token == ",":
            if not skipping_on:
                expect_table = True  # 逗号连接的下一个表
            continue
        if skipping_on:
            if lowered in _JOIN_KEYWORDS:
                pending_join = True
                skipping_on = False
                continue
            if lowered in _TABLE_CONTEXT_STOP_WORDS:
                break
            continue
        if expect_table:
            if lowered in _TABLE_CONTEXT_STOP_WORDS or lowered in {"select", "with"}:
                break
            references.append((token, None))
            expect_table = False
            continue
        if lowered in _JOIN_KEYWORDS:
            pending_join = True
            continue
        if lowered == "as":
            continue
        if lowered in _TABLE_CONTEXT_STOP_WORDS:
            break
        if lowered in {"on", "using"}:
            skipping_on = True
            continue
        if pending_join:
            references.append((token, None))
            pending_join = False
            expect_table = False
        elif _ALIAS_PATTERN.match(lowered) or not lowered.isupper():
            # 紧跟表名的短标识符视为别名（别名本身不参与核对）
            references[-1] = (references[-1][0], token)
    return references


def _extract_from_join_clause(sql_code: str) -> str | None:
    """提取去除字面量后的 SQL 中 FROM 之后、WHERE 等子句之前的片段。"""
    match = re.search(
        r"(?is)\bFROM\b(.*?)(?=\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b"
        r"|\bHAVING\b|\bLIMIT\b|\bUNION\b|\bWINDOW\b|$)",
        sql_code,
    )
    return match.group(1) if match else None


def validate_sql_schema(sql_text: str, db_manager) -> str | None:
    """核对 SQL 引用的表名/字段名是否与数据库真实 schema 一致。

    全部通过时返回 ``None``；发现编造（或写错）的表/字段时返回中文
    错误说明，其中会列出该表的真实字段，引导模型修正 SQL 或如实
    告知用户该维度无法查询，而不是继续编造。
    """
    sql_code = _SQL_STRIP_PATTERN.sub(" ", sql_text)

    from_clause = _extract_from_join_clause(sql_code)
    if from_clause is None:
        # 无法定位 FROM 子句时不做核对，交由 EXPLAIN 与真实执行兜底。
        return None

    table_refs = _extract_table_sequence(from_clause)
    if not table_refs:
        return None

    # 数据库真实表名集合（大小写不敏感比较）
    try:
        real_tables = db_manager.get_tables()
    except SQLAlchemyError:
        return None
    real_table_names = {str(item.get("name") or "") for item in real_tables}
    real_table_lower = {name.lower() for name in real_table_names if name}

    # 1. 核对表名：引用了不存在的表时直接拒绝并给出相近表名提示
    for table_name, _alias in table_refs:
        if table_name.lower() not in real_table_lower:
            return (
                f"SQL 引用了不存在的表 '{table_name}'。"
                "该表并不在数据库中，请勿编造表名；"
                "请调用 sql_db_list_tables 确认真实存在的表后重写 SQL。"
            )

    # 2. 建立 表/别名 -> 真实字段集合 的映射，并记录别名到真实表名的对应关系
    table_columns: dict[str, set[str]] = {}
    resolved_names: dict[str, str] = {}
    for table_name, alias in table_refs:
        matched = next(
            (name for name in real_table_names if name.lower() == table_name.lower()),
            None,
        )
        if matched is None:
            continue
        try:
            columns = db_manager.get_column_names(matched)
        except SQLAlchemyError:
            continue
        column_set = {str(column).lower() for column in columns}
        table_columns[table_name.lower()] = column_set
        resolved_names[table_name.lower()] = matched
        if alias:
            table_columns[alias.lower()] = column_set
            resolved_names[alias.lower()] = matched

    # 3. 核对裸列引用（未加表限定）：只要任一被引用表的字段集合包含它即放行
    all_columns = set().union(*table_columns.values()) if table_columns else set()
    qualified_parts = set()
    for match in _QUALIFIED_REF_PATTERN.finditer(sql_code):
        qualified_parts.add((match.start(), match.end()))

    # AS 定义的别名（如 COUNT(s.id) AS site_count）不是数据库字段，
    # ORDER BY/GROUP BY 也允许引用别名，统一排除以免误报。
    alias_names = {
        match.group(1).lower()
        for match in re.finditer(
            r"(?i)\bAS\s+`?([\w\u4e00-\u9fff]+)`?", sql_code
        )
    }

    problems: list[str] = []
    for match in re.finditer(r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*", sql_code):
        if any(start <= match.start() < end for start, end in qualified_parts):
            continue
        word = match.group(0)
        lowered = word.lower()
        if lowered in _SQL_RESERVED_WORDS or lowered in table_columns:
            continue
        if lowered in alias_names:
            continue
        # 紧跟左括号的标识符是函数调用（含自定义函数），不作为字段核对
        if re.match(r"\s*\(", sql_code[match.end():]):
            continue
        if all_columns and lowered not in all_columns:
            problems.append(f"字段 '{word}' 在本次引用的任何表中都不存在")

    # 4. 核对限定列引用 alias.col / table.col
    for match in _QUALIFIED_REF_PATTERN.finditer(sql_code):
        prefix, column = match.group(1), match.group(2)
        prefix_lower, column_lower = prefix.lower(), column.lower()
        if prefix_lower in _SQL_RESERVED_WORDS:
            continue
        column_set = table_columns.get(prefix_lower)
        if column_set is None:
            # 前缀无法对应到已知表/别名（可能来自子查询），跳过以免误伤
            continue
        if column_lower not in column_set:
            resolved = resolved_names.get(prefix_lower, prefix)
            problems.append(
                f"表 '{resolved}' 中不存在字段 '{column}'，"
                f"该表实际字段为: {', '.join(sorted(column_set))}"
            )

    if not problems:
        return None

    unique_problems = list(dict.fromkeys(problems))
    return (
        "SQL 与数据库真实结构不一致，已拒绝执行：\n- "
        + "\n- ".join(unique_problems)
        + "\n严禁编造 schema 中不存在的表名或字段名。请根据上述真实字段重写 SQL；"
        "若用户问题的统计维度在现有字段中无法满足，必须如实告知用户无法按该条件查询，"
        "不得虚构字段或伪造过滤条件。"
    )


# ---------------------------------------------------------------------- #
# 唯一准备入口：串联全部校验
# ---------------------------------------------------------------------- #

def prepare_select(
    sql: str,
    db_manager=None,
    *,
    hard_cap: int = 1000,
    apply_limit: bool = True,
) -> tuple[str | None, str | None]:
    """只读查询的唯一准备入口，依次执行：

    1. :func:`guard_select`  AST 级只读校验
    2. :func:`find_sensitive_fields`  敏感隐私字段拦截
    3. :func:`validate_sql_schema`  表/字段真实性核对（db_manager 为 None 时跳过）
    4. :func:`ensure_limit`  强制 LIMIT 上限（apply_limit 为 False 时跳过）

    返回 ``(safe_sql, error)``：全部通过时 error 为 ``None``；
    任一环节失败时 safe_sql 为 ``None``，error 为可直接展示的中文说明。
    """
    ok, message = guard_select(sql)
    if not ok:
        return None, message

    normalized = sql.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()

    sensitive_fields = find_sensitive_fields(normalized)
    if sensitive_fields:
        return None, _sensitive_reject_message(sensitive_fields)

    if db_manager is not None:
        schema_error = validate_sql_schema(normalized, db_manager)
        if schema_error:
            return None, schema_error

    if apply_limit:
        normalized = ensure_limit(normalized, hard_cap)
    return normalized, None
