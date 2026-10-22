"""Safe public errors and conservative credential filtering."""
import re
from app.config import Settings


class ExecutionError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class ToolFailure(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def check_secrets(settings: Settings, text: str) -> None:
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ''
    patterns = [r'sk-[A-Za-z0-9_-]{12,}', r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
                r'(?i)(?:api[_-]?key|password|secret|access[_-]?token|authorization)\s*[=:]\s*["\x27]?\S{6,}']
    if (key and key in text) or any(re.search(pattern, text) for pattern in patterns):
        raise ExecutionError('sensitive_data', 'Potential credentials detected. Remove secrets before planning or execution.', 422)
