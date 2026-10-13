"""框架统一异常类型。"""

from __future__ import annotations


class AiPrError(Exception):
    """所有业务异常的基类，带一个可选的机器可读错误码。"""

    code = "aipr_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class MediaAccessError(AiPrError):
    """素材路径不在允许的根目录内，或路径不可用。"""

    code = "media_access_denied"


class PlanValidationError(AiPrError):
    """大模型返回的剪辑方案不合法。"""

    code = "plan_invalid"


class LLMError(AiPrError):
    """调用大模型失败。"""

    code = "llm_error"


class PremiereError(AiPrError):
    """与 Premiere Pro 交互失败。"""

    code = "premiere_error"


class PremiereUnavailableError(PremiereError):
    """Premiere Pro / 桥接组件不可用。"""

    code = "premiere_unavailable"
