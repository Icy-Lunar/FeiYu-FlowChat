"""Domain exceptions translated to stable JSON errors by the API layer."""


class FlowChatError(Exception):
    """Base error safe to show to a local UI user."""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "flowchat_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class ConversationNotFound(FlowChatError):
    """Raised when an archive or revision cannot be found."""

    def __init__(self) -> None:
        super().__init__("找不到该对话归档", status_code=404, code="conversation_not_found")


class ImportError(FlowChatError):
    """Raised for invalid share links, archives, and backup packages."""

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message, status_code=status_code, code="import_failed")


class ProviderError(FlowChatError):
    """Raised when an OpenAI-compatible provider cannot serve a request."""

    def __init__(self, message: str, *, status_code: int = 502,
                 upstream_status: int | None = None, upstream_code: str | None = None,
                 upstream_param: str | None = None) -> None:
        super().__init__(message, status_code=status_code, code="provider_error")
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code
        self.upstream_param = upstream_param
