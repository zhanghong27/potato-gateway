from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


LOCAL_PATH = re.compile(
    r"(?:/Users/|/home/|/private/|/tmp/|/var/|/etc/|~/|[A-Za-z]:\\)[^\s\n\r\"']*"
)
SENSITIVE_KEYS = {
    "file_path",
    "local_path",
    "probe_path",
    "keyframe_paths",
    "contact_sheet_path",
    "audio_path",
    "subtitle_path",
    "token",
    "authorization",
    "prompt_content",
}


class HubClientError(Exception):
    pass


class HubNotFoundError(HubClientError):
    pass


class HubConflictError(HubClientError):
    pass


class HubUnavailableError(HubClientError):
    pass


class HubClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/api/health", sanitize=True)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        sanitize: bool = True,
    ) -> dict[str, Any]:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
            request_headers["X-Potato-Hub-Token"] = self.token
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise HubNotFoundError(path) from None
            if exc.code in {400, 409, 422}:
                detail = self._error_detail(exc)
                raise HubConflictError(detail) from None
            raise HubUnavailableError(f"Potato Hub returned HTTP {exc.code}") from None
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise HubUnavailableError("Potato Hub is unavailable") from None
        if not isinstance(result, dict):
            raise HubUnavailableError("Potato Hub returned invalid data")
        return sanitize_hub_payload(result) if sanitize else result

    def request_bytes(self, path: str) -> tuple[bytes, str, str]:
        headers = {"Accept": "*/*"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-Potato-Hub-Token"] = self.token
        request = urllib.request.Request(f"{self.base_url}{path}", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return (
                    response.read(),
                    response.headers.get_content_type() or "application/octet-stream",
                    response.headers.get_filename() or "evidence",
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise HubNotFoundError(path) from None
            raise HubUnavailableError(f"Potato Hub returned HTTP {exc.code}") from None
        except (OSError, urllib.error.URLError, TimeoutError):
            raise HubUnavailableError("Potato Hub is unavailable") from None

    @staticmethod
    def _error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "Potato Hub rejected the request"
        return str(payload.get("error") or payload.get("detail") or "Potato Hub rejected the request")[:500]


def sanitize_hub_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitize_hub_payload(item)
            for key, item in value.items()
            if key.lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_hub_payload(item) for item in value]
    if isinstance(value, str):
        return LOCAL_PATH.sub("[local path hidden]", value)
    return value
