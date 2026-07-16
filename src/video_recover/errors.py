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


class DownloadTooLarge(UserFacingError):
    def __init__(self) -> None:
        super().__init__("download_too_large", "视频超过允许的最大文件大小")


class InsufficientStorage(UserFacingError):
    def __init__(self) -> None:
        super().__init__("insufficient_storage", "本地磁盘空间不足，请清理空间后重试")


class UnsafeMediaUrl(UserFacingError):
    def __init__(self) -> None:
        super().__init__("unsafe_media_url", "解析器返回了不受信任的视频地址")


class UnsafeCapture(UserFacingError):
    def __init__(self) -> None:
        super().__init__("unsafe_capture", "仅允许导入本机 data/browser-capture 中的有效视频")


class CaptureConflict(UserFacingError):
    def __init__(self) -> None:
        super().__init__("capture_conflict", "该视频任务正在处理或已经完成，不能重复导入")


class TranscriptionFailed(UserFacingError):
    def __init__(self, message: str = "视频已保存，但语音转写失败") -> None:
        super().__init__("transcription_failed", message, retryable=True)


class InternalFailure(UserFacingError):
    def __init__(self) -> None:
        super().__init__("internal_failure", "处理失败，请查看脱敏日志后重试", retryable=True)
