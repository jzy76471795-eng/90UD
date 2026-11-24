from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class LauncherApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


def launcher_base_url(server_ipv4: str, port: int = 17602) -> str:
    host = str(ipaddress.IPv4Address(str(server_ipv4).strip()))
    value = int(port)
    if not 1 <= value <= 65535:
        raise ValueError("port must be in 1..65535")
    return "http://%s:%s" % (host, value)


def normalize_base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme != "http":
        raise ValueError("launcher API URL must use http")
    if parsed.username or parsed.password:
        raise ValueError("launcher API URL must not contain user information")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("launcher API URL must contain only IPv4 and port")
    host = str(ipaddress.IPv4Address(parsed.hostname or ""))
    try:
        port = parsed.port or 17602
    except ValueError as exc:
        raise ValueError("launcher API port is invalid") from exc
    return launcher_base_url(host, port)


class LauncherApi:
    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self.base_url = normalize_base_url(base_url)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.access_token = ""

    def _request(
        self,
        method: str,
        path: str,
        document: dict[str, Any] | None = None,
        *,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        payload = None
        headers = {"Accept": "application/json"}
        if document is not None:
            payload = json.dumps(
                document, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if authenticated:
            if not self.access_token:
                raise LauncherApiError(401, "AUTH_FAILED", "login is required")
            headers["Authorization"] = "Bearer " + self.access_token
        request = Request(
            self.base_url + path,
            data=payload,
            headers=headers,
            method=str(method).upper(),
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw.decode("utf-8"))
                error = dict(body.get("error") or {})
            except Exception:
                error = {}
            raise LauncherApiError(
                int(exc.code),
                str(error.get("code") or "HTTP_ERROR"),
                str(error.get("message") or exc.reason),
            ) from exc
        except (URLError, OSError) as exc:
            raise LauncherApiError(0, "NETWORK_ERROR", str(exc)) from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise LauncherApiError(
                0, "INVALID_RESPONSE", "server response is not UTF-8 JSON"
            ) from exc
        if not isinstance(result, dict):
            raise LauncherApiError(
                0, "INVALID_RESPONSE", "server response must be a JSON object"
            )
        return result

    def meta(self) -> dict[str, Any]:
        return self._request("GET", "/launcher/v1/meta")

    def login(self, account: str, password: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/launcher/v1/sessions",
            {"account": str(account), "password": str(password)},
        )
        token = str(response.get("access_token") or "")
        if not token:
            raise LauncherApiError(
                0, "INVALID_RESPONSE", "login response has no access_token"
            )
        self.access_token = token
        return response

    def current_session(self) -> dict[str, Any]:
        return self._request(
            "GET", "/launcher/v1/sessions/current", authenticated=True
        )

    def logout(self) -> dict[str, Any]:
        try:
            return self._request(
                "DELETE", "/launcher/v1/sessions/current", authenticated=True
            )
        finally:
            self.access_token = ""


__all__ = [
    "LauncherApi",
    "LauncherApiError",
    "launcher_base_url",
    "normalize_base_url",
]
