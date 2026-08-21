"""智慧问数：基于 LangGraph 的自然语言数据库问答模块。

用户用自然语言提问，由大模型 Agent 调用数据库工具完成
"列表 → 取 schema → 写 SQL → 校验 → 执行 → 总结" 的完整链路，
最终返回中文答案、所使用的 SQL 以及中间工具调用过程。

基于 LangChain 1.x / LangGraph 1.x 的 ``create_react_agent`` 实现。

依赖：
    - database.MySQLDatabase             数据库连接与元数据读取
    - agent_tools.*                      供 Agent 调用的四个工具
    - langchain_openai.ChatOpenAI        OpenAI 兼容的大模型客户端

环境变量（用于 ``__main__`` 交互式运行）：
    LLM_API_KEY   大模型 API Key（必填）
    LLM_BASE_URL  OpenAI 兼容接口地址，默认 DeepSeek
    LLM_MODEL     模型名称，默认 deepseek-chat
    DB_HOST / DB_USER / DB_PASSWORD / DB_NAME / DB_PORT
                  数据库连接信息，默认沿用项目内测试库
"""

from __future__ import annotations

import json
import os
import time
import warnings
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from qa_exceptions import ConfigError, DatabaseError, LLMError, SmartQAError
from rag import format_few_shot, format_glossary, retrieve_examples

# 可选依赖的异常类型：用于把 Agent 底层错误归类为 LLMError。
try:
    from openai import APIError as _OpenAIAPIError
except ImportError:  # pragma: no cover - openai 随 langchain-openai 安装
    _OpenAIAPIError = ()

try:
    import httpx as _httpx
except ImportError:  # pragma: no cover
    _httpx = None

# langchain_core 在导入时会强制开启自身的弃用告警过滤器，因此需要先导入
# langchain_core，再插入忽略规则，最后才导入 langgraph，才能屏蔽 langgraph
# checkpoint 抛出的 allowed_objects 无影响告警。
# 故下方导入必须位于 warnings.filterwarnings 之后（E402 为有意为之）。
import langchain_core  # noqa: F401  触发 langchain_core 的过滤器初始化
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)

from langchain.agents import create_agent  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI  # noqa: E402

from agent_tools import (  # noqa: E402
    SQLCheckTool,
    SQLExecuteTool,
    TablesListTool,
    TablesSchemaTool,
)
from database import MySQLDatabase  # noqa: E402

# ---------------------------------------------------------------------- #
# 敏感隐私问题检测
# ---------------------------------------------------------------------- #

# 用户问题命中以下任一关键词时，直接拒绝、不进入 Agent，
# 从源头避免生成任何触碰敏感字段的 SQL。
SENSITIVE_QUESTION_KEYWORDS = (
    # 凭证 / 密钥类
    "密码", "口令", "密钥", "秘钥", "私钥", "盐值",
    "令牌", "token", "凭据", "凭证", "apikey", "api key",
    # 个人身份 / 联系方式类
    "身份证", "手机号", "电话号码", "联系电话", "座机号",
    "邮箱", "电邮", "email", "邮件地址",
)

SENSITIVE_REJECT_MESSAGE = (
    "出于数据安全与隐私保护要求，系统不会执行涉及密码、密钥、认证令牌、"
    "身份证号、手机号、邮箱地址等敏感隐私字段的查询。"
    "请调整您的问题，避免涉及上述敏感隐私数据后重试。"
)


def detect_sensitive_question(question: str) -> list[str]:
    """返回用户问题命中的全部敏感隐私关键词（大小写不敏感）。"""
    lowered = (question or "").lower()
    return [keyword for keyword in SENSITIVE_QUESTION_KEYWORDS if keyword.lower() in lowered]


# 默认系统提示词：约束 Agent 的行为与作答格式。
DEFAULT_SYSTEM_PROMPT = """你是一位资深的数据分析助手"智慧问数"，可以通过编写并执行 SQL 查询 MySQL 数据库来回答用户的自然语言问题。

请严格按下面的工作流程完成任务：
1. 先调用 `sql_db_list_tables` 了解数据库中有哪些表及其备注；
2. 根据用户问题挑选可能相关的表，调用 `sql_db_schema` 获取这些表的字段名、类型、备注、主键和外键等信息（必要时可多次调用补充更多表）；
3. 基于 schema 编写一条只读 SELECT 查询（MySQL 方言），**只能使用 schema 中真实存在的字段**，先调用 `sql_db_checker` 校验语法与表/字段引用是否正确；
4. 校验通过后再调用 `sql_db_execute` 执行查询；如果校验或执行失败，请仔细阅读错误信息、修正 SQL 后重试，最多重试 3 次；若失败原因是字段/表不存在，**绝不能虚构字段重试**，应如实告知用户；
5. **数据库不可用快速失败**：若任何工具返回的错误文本含 "Can't connect"、"timed out"、"超时"、"连接" 等连接失败信息，说明数据库服务当前不可达，**禁止重试任何数据库工具**（重试只会重复等待连接超时），立即用中文如实告知用户"数据库连接异常，请检查网络或联系管理员"，并附上错误摘要；
6. 结合查询结果，用中文给出最终回答。

编写 SQL 时的注意事项：
- 只允许 SELECT 查询，严禁 INSERT/UPDATE/DELETE/DDL/加锁/写文件等任何写操作；
- **严禁编造字段和表名**：SQL 中引用的每一个表名、字段名都必须来自 `sql_db_schema` 返回的真实 schema，绝不能凭常识或猜测添加 schema 中不存在的字段（如 create_time、update_time、status 等）；编写 SQL 前必须先获取相关表的 schema；
- **无法查询时如实告知**：如果用户问题的某个条件（如时间范围、状态筛选）在相关表的 schema 中找不到对应字段，不要虚构字段硬套条件，应如实告知用户"该表没有记录 XX 信息的字段，无法按此条件查询"，并可建议改用其他可用字段或询问用户是否调整问题；
- 回答中引用的所有数字、表名、字段名必须来自真实的查询结果或 schema，严禁自行编造数据；
- 严禁查询密码、密钥、认证令牌、身份证号、手机号、邮箱地址等敏感隐私字段；如果用户的问题涉及这些字段，请直接拒绝并说明出于数据安全与隐私保护不能提供，不要编写或执行任何涉及这些字段的 SQL；
- 表名、字段名如果是 MySQL 关键字或含特殊字符，请用反引号 `` ` `` 包裹；
- 涉及时间的过滤、分组、排序，请使用 MySQL 日期函数（如 DATE()、DATE_FORMAT()、YEAR()、NOW()、INTERVAL 等）；
- 字段含义请优先参考字段备注；模糊匹配用 LIKE 且尽量精确；
- 只查询回答问题所需的最少数据，避免 SELECT * 和返回过多行。

最终回答格式（直接用中文回答用户，不要暴露内部思考）：
- 先用一两句话直接回答用户的问题；
- 随后给出本次使用的 SQL（用 ```sql 代码块包裹）；
- 最后简要说明结果情况（共多少行、关键数值或趋势等）。如果未能查出数据，请说明原因。"""


@dataclass
class DatabaseConfig:
    """MySQL 数据库连接配置。"""

    host: str
    username: str
    password: str
    database: str
    port: int = 3306
    ssl_disabled: bool = False
    # 建连超时（秒）：数据库不可达时每次工具调用的最大等待时间，
    # 黑洞网络下 OS 层 TCP 超时可能长达分钟级，必须靠它兑底
    connect_timeout: int = 10


@dataclass
class LLMConfig:
    """OpenAI 兼容大模型的访问配置。"""

    api_key: str
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    temperature: float = 0.0
    request_timeout: int = 60


class SmartQA:
    """智慧问数 Agent。

    通过自然语言向 MySQL 数据库提问，Agent 会自主调用
    列表 / schema / 校验 / 执行 四个工具完成数据查询并给出中文答案。

    Example:
        >>> qa = SmartQA(db_config, llm_config)
        >>> result = qa.ask("最近 7 天新增了多少用户？")
        >>> print(result["answer"])
        >>> qa.close()
    """

    def __init__(
        self,
        db_config: DatabaseConfig,
        llm_config: LLMConfig,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_iterations: int = 15,
        verbose: bool = False,
    ) -> None:
        self.db_config = db_config
        self.llm_config = llm_config
        # 业务术语表（rag/glossary.md，P3-16）随系统提示词一次性注入；
        # 文件缺失或全注释时 format_glossary 返回空串，行为不变。
        glossary_text = format_glossary()
        if glossary_text:
            system_prompt = f"{system_prompt}\n\n{glossary_text}"
        self.system_prompt = system_prompt
        self.verbose = verbose
        # LangGraph 每轮工具调用大约消耗 2 个图步（模型调用 + 工具执行），
        # recursion_limit 需覆盖所有步数，这里按 max_iterations 放大并留余量。
        self.recursion_limit = max(max_iterations * 2 + 2, 25)

        # 复用项目已有的 MySQLDatabase，连接信息集中管理。
        self.database = MySQLDatabase(
            host=db_config.host,
            username=db_config.username,
            password=db_config.password,
            database=db_config.database,
            port=db_config.port,
            ssl_disabled=db_config.ssl_disabled,
            connect_timeout=db_config.connect_timeout,
        )

        self.llm = self._build_llm()
        self.tools = self._build_tools()
        self.agent = self._build_agent()

    # ------------------------------------------------------------------ #
    # 构建各组件
    # ------------------------------------------------------------------ #

    def _build_llm(self) -> ChatOpenAI:
        return ChatOpenAI(
            api_key=self.llm_config.api_key,
            base_url=self.llm_config.base_url,
            model=self.llm_config.model,
            temperature=self.llm_config.temperature,
            request_timeout=self.llm_config.request_timeout,
        )

    def _build_tools(self) -> list[Any]:
        return [
            TablesListTool(db_manager=self.database),
            TablesSchemaTool(db_manager=self.database),
            SQLCheckTool(db_manager=self.database),
            SQLExecuteTool(db_manager=self.database),
        ]

    def _build_agent(self) -> Any:
        # LangChain 1.x 推荐使用 langchain.agents.create_agent（langgraph 的
        # create_react_agent 在 V1.0 已弃用）。system_prompt 作为系统消息，
        # model 通过 bind_tools 接入工具的原生函数调用能力。
        return create_agent(
            model=self.llm,
            tools=self.tools,
            system_prompt=self.system_prompt,
        )

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #

    def ask(
        self,
        question: str,
        chat_history: list[BaseMessage] | None = None,
    ) -> dict[str, Any]:
        """向数据库提出一个自然语言问题，返回结构化结果。

        返回字段：
            - question: 原始问题
            - answer:   Agent 的最终中文回答
            - sql:      从工具调用中提取的最后一条 SQL（可能为 None）
            - data:     最后一次成功执行的查询结果，含 columns/rows 等（可能为 None）
            - intermediate_steps: 工具调用过程列表，每项含 tool / input / output
            - rejected: 是否因命中敏感隐私问题被拒绝（拒绝时不会调用 Agent）
            - error:    保留字段，成功时恒为 None；Agent 整体失败时改为
                        抛出分层异常（SmartQAError 及其子类），由调用方处理
            - usage:    本次调用的 token 用量与工具调用次数统计
            - duration_ms: Agent 执行耗时（毫秒）
        """
        # 敏感隐私问题拦截：问题本身命中敏感关键词时直接拒绝，
        # 不进入 Agent，避免任何 SQL 触碰敏感字段。
        if detect_sensitive_question(question):
            return {
                "question": question,
                "answer": SENSITIVE_REJECT_MESSAGE,
                "sql": None,
                "data": None,
                "intermediate_steps": [],
                "rejected": True,
                "error": None,
            }

        messages: list[BaseMessage] = self._build_messages(question, chat_history)

        config = {"recursion_limit": self.recursion_limit}
        if self.verbose:
            config["debug"] = False  # True 会输出图执行明细，按需开启

        started = time.monotonic()
        try:
            result = self.agent.invoke({"messages": messages}, config=config)
        except SmartQAError:
            raise
        except Exception as exc:  # noqa: BLE001 - 分类后以分层异常上抛
            raise _classify_agent_error(exc) from exc
        duration_ms = int((time.monotonic() - started) * 1000)

        messages_out: list[BaseMessage] = result.get("messages", [])
        intermediate_steps = self._extract_steps_from_messages(messages_out)

        return {
            "question": question,
            "answer": self._extract_final_answer(messages_out),
            "sql": self._extract_sql(intermediate_steps),
            "data": self._extract_data(intermediate_steps),
            "intermediate_steps": intermediate_steps,
            "rejected": False,
            "error": None,
            "usage": self._collect_usage(messages_out),
            "duration_ms": duration_ms,
        }

    def ask_stream(
        self,
        question: str,
        chat_history: list[BaseMessage] | None = None,
    ):
        """流式提问生成器（P3-15）：边执行边产出事件，消除“白等”。

        事件序列（每个 yield 均为 dict）：
            {"type": "step_start", "tool": ...}     Agent 声明要调用某工具
            {"type": "step_done", "tool": ..., "output": ...}  工具返回（截断）
            {"type": "result", "result": {...}}      最终结构化结果（字段同 ask）

        敏感拒绝不经过 Agent，直接产出 rejected 的 result 事件；
        Agent 异常按分层异常上抛，由调用方（Web 层）捕获并转为错误事件。
        """
        if detect_sensitive_question(question):
            yield {
                "type": "result",
                "result": {
                    "question": question,
                    "answer": SENSITIVE_REJECT_MESSAGE,
                    "sql": None,
                    "data": None,
                    "intermediate_steps": [],
                    "rejected": True,
                    "error": None,
                },
            }
            return

        messages = self._build_messages(question, chat_history)
        config = {"recursion_limit": self.recursion_limit}

        collected: list[BaseMessage] = []
        started = time.monotonic()
        try:
            # stream_mode="updates"：每个图节点完成后推送一次增量状态，
            # 工具步骤可实时透出；消息对象完整，可直接复用提取辅助。
            for update in self.agent.stream(
                {"messages": messages}, config=config, stream_mode="updates"
            ):
                for node_output in update.values():
                    for message in node_output.get("messages") or []:
                        collected.append(message)
                        if isinstance(message, AIMessage) and message.tool_calls:
                            for call in message.tool_calls:
                                yield {"type": "step_start", "tool": str(call.get("name") or "")}
                        elif isinstance(message, ToolMessage):
                            output = str(message.content or "")
                            if len(output) > 200:
                                output = output[:200] + " …(已截断)"
                            yield {
                                "type": "step_done",
                                "tool": str(message.name or ""),
                                "output": output,
                            }
        except SmartQAError:
            raise
        except Exception as exc:  # noqa: BLE001 - 分类后以分层异常上抛
            raise _classify_agent_error(exc) from exc
        duration_ms = int((time.monotonic() - started) * 1000)

        intermediate_steps = self._extract_steps_from_messages(collected)
        yield {
            "type": "result",
            "result": {
                "question": question,
                "answer": self._extract_final_answer(collected),
                "sql": self._extract_sql(intermediate_steps),
                "data": self._extract_data(intermediate_steps),
                "intermediate_steps": intermediate_steps,
                "rejected": False,
                "error": None,
                "usage": self._collect_usage(collected),
                "duration_ms": duration_ms,
            },
        }

    def close(self) -> None:
        """释放数据库连接池。"""
        self.database.close()

    def _build_messages(
        self,
        question: str,
        chat_history: list[BaseMessage] | None,
    ) -> list[BaseMessage]:
        """组装本轮输入消息：历史 + few-shot 示例（P3-16，命中才注入）+ 问题。"""
        messages: list[BaseMessage] = list(chat_history or [])
        few_shot = format_few_shot(retrieve_examples(question))
        if few_shot:
            messages.append(SystemMessage(content=few_shot))
        messages.append(HumanMessage(content=question))
        return messages

    def __enter__(self) -> "SmartQA":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 结果解析辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _collect_usage(messages: list[BaseMessage]) -> dict[str, Any]:
        """汇总本次调用的 LLM token 用量与工具调用次数（供日志/监控）。"""
        input_tokens = output_tokens = tool_calls = 0
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            tool_calls += len(message.tool_calls or [])
            usage = getattr(message, "usage_metadata", None) or {}
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tool_calls": tool_calls,
        }

    @staticmethod
    def _extract_steps_from_messages(
        messages: list[BaseMessage],
    ) -> list[dict[str, Any]]:
        """从 LangGraph 输出的消息流中提取工具调用过程。

        AIMessage 携带 tool_calls（声明要调用什么），紧随其后的 ToolMessage
        携带该次调用的返回结果。通过 tool_call_id 把二者配对成一条步骤。
        """
        steps: list[dict[str, Any]] = []
        # tool_call_id -> (tool_name, args)
        pending: dict[str, tuple[str, Any]] = {}

        for message in messages:
            if isinstance(message, AIMessage):
                for tool_call in message.tool_calls or []:
                    pending[tool_call["id"]] = (
                        tool_call["name"],
                        tool_call.get("args", {}),
                    )
            elif isinstance(message, ToolMessage):
                tool_call_id = message.tool_call_id
                if tool_call_id in pending:
                    name, args = pending.pop(tool_call_id)
                else:
                    name = message.name or "(未知工具)"
                    args = {}
                steps.append(
                    {
                        "tool": name,
                        "input": args,
                        "output": message.content,
                    }
                )
        return steps

    @staticmethod
    def _extract_final_answer(messages: list[BaseMessage]) -> str:
        """取最后一条 AIMessage 的文本内容作为最终回答。"""
        for message in reversed(messages):
            if isinstance(message, AIMessage) and message.content:
                if isinstance(message.content, str):
                    return message.content.strip()
                # 部分模型返回 list[dict] 形式的 content，拼出文本
                return "".join(
                    part.get("text", "")
                    for part in message.content
                    if isinstance(part, dict)
                ).strip()
        return ""

    @staticmethod
    def _extract_sql(steps: list[dict[str, Any]]) -> str | None:
        """从中间步骤中提取最后一条被使用过的 SQL。

        优先取最后一次执行(sql_db_execute)的 query，其次取最后一次
        校验(sql_db_checker)的 query。
        """
        checker_sql: str | None = None
        for step in steps:
            if step["tool"] == "sql_db_execute":
                query = SmartQA._query_from_input(step["input"])
                if query:
                    return query
            if step["tool"] == "sql_db_checker":
                query = SmartQA._query_from_input(step["input"])
                if query:
                    checker_sql = query
        return checker_sql

    @staticmethod
    def _extract_data(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
        """提取最后一次成功执行的查询结果数据。

        ``sql_db_execute`` 成功时返回含 columns/rows 的 JSON 字符串；
        失败时返回中文错误文本（无法解析为 JSON，会被自动跳过）。
        """
        data: dict[str, Any] | None = None
        for step in steps:
            if step["tool"] != "sql_db_execute":
                continue
            output = step["output"]
            if isinstance(output, list):
                # 部分模型/框架会把 ToolMessage 内容存为分片列表
                output = "".join(
                    part.get("text", "")
                    for part in output
                    if isinstance(part, dict)
                )
            if not isinstance(output, str):
                continue
            try:
                parsed = json.loads(output)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict) and "columns" in parsed and "rows" in parsed:
                data = parsed
        return data

    @staticmethod
    def _query_from_input(tool_input: Any) -> str | None:
        if isinstance(tool_input, dict):
            query = tool_input.get("query")
            if isinstance(query, str) and query.strip():
                return query.strip()
        elif isinstance(tool_input, str):
            # 兼容 tool_input 为 JSON 字符串的情况（例如格式化前或非结构化输入）
            try:
                parsed = json.loads(tool_input)
            except (json.JSONDecodeError, ValueError):
                return None
            return SmartQA._query_from_input(parsed)
        return None


# ---------------------------------------------------------------------- #
# 异常分类：把 Agent 底层错误归入分层异常体系
# ---------------------------------------------------------------------- #

def _classify_agent_error(exc: Exception) -> SmartQAError:
    """按异常类型归类：数据库 / 大模型 / 通用，供上层统一处理。"""
    if isinstance(exc, SQLAlchemyError):
        return DatabaseError(f"数据库访问失败: {exc}")
    if _OpenAIAPIError and isinstance(exc, _OpenAIAPIError):
        return LLMError(f"大模型服务异常: {exc}")
    if _httpx is not None and isinstance(exc, _httpx.HTTPError):
        return LLMError(f"大模型调用网络异常: {exc}")
    if isinstance(exc, RecursionError) or "recursion" in str(exc).lower():
        return SmartQAError(f"Agent 推理轮次超限，请简化问题后重试: {exc}")
    return SmartQAError(f"Agent 执行失败: {exc}")


# ---------------------------------------------------------------------- #
# 交互式命令行入口
# ---------------------------------------------------------------------- #

def _config_from_env() -> tuple[DatabaseConfig, LLMConfig]:
    """从环境变量（或项目根目录 .env 文件）读取数据库与大模型配置。

    凭据一律不写入源码：数据库地址、账号、密码缺失时直接报错，
    请参照 .env.example 创建 .env 并填写真实值。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()  # 存在 .env 时加载，不存在时静默跳过
    except ImportError:
        pass

    missing_db = [
        name
        for name in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")
        if not os.getenv(name)
    ]
    if missing_db:
        raise ConfigError(
            f"未检测到数据库配置: {', '.join(missing_db)}。\n"
            "请复制 .env.example 为 .env 并填写只读账号的连接信息，"
            "或设置对应的环境变量。"
        )

    db_config = DatabaseConfig(
        host=os.environ["DB_HOST"],
        username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        port=int(os.getenv("DB_PORT", "3306")),
        ssl_disabled=os.getenv("DB_SSL_DISABLED", "true").lower() == "true",
        connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
    )

    api_key = os.getenv("LLM_API_KEY", "")
    if not api_key:
        raise ConfigError(
            "未检测到 LLM_API_KEY 环境变量。\n"
            "请先设置大模型 API Key，例如：\n"
            '    $env:LLM_API_KEY = "sk-xxxxxx"\n'
            "可选环境变量：LLM_BASE_URL（默认 DeepSeek）、LLM_MODEL（默认 deepseek-chat）、"
            "DB_HOST/DB_USER/DB_PASSWORD/DB_NAME/DB_PORT。"
        )

    llm_config = LLMConfig(
        api_key=api_key,
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        model=os.getenv("LLM_MODEL", "deepseek-chat"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.0")),
        request_timeout=int(os.getenv("LLM_TIMEOUT", "60")),
    )
    return db_config, llm_config


def _print_result(result: dict[str, Any]) -> None:
    """在终端友好地打印一次问答结果。"""
    print("\n" + "=" * 80)
    print("【问题】", result["question"])
    print("-" * 80)
    if result.get("error"):
        print("【错误】", result["error"])
    else:
        print("【回答】")
        print(result.get("answer") or "(Agent 未给出回答)")
    if result.get("sql"):
        print("\n【SQL】")
        print("```sql")
        print(result["sql"])
        print("```")
    print("-" * 80)
    print("【工具调用过程】")
    steps = result.get("intermediate_steps") or []
    if not steps:
        print("  (无)")
    for index, step in enumerate(steps, start=1):
        tool_input = step["input"]
        if isinstance(tool_input, (dict, list)):
            tool_input = json.dumps(tool_input, ensure_ascii=False)
        output = step["output"]
        if len(output) > 500:
            output = output[:500] + " …(已截断)"
        print(f"  {index}. 工具: {step['tool']}")
        print(f"     输入: {tool_input}")
        print(f"     输出: {output}")
    print("=" * 80)


def main() -> None:
    """交互式问答入口：读取配置 → 连接数据库 → 循环提问。"""
    try:
        db_config, llm_config = _config_from_env()
    except ConfigError as exc:
        raise SystemExit(exc.detail) from exc
    print(
        f"已加载配置：数据库 {db_config.database}@{db_config.host}:{db_config.port}，"
        f"模型 {llm_config.model}（{llm_config.base_url}）。"
    )

    # 多轮对话历史，保留最近若干轮以提供上下文。
    chat_history: list[BaseMessage] = []

    with SmartQA(db_config, llm_config, verbose=False) as qa:
        print("智慧问数已就绪，输入问题开始提问；输入 exit 或 quit 退出。\n")
        while True:
            try:
                question = input("请输入问题> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                break
            if not question:
                continue
            if question.lower() in {"exit", "quit", "q"}:
                print("再见。")
                break

            try:
                result = qa.ask(question, chat_history=chat_history)
            except SmartQAError as exc:
                print(f"\n【错误】{exc.public_message}")
                print(f"细节: {exc.detail}")
                continue
            _print_result(result)

            # 维护对话历史（仅保留每轮最终文本，保留最近 6 条避免上下文过长）。
            chat_history.append(HumanMessage(content=question))
            chat_history.append(AIMessage(content=result.get("answer") or ""))
            chat_history[:] = chat_history[-6:]


if __name__ == "__main__":
    main()
