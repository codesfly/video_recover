from __future__ import annotations


class VideoRecoverError(Exception):
    """Base exception for stable application errors."""


class InvalidTransition(VideoRecoverError):
    """Raised when a task attempts an invalid state change."""


class UserFacingError(VideoRecoverError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class UnsafeUrl(UserFacingError):
    def __init__(self, message: str = "仅支持安全的抖音 HTTPS 视频链接") -> None:
        super().__init__("unsafe_url", message)


class CookieRequired(UserFacingError):
    def __init__(self, message: str = "抖音要求登录，请在设置中更新 Cookie 后重试") -> None:
        super().__init__("cookie_required", message, retryable=True)


class ParserChanged(UserFacingError):
    def __init__(self, message: str = "抖音页面已变化，当前解析器无法读取视频") -> None:
        super().__init__("parser_changed", message, retryable=True)


class DownloadFailed(UserFacingError):
    def __init__(self, message: str = "视频下载失败，请稍后重试") -> None:
        super().__init__("download_failed", message, retryable=True)


class TranscriptionFailed(UserFacingError):
    def __init__(self, message: str = "视频已保存，但语音转写失败") -> None:
        super().__init__("transcription_failed", message, retryable=True)


class InternalFailure(UserFacingError):
    def __init__(self) -> None:
        super().__init__("internal_failure", "处理失败，请查看脱敏日志后重试", retryable=True)

