"""利用者向け文字列から認証情報を除去する。"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
HEADER_SECRET = re.compile(
    r"(?i)\b(authorization|api-key|x-api-key)\b(\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)"
)
URL = re.compile(r"https?://[^\s<>\"']+")
SENSITIVE_QUERY = re.compile(r"(?i)(?:token|key|secret)")


class SecretSanitizer:
    """既知の秘密と一般的な認証表現を決定的に置換する。"""

    def __init__(self, secrets: list[str] | tuple[str, ...] | set[str] = ()) -> None:
        self._secrets = tuple(sorted({value for value in secrets if value}, key=len, reverse=True))

    def sanitize(self, value: str, *, limit: int | None = None) -> str:
        text = value
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = HEADER_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
        text = URL.sub(lambda match: _sanitize_url(match.group(0)), text)
        if limit is not None and len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
        netloc = hostname
        if parsed.username is not None or parsed.password is not None:
            netloc = f"{REDACTED}@{hostname}"
        query = urlencode(
            [
                (name, REDACTED if SENSITIVE_QUERY.search(name) else item)
                for name, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        return value
