"""Durable custom image slot for the Stage3 Item of the Month popup."""
from __future__ import annotations

import hashlib
import io
import os
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from PIL import Image, UnidentifiedImageError


ITEM_OF_MONTH_CONFIG_MAGIC = b"UIOM"
ITEM_OF_MONTH_CONFIG_VERSION = 1
ITEM_OF_MONTH_MIME_PNG = 1
ITEM_OF_MONTH_MIME_JPEG = 2
ITEM_OF_MONTH_POPUP_REQUEST_MAGIC = b"UIOC"
ITEM_OF_MONTH_POPUP_REQUEST_LEGACY_VERSION = 1
ITEM_OF_MONTH_POPUP_REQUEST_TITLE_VERSION = 2
ITEM_OF_MONTH_POPUP_REQUEST_VERSION = 3
ITEM_OF_MONTH_POPUP_ACTION_SHOW = "show"
ITEM_OF_MONTH_POPUP_ACTION_CLOSE = "close"
ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM = "custom"
ITEM_OF_MONTH_POPUP_SOURCE_ICON = "icon"
DEFAULT_ITEM_OF_MONTH_POPUP_TITLE = "Item of Month"
ITEM_OF_MONTH_ICON_POPUP_TITLE = "90US\u7248\u672c\u58f0\u660e"
MAX_ITEM_OF_MONTH_POPUP_TITLE_UTF16_UNITS = 80
MAX_ITEM_OF_MONTH_POPUP_TITLE_UTF8_BYTES = (
    MAX_ITEM_OF_MONTH_POPUP_TITLE_UTF16_UNITS * 4
)
MAX_ITEM_OF_MONTH_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ITEM_OF_MONTH_IMAGE_EDGE = 4096
MAX_ITEM_OF_MONTH_IMAGE_PIXELS = 16 * 1024 * 1024

_CONFIG_STRUCT = struct.Struct("<4sHHQIIQQ32s")
_POPUP_REQUEST_STRUCT = struct.Struct("<4sHHQQ")
_MIME_TO_CODE = {
    "image/png": ITEM_OF_MONTH_MIME_PNG,
    "image/jpeg": ITEM_OF_MONTH_MIME_JPEG,
}
_CODE_TO_MIME = {value: key for key, value in _MIME_TO_CODE.items()}
_FORMAT_TO_MIME = {"PNG": "image/png", "JPEG": "image/jpeg"}
_POPUP_ACTION_TO_CODE = {
    ITEM_OF_MONTH_POPUP_ACTION_SHOW: 1,
    ITEM_OF_MONTH_POPUP_ACTION_CLOSE: 2,
}
_POPUP_CODE_TO_ACTION = {value: key for key, value in _POPUP_ACTION_TO_CODE.items()}
_POPUP_SOURCE_TO_CODE = {
    ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM: 1,
    ITEM_OF_MONTH_POPUP_SOURCE_ICON: 2,
}
_POPUP_CODE_TO_SOURCE = {value: key for key, value in _POPUP_SOURCE_TO_CODE.items()}
_POPUP_ACTION_MASK = 0x00FF
_POPUP_SOURCE_SHIFT = 8
_STATE_LOCK = threading.RLock()


class ItemOfMonthImageError(ValueError):
    """Stable validation or persistence error for the custom image slot."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.status = int(status)


def _normalize_popup_title(value: str) -> tuple[str, bytes]:
    if not isinstance(value, str):
        raise ItemOfMonthImageError(
            "INVALID_POPUP_TITLE",
            "popup title must be a string",
            status=400,
        )
    title = value.strip()
    if not title or "\x00" in title or "\r" in title or "\n" in title:
        raise ItemOfMonthImageError(
            "INVALID_POPUP_TITLE",
            "popup title must be one nonempty line without NUL",
            status=400,
        )
    try:
        utf16_units = len(title.encode("utf-16-le")) // 2
        encoded = title.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ItemOfMonthImageError(
            "INVALID_POPUP_TITLE",
            "popup title is not encodable",
            status=400,
        ) from exc
    if (
        utf16_units > MAX_ITEM_OF_MONTH_POPUP_TITLE_UTF16_UNITS
        or len(encoded) > MAX_ITEM_OF_MONTH_POPUP_TITLE_UTF8_BYTES
    ):
        raise ItemOfMonthImageError(
            "INVALID_POPUP_TITLE",
            "popup title exceeds 80 UTF-16 units",
            status=400,
        )
    return title, encoded


def normalize_item_of_month_popup_title(value: str) -> str:
    title, _encoded = _normalize_popup_title(value)
    return title


@dataclass(frozen=True)
class ItemOfMonthImageMetadata:
    mime_type: str
    width: int
    height: int
    image_size: int
    updated_unix_ms: int
    content_version: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": True,
            "mime_type": self.mime_type,
            "width": int(self.width),
            "height": int(self.height),
            "image_size": int(self.image_size),
            "updated_unix_ms": int(self.updated_unix_ms),
            "content_version": int(self.content_version),
            "sha256": self.sha256,
        }

    def pack_config(self) -> bytes:
        try:
            digest = bytes.fromhex(self.sha256)
        except ValueError as exc:
            raise ItemOfMonthImageError(
                "IMAGE_STATE_INVALID",
                "stored image digest is invalid",
                status=409,
            ) from exc
        mime_code = _MIME_TO_CODE.get(self.mime_type)
        if mime_code is None or len(digest) != 32:
            raise ItemOfMonthImageError(
                "IMAGE_STATE_INVALID",
                "stored image metadata is invalid",
                status=409,
            )
        return _CONFIG_STRUCT.pack(
            ITEM_OF_MONTH_CONFIG_MAGIC,
            ITEM_OF_MONTH_CONFIG_VERSION,
            mime_code,
            int(self.image_size),
            int(self.width),
            int(self.height),
            int(self.updated_unix_ms),
            int(self.content_version),
            digest,
        )

    @classmethod
    def unpack_config(cls, value: bytes) -> "ItemOfMonthImageMetadata":
        if len(value) != _CONFIG_STRUCT.size:
            raise ItemOfMonthImageError(
                "IMAGE_STATE_INVALID",
                "stored image config size is invalid",
                status=409,
            )
        (
            magic,
            config_version,
            mime_code,
            image_size,
            width,
            height,
            updated_unix_ms,
            content_version,
            digest,
        ) = _CONFIG_STRUCT.unpack(value)
        mime_type = _CODE_TO_MIME.get(int(mime_code))
        if (
            magic != ITEM_OF_MONTH_CONFIG_MAGIC
            or int(config_version) != ITEM_OF_MONTH_CONFIG_VERSION
            or mime_type is None
            or not 0 < int(image_size) <= MAX_ITEM_OF_MONTH_IMAGE_BYTES
            or not _valid_dimensions(int(width), int(height))
            or int(updated_unix_ms) <= 0
            or int(content_version) <= 0
        ):
            raise ItemOfMonthImageError(
                "IMAGE_STATE_INVALID",
                "stored image config fields are invalid",
                status=409,
            )
        return cls(
            mime_type=mime_type,
            width=int(width),
            height=int(height),
            image_size=int(image_size),
            updated_unix_ms=int(updated_unix_ms),
            content_version=int(content_version),
            sha256=bytes(digest).hex(),
        )


@dataclass(frozen=True)
class ItemOfMonthPopupRequest:
    action: str
    request_id: int
    created_unix_ms: int
    title: str = ""
    source: str = ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "request_id": int(self.request_id),
            "created_unix_ms": int(self.created_unix_ms),
            "title": self.title,
            "source": (
                self.source
                if self.action == ITEM_OF_MONTH_POPUP_ACTION_SHOW
                else ""
            ),
        }

    def pack(self) -> bytes:
        action_code = _POPUP_ACTION_TO_CODE.get(self.action)
        if (
            action_code is None
            or int(self.request_id) <= 0
            or int(self.created_unix_ms) <= 0
        ):
            raise ItemOfMonthImageError(
                "POPUP_REQUEST_INVALID",
                "popup request fields are invalid",
                status=400,
            )
        if self.action == ITEM_OF_MONTH_POPUP_ACTION_SHOW:
            source_code = _POPUP_SOURCE_TO_CODE.get(self.source)
            if source_code is None:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "popup request source is invalid",
                    status=400,
                )
            title, encoded_title = _normalize_popup_title(self.title)
            if title != self.title:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "popup title must already be normalized",
                    status=400,
                )
            if (
                self.source == ITEM_OF_MONTH_POPUP_SOURCE_ICON
                and self.title != ITEM_OF_MONTH_ICON_POPUP_TITLE
            ):
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "icon popup title must match the fixed activity title",
                    status=400,
                )
            detail = action_code | (source_code << _POPUP_SOURCE_SHIFT)
        else:
            if self.title:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "close popup request must not contain a title",
                    status=400,
                )
            if self.source not in {"", ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM}:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "close popup request must not contain a source",
                    status=400,
                )
            encoded_title = b""
            detail = action_code
        return _POPUP_REQUEST_STRUCT.pack(
            ITEM_OF_MONTH_POPUP_REQUEST_MAGIC,
            ITEM_OF_MONTH_POPUP_REQUEST_VERSION,
            detail,
            int(self.request_id),
            int(self.created_unix_ms),
        ) + encoded_title

    @classmethod
    def unpack(cls, payload: bytes) -> "ItemOfMonthPopupRequest":
        if len(payload) < _POPUP_REQUEST_STRUCT.size:
            raise ItemOfMonthImageError(
                "POPUP_REQUEST_INVALID",
                "popup request size is invalid",
                status=400,
            )
        magic, version, detail, request_id, created_unix_ms = (
            _POPUP_REQUEST_STRUCT.unpack_from(payload)
        )
        if (
            magic != ITEM_OF_MONTH_POPUP_REQUEST_MAGIC
            or int(version) not in {
                ITEM_OF_MONTH_POPUP_REQUEST_LEGACY_VERSION,
                ITEM_OF_MONTH_POPUP_REQUEST_TITLE_VERSION,
                ITEM_OF_MONTH_POPUP_REQUEST_VERSION,
            }
            or int(request_id) <= 0
            or int(created_unix_ms) <= 0
        ):
            raise ItemOfMonthImageError(
                "POPUP_REQUEST_INVALID",
                "popup request fields are invalid",
                status=400,
            )
        if int(version) == ITEM_OF_MONTH_POPUP_REQUEST_VERSION:
            action_code = int(detail) & _POPUP_ACTION_MASK
            source_code = int(detail) >> _POPUP_SOURCE_SHIFT
            action = _POPUP_CODE_TO_ACTION.get(action_code)
            source = _POPUP_CODE_TO_SOURCE.get(source_code, "")
            source_valid = (
                action == ITEM_OF_MONTH_POPUP_ACTION_SHOW
                and source in _POPUP_SOURCE_TO_CODE
            ) or (
                action == ITEM_OF_MONTH_POPUP_ACTION_CLOSE
                and source_code == 0
            )
        else:
            action = _POPUP_CODE_TO_ACTION.get(int(detail))
            source = (
                ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM
                if action == ITEM_OF_MONTH_POPUP_ACTION_SHOW
                else ""
            )
            source_valid = action is not None
        if action is None or not source_valid:
            raise ItemOfMonthImageError(
                "POPUP_REQUEST_INVALID",
                "popup request action or source is invalid",
                status=400,
            )
        encoded_title = payload[_POPUP_REQUEST_STRUCT.size :]
        if int(version) == ITEM_OF_MONTH_POPUP_REQUEST_LEGACY_VERSION:
            if encoded_title:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "legacy popup request has trailing data",
                    status=400,
                )
            title = (
                DEFAULT_ITEM_OF_MONTH_POPUP_TITLE
                if action == ITEM_OF_MONTH_POPUP_ACTION_SHOW
                else ""
            )
        elif action == ITEM_OF_MONTH_POPUP_ACTION_SHOW:
            if not encoded_title or len(encoded_title) > MAX_ITEM_OF_MONTH_POPUP_TITLE_UTF8_BYTES:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "show popup request title length is invalid",
                    status=400,
                )
            try:
                decoded_title = encoded_title.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "popup request title is not UTF-8",
                    status=400,
                ) from exc
            title, normalized_title = _normalize_popup_title(decoded_title)
            if normalized_title != encoded_title:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "popup request title is not normalized",
                    status=400,
                )
        else:
            if encoded_title:
                raise ItemOfMonthImageError(
                    "POPUP_REQUEST_INVALID",
                    "close popup request must not contain a title",
                    status=400,
                )
            title = ""
        if (
            source == ITEM_OF_MONTH_POPUP_SOURCE_ICON
            and title != ITEM_OF_MONTH_ICON_POPUP_TITLE
        ):
            raise ItemOfMonthImageError(
                "POPUP_REQUEST_INVALID",
                "icon popup title does not match the fixed activity title",
                status=400,
            )
        return cls(
            action=action,
            request_id=int(request_id),
            created_unix_ms=int(created_unix_ms),
            title=title,
            source=source,
        )


def _server_home() -> Path:
    raw = str(os.environ.get("USDOF_SERVER_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _path_from_env(name: str, fallback_name: str) -> Path:
    raw = str(os.environ.get(name) or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            base_raw = str(os.environ.get("USDOF_PATH_BASE") or "").strip()
            path = (Path(base_raw) if base_raw else Path.cwd()) / path
        return path.resolve()
    return (
        _server_home()
        / "data"
        / "runtime_cache"
        / "item_of_month"
        / fallback_name
    ).resolve()


def _resource_path_from_env(name: str, fallback_name: str) -> Path:
    raw = str(os.environ.get(name) or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            base_raw = str(os.environ.get("USDOF_PATH_BASE") or "").strip()
            path = (Path(base_raw) if base_raw else Path.cwd()) / path
        return path.resolve()
    return (_server_home() / "data" / "resources" / fallback_name).resolve()


def item_of_month_icon_config_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    return _resource_path_from_env(
        "USDOF_ITEM_OF_MONTH_ICON_CONFIG_PATH",
        "USDOFItemOfMonthIconConfig.bin",
    )


def item_of_month_icon_image_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    return _resource_path_from_env(
        "USDOF_ITEM_OF_MONTH_ICON_IMAGE_PATH",
        "USDOFItemOfMonthIconImage.bin",
    )


def item_of_month_config_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    return _path_from_env(
        "USDOF_ITEM_OF_MONTH_CONFIG_PATH",
        "USDOFItemOfMonthConfig.bin",
    )


def item_of_month_image_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    return _path_from_env(
        "USDOF_ITEM_OF_MONTH_IMAGE_PATH",
        "USDOFItemOfMonthImage.bin",
    )


def item_of_month_popup_request_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    return _path_from_env(
        "USDOF_ITEM_OF_MONTH_POPUP_REQUEST_PATH",
        "USDOFItemOfMonthPopupRequest.bin",
    )


def _normalize_mime_type(content_type: str) -> str:
    value = str(content_type or "").split(";", 1)[0].strip().lower()
    if value == "image/jpg":
        value = "image/jpeg"
    if value not in _MIME_TO_CODE:
        raise ItemOfMonthImageError(
            "UNSUPPORTED_MEDIA_TYPE",
            "image content type must be image/png or image/jpeg",
            status=415,
        )
    return value


def _valid_dimensions(width: int, height: int) -> bool:
    return (
        0 < int(width) <= MAX_ITEM_OF_MONTH_IMAGE_EDGE
        and 0 < int(height) <= MAX_ITEM_OF_MONTH_IMAGE_EDGE
        and int(width) * int(height) <= MAX_ITEM_OF_MONTH_IMAGE_PIXELS
    )


def _validate_decoded_image(payload: bytes, content_type: str) -> Tuple[str, int, int]:
    mime_type = _normalize_mime_type(content_type)
    if not isinstance(payload, bytes):
        raise ItemOfMonthImageError(
            "INVALID_IMAGE",
            "image body must be bytes",
            status=400,
        )
    if not payload:
        raise ItemOfMonthImageError(
            "INVALID_IMAGE",
            "image body is empty",
            status=400,
        )
    if len(payload) > MAX_ITEM_OF_MONTH_IMAGE_BYTES:
        raise ItemOfMonthImageError(
            "IMAGE_TOO_LARGE",
            "image body exceeds 5 MiB",
            status=413,
        )
    try:
        with Image.open(io.BytesIO(payload)) as image:
            actual_mime = _FORMAT_TO_MIME.get(str(image.format or "").upper())
            width, height = (int(image.width), int(image.height))
            if actual_mime != mime_type:
                raise ItemOfMonthImageError(
                    "IMAGE_FORMAT_MISMATCH",
                    "decoded image format does not match Content-Type",
                    status=400,
                )
            if not _valid_dimensions(width, height):
                raise ItemOfMonthImageError(
                    "IMAGE_DIMENSIONS_INVALID",
                    "image dimensions exceed 4096 per edge or 16 megapixels",
                    status=400,
                )
            image.verify()
        with Image.open(io.BytesIO(payload)) as decoded:
            decoded.load()
            if (int(decoded.width), int(decoded.height)) != (width, height):
                raise ItemOfMonthImageError(
                    "INVALID_IMAGE",
                    "decoded image dimensions are inconsistent",
                    status=400,
                )
    except ItemOfMonthImageError:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ItemOfMonthImageError(
            "INVALID_IMAGE",
            "image data is incomplete or invalid",
            status=400,
        ) from exc
    return mime_type, width, height


def _atomic_write(path: Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        "%s.tmp.%s.%s" % (target.name, os.getpid(), time.time_ns())
    )
    try:
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(target))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def store_item_of_month_image(
    payload: bytes,
    content_type: str,
    *,
    config_path: Optional[Path] = None,
    image_path: Optional[Path] = None,
) -> ItemOfMonthImageMetadata:
    raw = bytes(payload) if isinstance(payload, (bytes, bytearray)) else payload
    mime_type, width, height = _validate_decoded_image(raw, content_type)
    now_ns = time.time_ns()
    metadata = ItemOfMonthImageMetadata(
        mime_type=mime_type,
        width=width,
        height=height,
        image_size=len(raw),
        updated_unix_ms=max(1, now_ns // 1_000_000),
        content_version=max(1, now_ns & 0xFFFFFFFFFFFFFFFF),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    config = item_of_month_config_path(config_path)
    image = item_of_month_image_path(image_path)
    if config == image:
        raise ItemOfMonthImageError(
            "IMAGE_STATE_INVALID",
            "image and config paths must be different",
            status=500,
        )
    with _STATE_LOCK:
        _atomic_write(image, raw)
        _atomic_write(config, metadata.pack_config())
    return metadata


def get_item_of_month_image_metadata(
    *,
    config_path: Optional[Path] = None,
    image_path: Optional[Path] = None,
) -> Optional[ItemOfMonthImageMetadata]:
    config = item_of_month_config_path(config_path)
    image = item_of_month_image_path(image_path)
    with _STATE_LOCK:
        try:
            config_payload = config.read_bytes()
        except FileNotFoundError:
            return None
        metadata = ItemOfMonthImageMetadata.unpack_config(config_payload)
        try:
            raw = image.read_bytes()
        except FileNotFoundError as exc:
            raise ItemOfMonthImageError(
                "IMAGE_STATE_INVALID",
                "stored image file is missing",
                status=409,
            ) from exc
        if (
            len(raw) != metadata.image_size
            or hashlib.sha256(raw).hexdigest() != metadata.sha256
        ):
            raise ItemOfMonthImageError(
                "IMAGE_STATE_INVALID",
                "stored image does not match its committed config",
                status=409,
            )
    return metadata


def clear_item_of_month_image(
    *,
    config_path: Optional[Path] = None,
    image_path: Optional[Path] = None,
) -> bool:
    config = item_of_month_config_path(config_path)
    image = item_of_month_image_path(image_path)
    removed = False
    with _STATE_LOCK:
        for path in (config, image):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ItemOfMonthImageError(
                    "IMAGE_PERSISTENCE_FAILED",
                    "stored image could not be cleared",
                    status=500,
                ) from exc
    return removed


def publish_item_of_month_popup_request(
    action: str,
    *,
    title: Optional[str] = None,
    source: str = ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM,
    request_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    image_path: Optional[Path] = None,
    icon_config_path: Optional[Path] = None,
    icon_image_path: Optional[Path] = None,
) -> ItemOfMonthPopupRequest:
    normalized = str(action or "").strip().lower()
    if normalized not in _POPUP_ACTION_TO_CODE:
        raise ItemOfMonthImageError(
            "INVALID_POPUP_ACTION",
            "popup action must be show or close",
            status=400,
        )
    if normalized == ITEM_OF_MONTH_POPUP_ACTION_SHOW:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in _POPUP_SOURCE_TO_CODE:
            raise ItemOfMonthImageError(
                "INVALID_POPUP_SOURCE",
                "popup source must be custom or icon",
                status=400,
            )
        if normalized_source == ITEM_OF_MONTH_POPUP_SOURCE_ICON:
            if title not in {None, ITEM_OF_MONTH_ICON_POPUP_TITLE}:
                raise ItemOfMonthImageError(
                    "INVALID_POPUP_TITLE",
                    "icon popup title is fixed by the activity entry",
                    status=400,
                )
            popup_title = ITEM_OF_MONTH_ICON_POPUP_TITLE
            selected_config = item_of_month_icon_config_path(icon_config_path)
            selected_image = item_of_month_icon_image_path(icon_image_path)
        else:
            popup_title, _encoded_title = _normalize_popup_title(
                DEFAULT_ITEM_OF_MONTH_POPUP_TITLE if title is None else title
            )
            selected_config = item_of_month_config_path(config_path)
            selected_image = item_of_month_image_path(image_path)
        metadata = get_item_of_month_image_metadata(
            config_path=selected_config,
            image_path=selected_image,
        )
        if metadata is None:
            raise ItemOfMonthImageError(
                "IMAGE_NOT_CONFIGURED",
                "%s Item of the Month image is not configured" % normalized_source,
                status=409,
            )
    else:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in {"", ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM}:
            raise ItemOfMonthImageError(
                "INVALID_POPUP_SOURCE",
                "close popup request does not accept a source",
                status=400,
            )
        if title not in {None, ""}:
            raise ItemOfMonthImageError(
                "INVALID_POPUP_TITLE",
                "close popup request does not accept a title",
                status=400,
            )
        popup_title = ""
        normalized_source = ""
        selected_config = item_of_month_config_path(config_path)
        selected_image = item_of_month_image_path(image_path)

    target = item_of_month_popup_request_path(request_path)
    if target in {selected_config, selected_image}:
        raise ItemOfMonthImageError(
            "IMAGE_STATE_INVALID",
            "popup request path must be separate from image state",
            status=500,
        )
    now_ns = time.time_ns()
    request = ItemOfMonthPopupRequest(
        action=normalized,
        request_id=max(1, now_ns & 0xFFFFFFFFFFFFFFFF),
        created_unix_ms=max(1, now_ns // 1_000_000),
        title=popup_title,
        source=normalized_source,
    )
    with _STATE_LOCK:
        _atomic_write(target, request.pack())
    return request


def read_item_of_month_popup_request(
    *,
    request_path: Optional[Path] = None,
) -> Optional[ItemOfMonthPopupRequest]:
    target = item_of_month_popup_request_path(request_path)
    with _STATE_LOCK:
        try:
            payload = target.read_bytes()
        except FileNotFoundError:
            return None
    return ItemOfMonthPopupRequest.unpack(payload)


__all__ = [
    "ITEM_OF_MONTH_CONFIG_MAGIC",
    "ITEM_OF_MONTH_CONFIG_VERSION",
    "ITEM_OF_MONTH_MIME_JPEG",
    "ITEM_OF_MONTH_MIME_PNG",
    "ITEM_OF_MONTH_POPUP_ACTION_CLOSE",
    "ITEM_OF_MONTH_POPUP_ACTION_SHOW",
    "ITEM_OF_MONTH_POPUP_SOURCE_CUSTOM",
    "ITEM_OF_MONTH_POPUP_SOURCE_ICON",
    "DEFAULT_ITEM_OF_MONTH_POPUP_TITLE",
    "ITEM_OF_MONTH_ICON_POPUP_TITLE",
    "ITEM_OF_MONTH_POPUP_REQUEST_MAGIC",
    "ITEM_OF_MONTH_POPUP_REQUEST_LEGACY_VERSION",
    "ITEM_OF_MONTH_POPUP_REQUEST_TITLE_VERSION",
    "ITEM_OF_MONTH_POPUP_REQUEST_VERSION",
    "ItemOfMonthImageError",
    "ItemOfMonthImageMetadata",
    "ItemOfMonthPopupRequest",
    "MAX_ITEM_OF_MONTH_IMAGE_BYTES",
    "MAX_ITEM_OF_MONTH_IMAGE_EDGE",
    "MAX_ITEM_OF_MONTH_IMAGE_PIXELS",
    "MAX_ITEM_OF_MONTH_POPUP_TITLE_UTF16_UNITS",
    "MAX_ITEM_OF_MONTH_POPUP_TITLE_UTF8_BYTES",
    "clear_item_of_month_image",
    "get_item_of_month_image_metadata",
    "item_of_month_icon_config_path",
    "item_of_month_icon_image_path",
    "item_of_month_config_path",
    "item_of_month_image_path",
    "item_of_month_popup_request_path",
    "normalize_item_of_month_popup_title",
    "publish_item_of_month_popup_request",
    "read_item_of_month_popup_request",
    "store_item_of_month_image",
]
