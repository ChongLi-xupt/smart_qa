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




def validate_sql_schema(sql_text: str, db_manager) -> str | None:
    """核对 SQL 引用的表名/字段名是否与数据库真实 schema 一致（AST 版）。

    基于 sqlglot 语法树，天然覆盖多表 JOIN、逗号连接表、子查询、
    CTE 与 UNION；全部通过时返回 ``None``；发现编造（或写错）的
    表/字段时返回中文错误说明，其中会列出该表的真实字段，引导模型
    修正 SQL 或如实告知用户该维度无法查询，而不是继续编造。
    SQL 无法解析或读取不到数据库元数据时不做核对，交由 EXPLAIN 与
    真实执行兜底。
    """
    normalized = sql_text.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if not normalized:
        return None

    try:
        statement = sqlglot.parse_one(normalized, read="mysql")
    except sqlglot.errors.ParseError:
        # 语法都解析不了时不做核对，EXPLAIN 会给出更准确的错误信息
        return None
    if not isinstance(statement, (exp.Select, exp.Union)):
        # 非 SELECT 语句已被 guard_select 拒绝，此处无需核对
        return None

    # CTE（WITH ... AS）名称与派生表（子查询）别名不是物理表，
    # 其列也无法对照真实字段，统一记录后跳过。
    virtual_names = {
        node.alias.lower()
        for node in statement.find_all(exp.CTE, exp.Subquery)
        if node.alias
    }

    # 1. 收集物理表引用（含别名），先核对表名：引用不存在的表直接拒绝
    table_refs: list[tuple[str, str]] = []
    for table in statement.find_all(exp.Table):
        table_name = table.name
        if not table_name or table_name.lower() in virtual_names:
            continue
        table_refs.append((table_name, table.alias or ""))

    if not table_refs:
        return None

    try:
        real_tables = db_manager.get_tables()
    except SQLAlchemyError:
        return None
    real_table_names = {str(item.get("name") or "") for item in real_tables}
    real_table_lower = {name.lower() for name in real_table_names if name}

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

    if not table_columns:
        return None

    all_columns = set().union(*table_columns.values())
    # AS 定义的输出别名（如 COUNT(*) AS cnt），ORDER BY/GROUP BY 可合法引用
    output_aliases = {
        node.alias.lower() for node in statement.find_all(exp.Alias) if node.alias
    }

    # 3. 遍历语法树中的全部列引用逐一核对（函数、字面量、关键字在
    # AST 中各有节点类型，不会混入列引用，无需保留字名单过滤）。
    # 同时记录每个节点所属的 SELECT：歧义检测只在单个 SELECT 作用域
    # 内做，UNION 各分支的同名列互不冲突。
    column_scopes: list[tuple[exp.Expression, exp.Select | None]] = []
    select_table_refs: dict[exp.Select, set[str]] = {}
    for node in statement.walk():
        if isinstance(node, exp.Table):
            if node.name and node.name.lower() not in virtual_names:
                scope = node.find_ancestor(exp.Select, exp.Union)
                if isinstance(scope, exp.Select):
                    refs = select_table_refs.setdefault(scope, set())
                    refs.add(node.name.lower())
                    if node.alias:
                        refs.add(node.alias.lower())
        elif isinstance(node, exp.Column):
            column_scopes.append(
                (node, node.find_ancestor(exp.Select, exp.Union))
            )

    problems: list[str] = []
    for column, scope in column_scopes:
        column_name = column.name
        if not column_name:
            continue
        column_lower = column_name.lower()
        prefix = column.table
        if prefix:
            prefix_lower = prefix.lower()
            if prefix_lower in virtual_names:
                continue  # CTE/派生表的列引用无法对照真实字段
            column_set = table_columns.get(prefix_lower)
            if column_set is None:
                # 前缀无法对应到已知表（可能来自子查询），跳过以免误伤
                continue
            if column_lower not in column_set:
                resolved = resolved_names.get(prefix_lower, prefix)
                problems.append(
                    f"表 '{resolved}' 中不存在字段 '{column_name}'，"
                    f"该表实际字段为: {', '.join(sorted(column_set))}"
                )
            continue

        # 裸列（未加表限定）
        if column_lower in output_aliases:
            continue
        if column_lower not in all_columns:
            problems.append(f"字段 '{column_name}' 在本次引用的任何表中都不存在")
            continue
        holders = sorted(
            {
                resolved_names[key]
                for key, column_set in table_columns.items()
                if column_lower in column_set
            }
        )
        if len(holders) > 1:
            # 只在本 SELECT 实际引用的表范围内判歧义：全局存在多表
            # 但本分支只用了其一（如 UNION 另一侧）时不构成歧义。
            scope_refs = (
                select_table_refs.get(scope, set())
                if isinstance(scope, exp.Select)
                else set(table_columns)
            )
            holders_in_scope = sorted(
                {
                    resolved_names[key]
                    for key in scope_refs
                    if key in table_columns and column_lower in table_columns[key]
                }
            )
            if len(holders_in_scope) > 1:
                problems.append(
                    f"字段 '{column_name}' 同时存在于多个表"
                    f"（{', '.join(holders_in_scope)}），"
                    "请为其加上表名或别名限定，避免执行时产生歧义"
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
