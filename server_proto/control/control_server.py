"""Bearer-authenticated HTTP control API for the active single-player server."""
from __future__ import annotations

import errno
import hmac
import json
import os
import secrets
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from config import CONTROL_HOST, LOG_DIR
from control.contracts import ControlApiError
from game.client_item_of_month import (
    ITEM_OF_MONTH_POPUP_ACTION_CLOSE,
    ITEM_OF_MONTH_POPUP_ACTION_SHOW,
    ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM,
    ITEM_OF_MONTH_POPUP_SOURCE_ICON,
    MAX_ITEM_OF_MONTH_IMAGE_BYTES,
    ItemOfMonthImageError,
    clear_item_of_month_image,
    get_item_of_month_image_metadata,
    normalize_item_of_month_popup_title,
    publish_item_of_month_popup_request,
    store_item_of_month_image,
)
from game.client_toast_bridge import (
    ClientToastBridgeError,
    TOAST_MODE_COMFIRM,
    TOAST_MODE_TOAST,
    client_toast_bridge_enabled,
    send_client_toast,
)
from game.client_video_bridge import (
    ClientVideoBridgeError,
    client_video_bridge_enabled,
    play_client_video,
    stop_client_video,
)


CONTROL_PORT = 17600
MAX_REQUEST_BYTES = 16 * 1024
COMMAND_TIMEOUT_SECONDS = 10.0


class _ControlHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ControlServer:
    """Owns the authenticated API and forwards commands to one GameServer instance."""

    def __init__(
        self,
        game_server: Any,
        *,
        host: str = CONTROL_HOST,
        port: int = CONTROL_PORT,
        token: str | None = None,
        endpoint_host: str | None = None,
        endpoint_path: str | os.PathLike[str] | None = None,
        audit_prefix: str = "Control",
        service_name: str = "usdof-control",
        include_token_in_endpoint: bool = True,
        item_of_month_config_path: str | os.PathLike[str] | None = None,
        item_of_month_image_path: str | os.PathLike[str] | None = None,
        item_of_month_icon_config_path: str | os.PathLike[str] | None = None,
        item_of_month_icon_image_path: str | os.PathLike[str] | None = None,
        item_of_month_popup_request_path: str | os.PathLike[str] | None = None,
        client_video_command_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.game_server = game_server
        self.host = str(host)
        self.port = int(port)
        self.token = str(token or secrets.token_urlsafe(32))
        # The GM gateway connects through loopback even when this listener also
        # accepts connections from other machines.
        self.endpoint_host = str(endpoint_host or self.host)
        self.instance_id = secrets.token_hex(16)
        self.endpoint_path = Path(endpoint_path or (LOG_DIR / "control_endpoint.json"))
        self.audit_prefix = str(audit_prefix)
        self.service_name = str(service_name)
        self.include_token_in_endpoint = bool(include_token_in_endpoint)
        self.item_of_month_config_path = (
            Path(item_of_month_config_path) if item_of_month_config_path else None
        )
        self.item_of_month_image_path = (
            Path(item_of_month_image_path) if item_of_month_image_path else None
        )
        self.item_of_month_icon_config_path = (
            Path(item_of_month_icon_config_path)
            if item_of_month_icon_config_path
            else None
        )
        self.item_of_month_icon_image_path = (
            Path(item_of_month_icon_image_path)
            if item_of_month_icon_image_path
            else None
        )
        self.item_of_month_popup_request_path = (
            Path(item_of_month_popup_request_path)
            if item_of_month_popup_request_path
            else None
        )
        self.client_video_command_path = (
            Path(client_video_command_path) if client_video_command_path else None
        )
        self._httpd: _ControlHttpServer | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stopped = threading.Event()

    def serve_forever(self) -> None:
        handler = self._make_handler()
        httpd = _ControlHttpServer((self.host, self.port), handler)
        httpd.control_service = self  # type: ignore[attr-defined]
        self.port = int(httpd.server_address[1])
        with self._lock:
            self._httpd = httpd
        self._write_endpoint_file()
        self._ready.set()
        self._log("[%s] listening on %s:%s" % (self.audit_prefix, self.host, self.port))
        try:
            httpd.serve_forever(poll_interval=0.2)
        finally:
            try:
                httpd.server_close()
            finally:
                with self._lock:
                    self._httpd = None
                self._remove_endpoint_file()
                self._stopped.set()
                self._log("[%s] stopped" % self.audit_prefix)

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
        else:
            self._remove_endpoint_file()

    def wait_ready(self, timeout: float) -> bool:
        return bool(self._ready.wait(max(0.0, float(timeout))))

    def _log(self, message: str) -> None:
        try:
            self.game_server.log(message)
        except Exception:
            pass

    def _endpoint_payload(self) -> dict[str, Any]:
        payload = {
            "api_version": 1,
            "host": self.endpoint_host,
            "port": self.port,
            "server_pid": os.getpid(),
            "instance_id": self.instance_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if self.endpoint_host != self.host:
            payload["listen_host"] = self.host
        if self.include_token_in_endpoint:
            payload["token"] = self.token
        return payload

    def _write_endpoint_file(self) -> None:
        self.endpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._endpoint_payload(), ensure_ascii=True, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix="control_endpoint_",
            suffix=".json",
            dir=str(self.endpoint_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_name, str(self.endpoint_path))
        finally:
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass

    def _remove_endpoint_file(self) -> None:
        try:
            current = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
            if str(current.get("instance_id") or "") != self.instance_id:
                return
        except FileNotFoundError:
            return
        except Exception:
            return
        try:
            self.endpoint_path.unlink()
        except OSError:
            pass

    def _authorize(self, handler: BaseHTTPRequestHandler) -> None:
        value = str(handler.headers.get("Authorization") or "")
        prefix = "Bearer "
        if not value.startswith(prefix):
            raise ControlApiError("AUTH_FAILED", "missing bearer token", status=401)
        supplied = value[len(prefix) :].strip()
        if not supplied or not hmac.compare_digest(supplied, self.token):
            raise ControlApiError("AUTH_FAILED", "invalid bearer token", status=401)

    @staticmethod
    def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        raw_length = str(handler.headers.get("Content-Length") or "").strip()
        try:
            length = int(raw_length, 10)
        except ValueError:
            raise ControlApiError("INVALID_PARAMS", "Content-Length is required", status=400)
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ControlApiError("INVALID_PARAMS", "request body size is invalid", status=400)
        try:
            payload = json.loads(handler.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlApiError("INVALID_PARAMS", "request body must be UTF-8 JSON", status=400)
        if not isinstance(payload, dict):
            raise ControlApiError("INVALID_PARAMS", "request body must be an object", status=400)
        return payload

    def _handle_health(self) -> tuple[int, dict[str, Any]]:
        return 200, {
            "ok": True,
            "api_version": 1,
            "service": self.service_name,
            "target_guard": getattr(self.game_server, "_manager_target_guard", False) is True,
        }

    def _handle_session(self) -> tuple[int, dict[str, Any]]:
        return 200, {"ok": True, "session": self.game_server.get_control_session_snapshot()}

    def _handle_command(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if "target" in payload:
            raise ControlApiError(
                "INVALID_PARAMS",
                "single-player control does not accept a target field",
                status=400,
            )
        request_id = str(payload.get("request_id") or "").strip()
        command = str(payload.get("command") or "").strip()
        params = payload.get("params", {})
        if not request_id or len(request_id) > 128:
            raise ControlApiError("INVALID_PARAMS", "request_id is required", status=400)
        if not command or len(command) > 128:
            raise ControlApiError("INVALID_PARAMS", "command is required", status=400)
        if not isinstance(params, dict):
            raise ControlApiError("INVALID_PARAMS", "params must be an object", status=400)
        if any(not isinstance(key, str) or key.startswith("_manager_") for key in params):
            raise ControlApiError("INVALID_PARAMS", "reserved management parameter", status=400)
        has_character = "expected_character_id" in payload
        has_epoch = "expected_session_epoch" in payload
        if has_character != has_epoch:
            raise ControlApiError("INVALID_PARAMS", "expected target fields must be supplied together", status=400)
        params = dict(params)
        if has_character:
            character_id = payload["expected_character_id"]
            session_epoch = payload["expected_session_epoch"]
            if type(character_id) is not int or character_id <= 0 or type(session_epoch) is not int or session_epoch <= 0:
                raise ControlApiError("INVALID_PARAMS", "expected target fields must be positive integers", status=400)
            if getattr(self.game_server, "_manager_target_guard", False) is not True:
                raise ControlApiError("TARGET_GUARD_UNAVAILABLE", "target guard is not installed", status=503)
            params["_manager_expected_character"] = character_id
            params["_manager_expected_epoch"] = session_epoch
        result = self.game_server.submit_control_command(
            request_id=request_id,
            command=command,
            params=dict(params),
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        return 200, {
            "ok": True,
            "request_id": request_id,
            "result": result,
            "session_epoch": self.game_server.get_control_session_snapshot().get("session_epoch"),
        }

    @staticmethod
    def _image_error(exc: ItemOfMonthImageError) -> ControlApiError:
        return ControlApiError(exc.code, exc.message, status=exc.status)

    def _handle_item_of_month_image_get(self) -> tuple[int, dict[str, Any]]:
        try:
            metadata = get_item_of_month_image_metadata(
                config_path=self.item_of_month_config_path,
                image_path=self.item_of_month_image_path,
            )
        except ItemOfMonthImageError as exc:
            raise self._image_error(exc) from exc
        return 200, {
            "ok": True,
            "image": metadata.as_dict() if metadata else {"configured": False},
        }

    def _handle_item_of_month_image_put(
        self,
        payload: bytes,
        content_type: str,
    ) -> tuple[int, dict[str, Any]]:
        try:
            metadata = store_item_of_month_image(
                payload,
                content_type,
                config_path=self.item_of_month_config_path,
                image_path=self.item_of_month_image_path,
            )
        except ItemOfMonthImageError as exc:
            raise self._image_error(exc) from exc
        return 200, {"ok": True, "image": metadata.as_dict()}

    def _handle_item_of_month_image_delete(self) -> tuple[int, dict[str, Any]]:
        try:
            removed = clear_item_of_month_image(
                config_path=self.item_of_month_config_path,
                image_path=self.item_of_month_image_path,
            )
        except ItemOfMonthImageError as exc:
            raise self._image_error(exc) from exc
        return 200, {
            "ok": True,
            "removed": bool(removed),
            "image": {"configured": False},
        }

    def _handle_item_of_month_popup_post(
        self,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        action = str(body.get("action") or "").strip().lower()
        expected_keys = (
            {"action", "source"}
            if action == ITEM_OF_MONTH_POPUP_ACTION_SHOW and "source" in body
            else {"action"}
        )
        if set(body) != expected_keys:
            raise ControlApiError(
                "INVALID_PARAMS",
                "popup show accepts action and optional source; close accepts only action",
                status=400,
            )
        source = str(
            body.get("source", ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM) or ""
        ).strip().lower()
        if action == ITEM_OF_MONTH_POPUP_ACTION_SHOW:
            if source not in {
                ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM,
                ITEM_OF_MONTH_POPUP_SOURCE_ICON,
            }:
                raise ControlApiError(
                    "INVALID_PARAMS",
                    "popup source must be custom or icon",
                    status=400,
                )
        elif action == ITEM_OF_MONTH_POPUP_ACTION_CLOSE:
            source = ""
        snapshot = self.game_server.get_control_session_snapshot()
        if not bool(snapshot.get("connected")):
            raise ControlApiError(
                "NO_GAME_CLIENT",
                "no active Game client",
                status=503,
            )
        try:
            request = publish_item_of_month_popup_request(
                action,
                source=source,
                request_path=self.item_of_month_popup_request_path,
                config_path=self.item_of_month_config_path,
                image_path=self.item_of_month_image_path,
                icon_config_path=self.item_of_month_icon_config_path,
                icon_image_path=self.item_of_month_icon_image_path,
            )
        except ItemOfMonthImageError as exc:
            raise self._image_error(exc) from exc
        return 200, {"ok": True, "popup_request": request.as_dict()}

    def _handle_client_notification_post(
        self,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        notification_type = str(body.get("type") or "").strip().lower()
        if notification_type not in {"toast", "confirm", "img"}:
            raise ControlApiError(
                "INVALID_PARAMS",
                "notification type must be toast, confirm, or img",
                status=400,
            )
        expected_keys = (
            {"type", "title", "content"}
            if notification_type == "img"
            else {"type", "content"}
        )
        if set(body) != expected_keys:
            raise ControlApiError(
                "INVALID_PARAMS",
                "img requires type, title, and content; text types require type and content",
                status=400,
            )
        content = body.get("content")
        if (
            not isinstance(content, str)
            or not content
            or len(content) > 4096
            or "\x00" in content
        ):
            raise ControlApiError(
                "INVALID_PARAMS",
                "notification content must be 1..4096 characters without NUL",
                status=400,
            )
        snapshot = self.game_server.get_control_session_snapshot()
        if not bool(snapshot.get("connected")):
            raise ControlApiError(
                "NO_GAME_CLIENT",
                "no active Game client",
                status=503,
            )

        if notification_type in {"toast", "confirm"}:
            if not client_toast_bridge_enabled():
                raise ControlApiError(
                    "CLIENT_NOTIFICATION_UNAVAILABLE",
                    "client notification bridge is disabled",
                    status=503,
                )
            mode = (
                TOAST_MODE_TOAST
                if notification_type == "toast"
                else TOAST_MODE_COMFIRM
            )
            try:
                request = send_client_toast(content, mode=mode)
            except ClientToastBridgeError as exc:
                raise ControlApiError(
                    "INVALID_PARAMS",
                    str(exc),
                    status=400,
                ) from exc
            except OSError as exc:
                raise ControlApiError(
                    "CLIENT_NOTIFICATION_FAILED",
                    "client notification request could not be published",
                    status=500,
                ) from exc
            return 200, {
                "ok": True,
                "notification": {
                    "type": notification_type,
                    "request_id": int(request.request_id),
                    "created_unix_ms": int(request.created_unix_ms),
                },
            }

        try:
            popup_title = normalize_item_of_month_popup_title(body.get("title"))
        except ItemOfMonthImageError as exc:
            raise self._image_error(exc) from exc
        source = Path(content).expanduser()
        if not source.is_absolute():
            raise ControlApiError(
                "INVALID_IMAGE_PATH",
                "img content must be an absolute local image path",
                status=400,
            )
        suffix = source.suffix.lower()
        if suffix == ".png":
            content_type = "image/png"
        elif suffix in {".jpg", ".jpeg"}:
            content_type = "image/jpeg"
        else:
            raise ControlApiError(
                "INVALID_IMAGE_PATH",
                "img path must end in .png, .jpg, or .jpeg",
                status=400,
            )
        try:
            image_size = int(source.stat().st_size)
            if image_size > MAX_ITEM_OF_MONTH_IMAGE_BYTES:
                raise ControlApiError(
                    "IMAGE_TOO_LARGE",
                    "image body exceeds 5 MiB",
                    status=413,
                )
            image_payload = source.read_bytes()
        except ControlApiError:
            raise
        except OSError as exc:
            raise ControlApiError(
                "INVALID_IMAGE_PATH",
                "img path is not a readable local file",
                status=400,
            ) from exc
        try:
            metadata = store_item_of_month_image(
                image_payload,
                content_type,
                config_path=self.item_of_month_config_path,
                image_path=self.item_of_month_image_path,
            )
            popup_request = publish_item_of_month_popup_request(
                "show",
                title=popup_title,
                request_path=self.item_of_month_popup_request_path,
                config_path=self.item_of_month_config_path,
                image_path=self.item_of_month_image_path,
            )
        except ItemOfMonthImageError as exc:
            raise self._image_error(exc) from exc
        return 200, {
            "ok": True,
            "notification": {
                "type": "img",
                "title": popup_request.title,
                "image": metadata.as_dict(),
                "popup_request": popup_request.as_dict(),
            },
        }

    def _handle_client_video_post(
        self,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        action = str(body.get("action") or "").strip().lower()
        expected_keys = {"action", "path"} if action == "play" else {"action"}
        if action not in {"play", "stop"} or set(body) != expected_keys:
            raise ControlApiError(
                "INVALID_PARAMS",
                "video play requires action and path; stop requires only action",
                status=400,
            )
        snapshot = self.game_server.get_control_session_snapshot()
        if not bool(snapshot.get("connected")):
            raise ControlApiError(
                "NO_GAME_CLIENT",
                "no active Game client",
                status=503,
            )
        if not client_video_bridge_enabled():
            raise ControlApiError(
                "CLIENT_VIDEO_UNAVAILABLE",
                "client video extension is disabled",
                status=503,
            )
        try:
            if action == "play":
                command = play_client_video(
                    body.get("path"),
                    command_path=self.client_video_command_path,
                )
            else:
                command = stop_client_video(
                    command_path=self.client_video_command_path,
                )
        except ClientVideoBridgeError as exc:
            raise ControlApiError(
                "INVALID_VIDEO",
                str(exc),
                status=400,
            ) from exc
        except OSError as exc:
            raise ControlApiError(
                "CLIENT_VIDEO_FAILED",
                "client video command could not be published",
                status=500,
            ) from exc
        return 200, {"ok": True, "video_command": command.as_dict()}

    def _audit(self, *, command: str, request_id: str, status: int, code: str) -> None:
        self._log(
            "[%s] request_id=%s command=%s status=%s code=%s"
            % (self.audit_prefix, request_id or "-", command or "-", status, code)
        )

    def _write_json(self, handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        raw = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            handler.send_response(int(status))
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(raw)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(raw)
        except OSError as exc:
            disconnect_codes = {
                errno.EPIPE, errno.ECONNABORTED, errno.ECONNRESET,
                10053, 10054, 10058,
            }
            if (not isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError))
                    and exc.errno not in disconnect_codes
                    and getattr(exc, "winerror", None) not in disconnect_codes):
                raise
            # The command has already finished and been audited. A lost reply
            # must not turn into a second response or a second execution.
            handler.close_connection = True
            self._log(
                "[%s] response disconnected method=%s status=%d request_id=%s errno=%s"
                % (self.audit_prefix, handler.command, status,
                   payload.get("request_id") or "-",
                   getattr(exc, "winerror", None) or exc.errno)
            )

    def _make_handler(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_GET(self) -> None:
                service: ControlServer = self.server.control_service  # type: ignore[attr-defined]
                try:
                    if self.path == "/api/v1/health":
                        status, payload = service._handle_health()
                    elif self.path == "/api/v1/session":
                        service._authorize(self)
                        status, payload = service._handle_session()
                    elif self.path == "/api/v1/item-of-month-image":
                        service._authorize(self)
                        status, payload = service._handle_item_of_month_image_get()
                    else:
                        raise ControlApiError("NOT_FOUND", "unknown endpoint", status=404)
                except ControlApiError as exc:
                    status, payload = exc.status, {
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                except Exception as exc:
                    service._log("[%s] GET internal error: %r" % (service.audit_prefix, exc))
                    status, payload = 500, {
                        "ok": False,
                        "error": {"code": "INTERNAL_ERROR", "message": "control request failed"},
                    }
                service._write_json(self, status, payload)

            def do_PUT(self) -> None:
                service: ControlServer = self.server.control_service  # type: ignore[attr-defined]
                try:
                    if self.path != "/api/v1/item-of-month-image":
                        raise ControlApiError("NOT_FOUND", "unknown endpoint", status=404)
                    service._authorize(self)
                    content_type = str(self.headers.get("Content-Type") or "")
                    raw_length = str(self.headers.get("Content-Length") or "").strip()
                    try:
                        length = int(raw_length, 10)
                    except ValueError:
                        raise ControlApiError(
                            "INVALID_IMAGE",
                            "Content-Length is required",
                            status=400,
                        )
                    if length <= 0:
                        raise ControlApiError(
                            "INVALID_IMAGE",
                            "image body is empty",
                            status=400,
                        )
                    if length > MAX_ITEM_OF_MONTH_IMAGE_BYTES:
                        raise ControlApiError(
                            "IMAGE_TOO_LARGE",
                            "image body exceeds 5 MiB",
                            status=413,
                        )
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ControlApiError(
                            "INVALID_IMAGE",
                            "image body is incomplete",
                            status=400,
                        )
                    status, payload = service._handle_item_of_month_image_put(
                        body,
                        content_type,
                    )
                    service._audit(
                        command="item_of_month.image.put",
                        request_id="-",
                        status=status,
                        code="OK",
                    )
                except ControlApiError as exc:
                    status, payload = exc.status, {
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                    service._audit(
                        command="item_of_month.image.put",
                        request_id="-",
                        status=status,
                        code=exc.code,
                    )
                except Exception as exc:
                    service._log("[%s] PUT internal error: %r" % (service.audit_prefix, exc))
                    status, payload = 500, {
                        "ok": False,
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "control request failed",
                        },
                    }
                    service._audit(
                        command="item_of_month.image.put",
                        request_id="-",
                        status=status,
                        code="INTERNAL_ERROR",
                    )
                service._write_json(self, status, payload)

            def do_DELETE(self) -> None:
                service: ControlServer = self.server.control_service  # type: ignore[attr-defined]
                try:
                    if self.path != "/api/v1/item-of-month-image":
                        raise ControlApiError("NOT_FOUND", "unknown endpoint", status=404)
                    service._authorize(self)
                    status, payload = service._handle_item_of_month_image_delete()
                    service._audit(
                        command="item_of_month.image.delete",
                        request_id="-",
                        status=status,
                        code="OK",
                    )
                except ControlApiError as exc:
                    status, payload = exc.status, {
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                    service._audit(
                        command="item_of_month.image.delete",
                        request_id="-",
                        status=status,
                        code=exc.code,
                    )
                except Exception as exc:
                    service._log(
                        "[%s] DELETE internal error: %r" % (service.audit_prefix, exc)
                    )
                    status, payload = 500, {
                        "ok": False,
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": "control request failed",
                        },
                    }
                    service._audit(
                        command="item_of_month.image.delete",
                        request_id="-",
                        status=status,
                        code="INTERNAL_ERROR",
                    )
                service._write_json(self, status, payload)

            def do_POST(self) -> None:
                service: ControlServer = self.server.control_service  # type: ignore[attr-defined]
                request_id = ""
                command = ""
                try:
                    launcher_path = self.path.startswith("/api/v1/launcher/")
                    if not launcher_path and self.path not in {
                        "/api/v1/command",
                        "/api/v1/client-notification",
                        "/api/v1/client-video",
                        "/api/v1/item-of-month-popup",
                    }:
                        raise ControlApiError("NOT_FOUND", "unknown endpoint", status=404)
                    service._authorize(self)
                    body = service._read_json(self)
                    if launcher_path:
                        # A player token is never accepted here: _authorize above
                        # requires the server-owned control token. Also require
                        # loopback even when the legacy admin listener is public.
                        import ipaddress
                        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                            raise ControlApiError("FORBIDDEN", "launcher adapter is loopback only", status=403)
                        adapter = getattr(service.game_server, "_launcher_service", None)
                        if adapter is None:
                            raise ControlApiError("SERVER_NOT_READY", "game launcher adapter unavailable", status=503)
                        from login.game_sessions import LoginApiError
                        command = "launcher." + self.path[len("/api/v1/launcher/"):]
                        try:
                            result = adapter.dispatch(command[len("launcher."):], body)
                        except LoginApiError as exc:
                            raise ControlApiError(exc.code, exc.message, status=exc.status) from None
                        status, payload = 200, {"ok": True, "data": result}
                    elif self.path == "/api/v1/item-of-month-popup":
                        action = str(body.get("action") or "").strip().lower()
                        command = "item_of_month.popup.%s" % (action or "invalid")
                        status, payload = service._handle_item_of_month_popup_post(body)
                    elif self.path == "/api/v1/client-notification":
                        notification_type = str(body.get("type") or "").strip().lower()
                        command = "client_notification.%s" % (
                            notification_type or "invalid"
                        )
                        status, payload = service._handle_client_notification_post(body)
                    elif self.path == "/api/v1/client-video":
                        action = str(body.get("action") or "").strip().lower()
                        command = "client_video.%s" % (action or "invalid")
                        status, payload = service._handle_client_video_post(body)
                    else:
                        request_id = str(body.get("request_id") or "")
                        command = str(body.get("command") or "")
                        status, payload = service._handle_command(body)
                    service._audit(command=command, request_id=request_id, status=status, code="OK")
                except ControlApiError as exc:
                    status, payload = exc.status, {
                        "ok": False,
                        "request_id": request_id or None,
                        "error": {"code": exc.code, "message": exc.message},
                    }
                    service._audit(command=command, request_id=request_id, status=status, code=exc.code)
                except Exception as exc:
                    service._log("[%s] POST internal error: %r" % (service.audit_prefix, exc))
                    status, payload = 500, {
                        "ok": False,
                        "request_id": request_id or None,
                        "error": {"code": "INTERNAL_ERROR", "message": "control request failed"},
                    }
                    service._audit(command=command, request_id=request_id, status=status, code="INTERNAL_ERROR")
                service._write_json(self, status, payload)

        return Handler


__all__ = [
    "CONTROL_HOST",
    "CONTROL_PORT",
    "ControlServer",
]
