"""Standard-library SDK for the loopback control API."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union


SERVER_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ENDPOINT_PATH = SERVER_DIR / "log" / "control_endpoint.json"
LOOPBACK_HOST = "127.0.0.1"


class ControlEndpointError(RuntimeError):
    """The endpoint is absent, malformed, or unavailable."""


class ControlRequestError(RuntimeError):
    """A structured error returned by a control HTTP endpoint."""

    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def read_json_response(
    raw: bytes,
    status: int,
    *,
    allow_invalid: bool = False,
) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        if allow_invalid:
            return {}
        raise ControlRequestError(
            "INVALID_RESPONSE",
            "control response is not UTF-8 JSON",
            status,
        ) from exc
    if not isinstance(value, dict):
        if allow_invalid:
            return {}
        raise ControlRequestError(
            "INVALID_RESPONSE",
            "control response must be an object",
            status,
        )
    return value


class LocalControlApi:
    """Client for one running loopback control-server instance."""

    def __init__(
        self,
        endpoint_path: Path = DEFAULT_ENDPOINT_PATH,
        *,
        timeout_seconds: float = 12.0,
    ) -> None:
        self.endpoint_path = Path(endpoint_path)
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/health", authenticated=False)

    def session(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v1/session", authenticated=True)

    def get_item_of_month_image(self) -> Dict[str, Any]:
        response = self._request(
            "GET",
            "/api/v1/item-of-month-image",
            authenticated=True,
        )
        return dict(response.get("image") or {})

    def put_item_of_month_image(
        self,
        image: bytes,
        content_type: str,
    ) -> Dict[str, Any]:
        response = self._request(
            "PUT",
            "/api/v1/item-of-month-image",
            authenticated=True,
            raw_payload=bytes(image),
            content_type=str(content_type),
        )
        return dict(response.get("image") or {})

    def upload_item_of_month_image(self, path: Path) -> Dict[str, Any]:
        source = Path(path)
        suffix = source.suffix.lower()
        if suffix == ".png":
            content_type = "image/png"
        elif suffix in {".jpg", ".jpeg"}:
            content_type = "image/jpeg"
        else:
            raise ValueError("image path must end in .png, .jpg, or .jpeg")
        return self.put_item_of_month_image(source.read_bytes(), content_type)

    def delete_item_of_month_image(self) -> Dict[str, Any]:
        return self._request(
            "DELETE",
            "/api/v1/item-of-month-image",
            authenticated=True,
        )

    def set_item_of_month_popup(
        self,
        action: str,
        *,
        source: str = "custom",
    ) -> Dict[str, Any]:
        value = str(action or "").strip().lower()
        if value not in {"show", "close"}:
            raise ValueError("popup action must be show or close")
        source_value = str(source or "").strip().lower()
        if value == "show" and source_value not in {"custom", "icon"}:
            raise ValueError("popup source must be custom or icon")
        if value == "close" and source_value not in {"", "custom"}:
            raise ValueError("close popup does not accept a source")
        payload = {"action": value}
        if value == "show" and source_value != "custom":
            payload["source"] = source_value
        response = self._request(
            "POST",
            "/api/v1/item-of-month-popup",
            authenticated=True,
            payload=payload,
        )
        return dict(response.get("popup_request") or {})

    def show_item_of_month_popup(
        self,
        *,
        source: str = "custom",
    ) -> Dict[str, Any]:
        return self.set_item_of_month_popup("show", source=source)

    def close_item_of_month_popup(self) -> Dict[str, Any]:
        return self.set_item_of_month_popup("close")

    def send_client_notification(
        self,
        notification_type: str,
        content: Union[str, Path],
        *,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        value = str(notification_type or "").strip().lower()
        if value not in {"toast", "confirm", "img"}:
            raise ValueError("notification type must be toast, confirm, or img")
        content_value = str(content)
        payload = {"type": value, "content": content_value}
        if value == "img":
            title_value = str(title or "").strip()
            if not title_value:
                raise ValueError("img notification title is required")
            source = Path(content).expanduser()
            if not source.is_absolute():
                raise ValueError("img content must be an absolute local image path")
            content_value = str(source)
            payload = {
                "type": value,
                "title": title_value,
                "content": content_value,
            }
        elif title is not None:
            raise ValueError("toast and confirm notifications do not accept title")
        response = self._request(
            "POST",
            "/api/v1/client-notification",
            authenticated=True,
            payload=payload,
        )
        return dict(response.get("notification") or {})

    def play_client_video(self, path: Union[str, Path]) -> Dict[str, Any]:
        source = Path(path).expanduser()
        if not source.is_absolute():
            raise ValueError("video path must be absolute")
        response = self._request(
            "POST",
            "/api/v1/client-video",
            authenticated=True,
            payload={"action": "play", "path": str(source)},
        )
        return dict(response.get("video_command") or {})

    def stop_client_video(self) -> Dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/client-video",
            authenticated=True,
            payload={"action": "stop"},
        )
        return dict(response.get("video_command") or {})

    def command(
        self,
        command: str,
        params: Dict[str, Any],
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        command_text = str(command or "").strip()
        if not command_text:
            raise ValueError("command is required")
        if not isinstance(params, dict):
            raise TypeError("params must be a dict")
        identifier = str(request_id or uuid.uuid4().hex).strip()
        if not identifier:
            raise ValueError("request_id is required")
        response = self._request(
            "POST",
            "/api/v1/command",
            authenticated=True,
            payload={
                "request_id": identifier,
                "command": command_text,
                "params": dict(params),
            },
        )
        return dict(response.get("result") or {})

    def kill_all_monsters(self, *, request_id: Optional[str] = None) -> Dict[str, Any]:
        return self.command(
            "dungeon.kill_all_monsters",
            {},
            request_id=request_id,
        )

    def move_adjacent(
        self,
        direction: str,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        value = str(direction or "").strip().lower()
        if value not in ("left", "right", "up", "down"):
            raise ValueError("direction must be left, right, up, or down")
        return self.command(
            "dungeon.move_adjacent",
            {"direction": value},
            request_id=request_id,
        )

    def list_pvf_items(
        self,
        *,
        search: str = "",
        category: str = "all",
        page: int = 1,
        page_size: int = 50,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "pvf.list_items",
            {
                "search": str(search or ""),
                "category": str(category or "all"),
                "page": int(page),
                "page_size": int(page_size),
            },
            request_id=request_id,
        )

    def grant_item(
        self,
        item_id: int,
        count: int = 1,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "inventory.grant_item",
            {"item_id": int(item_id), "count": int(count)},
            request_id=request_id,
        )

    def list_inventory(
        self,
        section: str = "all",
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "inventory.list",
            {"section": str(section or "all")},
            request_id=request_id,
        )

    def add_inventory_item(
        self,
        item_id: int,
        count: int,
        section: str,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "inventory.add_item",
            {
                "item_id": int(item_id),
                "count": int(count),
                "section": str(section or ""),
            },
            request_id=request_id,
        )

    def delete_inventory_item(
        self,
        item_id: int,
        list_type: int,
        slot_index: int,
        section: str,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "inventory.delete_item",
            {
                "item_id": int(item_id),
                "list_type": int(list_type),
                "slot_index": int(slot_index),
                "section": str(section or ""),
            },
            request_id=request_id,
        )

    def clear_inventory_scope(
        self,
        scope: str,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Backup and clear one GM repair scope, returning the backup path."""
        return self.command(
            "inventory.clear_scope",
            {"scope": str(scope or "")},
            request_id=request_id,
        )

    def add_cera(
        self,
        amount: int,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "currency.add_cera",
            {"amount": int(amount)},
            request_id=request_id,
        )

    def add_gold(
        self,
        amount: int,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "currency.add_gold",
            {"amount": int(amount)},
            request_id=request_id,
        )

    def list_loadouts(
        self,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command("loadout.list", {}, request_id=request_id)

    def save_loadout(
        self,
        index: int,
        name: str,
        items: Dict[str, Any],
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "loadout.save",
            {"index": int(index), "name": str(name), "items": dict(items)},
            request_id=request_id,
        )

    def read_current_loadout(
        self,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command("loadout.read_current", {}, request_id=request_id)

    def apply_loadout(
        self,
        index: int,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "loadout.apply",
            {"index": int(index)},
            request_id=request_id,
        )

    def list_professions(
        self,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command("character.list_professions", {}, request_id=request_id)

    def change_job(
        self,
        job_id: int,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "character.change_job",
            {"job_id": int(job_id)},
            request_id=request_id,
        )

    def change_profession(
        self,
        chain_type: int,
        grow_number: int,
        *,
        job_id: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "chain_type": int(chain_type),
            "grow_number": int(grow_number),
        }
        if job_id is not None:
            params["job_id"] = int(job_id)
        return self.command("character.change_profession", params, request_id=request_id)

    def set_character_level(
        self,
        level: int,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "character.set_level",
            {"level": int(level)},
            request_id=request_id,
        )

    def list_accepted_quests(
        self,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command("quest.list_accepted", {}, request_id=request_id)

    def list_quests(
        self,
        search: str = "",
        *,
        status: str = "all",
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"search": str(search or "")}
        if str(status or "all") != "all":
            params["status"] = str(status)
        if page is not None:
            params["page"] = int(page)
        if page_size is not None:
            params["page_size"] = int(page_size)
        return self.command(
            "quest.list",
            params,
            request_id=request_id,
        )

    def accept_unfinished_quest(
        self,
        quest_id: int,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "quest.accept_unfinished",
            {"quest_id": int(quest_id)},
            request_id=request_id,
        )

    def complete_accepted_quest(
        self,
        quest_id: int,
        *,
        reward_selection_index: int = 0,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "quest.complete_accepted",
            {
                "quest_id": int(quest_id),
                "reward_selection_index": int(reward_selection_index),
            },
            request_id=request_id,
        )

    def giveup_accepted_quest(
        self,
        quest_id: int,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "quest.giveup_accepted",
            {"quest_id": int(quest_id)},
            request_id=request_id,
        )

    def reaccept_quest(
        self,
        quest_id: int,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "quest.reaccept",
            {"quest_id": int(quest_id)},
            request_id=request_id,
        )

    @staticmethod
    def _content_block_ids(values: Iterable[int]) -> list[int]:
        return [int(value) for value in values]

    def list_content_block_rules(
        self,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command("content_block.list", {}, request_id=request_id)

    def resolve_content_block_ids(
        self,
        *,
        quest_ids: Iterable[int] = (),
        item_ids: Iterable[int] = (),
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "content_block.resolve",
            {
                "quest_ids": self._content_block_ids(quest_ids),
                "item_ids": self._content_block_ids(item_ids),
            },
            request_id=request_id,
        )

    def list_content_block_candidates(
        self,
        *,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command("content_block.candidates", {}, request_id=request_id)

    def apply_content_block_rules(
        self,
        *,
        quest_ids: Iterable[int] = (),
        item_ids: Iterable[int] = (),
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "content_block.apply",
            {
                "quest_ids": self._content_block_ids(quest_ids),
                "item_ids": self._content_block_ids(item_ids),
            },
            request_id=request_id,
        )

    def remove_content_block_rules(
        self,
        *,
        quest_ids: Iterable[int] = (),
        item_ids: Iterable[int] = (),
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.command(
            "content_block.remove",
            {
                "quest_ids": self._content_block_ids(quest_ids),
                "item_ids": self._content_block_ids(item_ids),
            },
            request_id=request_id,
        )

    def _endpoint(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ControlEndpointError("control endpoint file is not available")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlEndpointError("control endpoint file is invalid: %s" % exc)
        if not isinstance(value, dict):
            raise ControlEndpointError("control endpoint must be a JSON object")
        if int(value.get("api_version") or 0) != 1:
            raise ControlEndpointError("control endpoint version is unsupported")
        if str(value.get("host") or "") != LOOPBACK_HOST:
            raise ControlEndpointError("control endpoint is not bound to 127.0.0.1")
        try:
            port = int(value.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not (1 <= port <= 65535):
            raise ControlEndpointError("control endpoint port is invalid")
        if not str(value.get("token") or ""):
            raise ControlEndpointError("control endpoint token is missing")
        return value

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool,
        payload: Optional[Dict[str, Any]] = None,
        raw_payload: Optional[bytes] = None,
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        endpoint = self._endpoint()
        headers = {"Accept": "application/json"}
        body = None
        if authenticated:
            headers["Authorization"] = "Bearer " + str(endpoint["token"])
        if payload is not None and raw_payload is not None:
            raise ValueError("payload and raw_payload are mutually exclusive")
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_payload is not None:
            body = bytes(raw_payload)
            headers["Content-Type"] = str(content_type)
        url = "http://%s:%s%s" % (LOOPBACK_HOST, int(endpoint["port"]), path)
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=str(method),
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                status = int(response.getcode())
                value = read_json_response(response.read(), status)
        except urllib.error.HTTPError as exc:
            value = read_json_response(
                exc.read(),
                int(exc.code),
                allow_invalid=True,
            )
            error = value.get("error") if isinstance(value, dict) else None
            if not isinstance(error, dict):
                error = {}
            raise ControlRequestError(
                str(error.get("code") or "HTTP_%s" % exc.code),
                str(error.get("message") or "control request failed"),
                int(exc.code),
            )
        except (OSError, urllib.error.URLError) as exc:
            raise ControlEndpointError("control endpoint is unavailable: %s" % exc)
        if not bool(value.get("ok")):
            error = value.get("error") or {}
            raise ControlRequestError(
                str(error.get("code") or "INTERNAL_ERROR"),
                str(error.get("message") or "control request failed"),
                status,
            )
        return value


__all__ = [
    "ControlEndpointError",
    "ControlRequestError",
    "DEFAULT_ENDPOINT_PATH",
    "LocalControlApi",
    "read_json_response",
]
