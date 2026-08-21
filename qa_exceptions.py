"""智慧问数分层异常体系。

异常分层（P1）：
    SmartQAError          所有业务异常的基类，携带对外安全文案与 HTTP 状态码
    ├── ConfigError       配置缺失/非法（启动阶段）            503
    ├── ParameterError    请求参数不合法（类型/长度/格式）      400
    ├── LLMError          大模型调用超时 / 网络异常 / 限流      502
    └── DatabaseError     数据库不可达 / 连接失败               503

使用约定：
- ``detail`` 记录完整错误细节，只允许进入服务端日志，禁止直接返回前端；
- ``public_message`` 是面向用户的统一友好文案，由 FastAPI 异常处理器输出。
"""

from __future__ import annotations


class SmartQAError(Exception):
    """业务异常基类。子类覆盖 ``http_code`` 与 ``public_message``。"""

    http_code: int = 500
    public_message: str = "服务暂时不可用，请稍后重试。"

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(detail or self.public_message)


class ConfigError(SmartQAError):
    """配置缺失或非法：应在启动阶段 fail-fast。"""

    http_code = 503
    public_message = "服务配置异常，请联系管理员。"


class ParameterError(SmartQAError):
    """请求参数不合法：类型错误、超长、缺失必填字段等。"""

    http_code = 400
    public_message = "请求参数不合法。"


class LLMError(SmartQAError):
    """大模型服务异常：调用超时、网络故障、触发限流等。"""

    http_code = 502
    public_message = "智能分析服务暂时不可用，请稍后重试。"


class DatabaseError(SmartQAError):
    """数据库异常：连接失败、不可达、会话建立失败等。"""

    http_code = 503
    public_message = "数据库服务暂时不可用，请稍后重试。"
