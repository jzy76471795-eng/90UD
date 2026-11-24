from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from channel.channel_info import parse_channelinfo_grouped_plaintext
from channel.channel_wire import CHANNEL_HEADER_SIZE, ChannelHeader, parse_channel_header


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hex_preview(data: bytes, limit: int = 128) -> str:
    preview = data[:limit].hex(" ")
    if len(data) > limit:
        preview += f" ... (+{len(data) - limit} bytes)"
    return preview


def _safe_text_preview(data: bytes, limit: int = 700) -> str:
    text = data[:limit].decode("utf-8", errors="replace")
    text = text.replace("\x00", "\\0")
    if len(data) > limit:
        text += f"\n... (+{len(data) - limit} bytes)"
    return text


def _packet_fields(header: ChannelHeader, body: bytes) -> dict[str, Any]:
    return {
        "flag": header.flag,
        "flag_hex": f"0x{header.flag:02X}",
        "opcode": header.opcode,
        "opcode_hex": f"0x{header.opcode:02X}",
        "total_length": header.length,
        "total_length_hex": f"0x{header.length:X}",
        "payload_len": header.payload_len,
        "payload_len_hex": f"0x{header.payload_len:X}",
        "tail_or_version": header.tail_or_version,
        "tail_or_version_hex": f"0x{header.tail_or_version:02X}",
        "header_raw_hex": header.raw.hex(" "),
        "body_len": len(body),
        "body_sha256": _sha256_hex(body),
        "body_preview_hex": _hex_preview(body),
    }


class ChannelConnectionDiary:
    """Per-TCP-connection Channel protocol diary.

    When artifact output is enabled, this class writes the verbose protocol
    diagnostics needed for local investigation:

    * all request/response packets as binary captures;
    * request/response header/body metadata;
    * decrypted ChannelInfo plaintext plus a parsed group/channel table;
    * script plaintext source/profile and section markers.
    """

    def __init__(
        self,
        *,
        prefix: str,
        remote: tuple[str, int] | Any,
        capture_dir: Path,
        log_dir: Path,
        doc_dir: Path,
        context: dict[str, Any] | None = None,
        write_artifacts: bool = True,
    ) -> None:
        self.prefix = prefix
        self.remote = f"{remote[0]}:{remote[1]}" if isinstance(remote, tuple) and len(remote) >= 2 else str(remote)
        self.capture_dir = capture_dir
        self.log_dir = log_dir
        self.doc_dir = doc_dir
        self.context = context or {}
        self.write_artifacts = bool(write_artifacts)
        self.started_at = _iso_now()
        self.ended_at: str | None = None
        self.status = "running"
        self.error: str | None = None
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.channelinfo: dict[str, Any] | None = None
        self.script: dict[str, Any] | None = None

    def _write_bytes(self, suffix: str, data: bytes) -> Path:
        path = self.capture_dir / f"{self.prefix}_{suffix}"
        if self.write_artifacts:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return path

    def _artifact(self, kind: str, path: Path, data: bytes, **extra: Any) -> dict[str, Any]:
        item: dict[str, Any] = {
            "kind": kind,
            "path": str(path),
            "size": len(data),
            "sha256": _sha256_hex(data),
        }
        item.update(extra)
        self.artifacts.append(item)
        return item

    def record_request(self, label: str, header: ChannelHeader, body: bytes) -> dict[str, Any]:
        header_path = self._write_bytes(f"{label}_header.bin", header.raw)
        body_path = self._write_bytes(f"{label}_body.bin", body) if body else None
        packet = header.raw + body
        packet_path = self._write_bytes(f"{label}.bin", packet)
        self._artifact("request_header", header_path, header.raw, label=label)
        if body_path:
            self._artifact("request_body", body_path, body, label=label)
        self._artifact("request_packet", packet_path, packet, label=label)
        item = {
            "timestamp": _iso_now(),
            "direction": "recv",
            "label": label,
            "packet_path": str(packet_path),
            "header_path": str(header_path),
            "body_path": str(body_path) if body_path else None,
            **_packet_fields(header, body),
        }
        self.requests.append(item)
        return item

    def record_response(self, label: str, packet: bytes, *, semantic: str | None = None) -> dict[str, Any]:
        if len(packet) < CHANNEL_HEADER_SIZE:
            raise ValueError(f"response packet {label} shorter than Channel header: {len(packet)}")
        header = parse_channel_header(packet[:CHANNEL_HEADER_SIZE])
        body = packet[CHANNEL_HEADER_SIZE:]
        header_path = self._write_bytes(f"{label}_header.bin", header.raw)
        body_path = self._write_bytes(f"{label}_body.bin", body) if body else None
        packet_path = self._write_bytes(f"{label}.bin", packet)
        self._artifact("response_header", header_path, header.raw, label=label, semantic=semantic)
        if body_path:
            self._artifact("response_body", body_path, body, label=label, semantic=semantic)
        self._artifact("response_packet", packet_path, packet, label=label, semantic=semantic)
        item = {
            "timestamp": _iso_now(),
            "direction": "send",
            "label": label,
            "semantic": semantic,
            "packet_path": str(packet_path),
            "header_path": str(header_path),
            "body_path": str(body_path) if body_path else None,
            **_packet_fields(header, body),
        }
        self.responses.append(item)
        return item

    def record_channelinfo_plaintext(
        self,
        plaintext: bytes,
        *,
        envelope: str | None = None,
        label: str = "channelinfo_plain",
    ) -> dict[str, Any]:
        plain_path = self._write_bytes(f"{label}.bin", plaintext)
        self._artifact("channelinfo_plaintext", plain_path, plaintext, envelope=envelope or "none")
        try:
            parsed = parse_channelinfo_grouped_plaintext(plaintext, envelope=envelope)
            parse_error = None
        except Exception as exc:
            parsed = None
            parse_error = f"{type(exc).__name__}: {exc}"
        summary: dict[str, Any] = {
            "timestamp": _iso_now(),
            "profile": self.context.get("channelinfo_profile"),
            "envelope": envelope or "none",
            "label": label,
            "plaintext_path": str(plain_path),
            "plaintext_len": len(plaintext),
            "plaintext_sha256": _sha256_hex(plaintext),
            "plaintext_preview_hex": _hex_preview(plaintext, 256),
            "parse_error": parse_error,
            "parsed": parsed,
        }
        json_data = json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")
        json_path = self._write_bytes(f"{label}.json", json_data)
        self._artifact("channelinfo_plaintext_json", json_path, json_data, envelope=envelope or "none")
        self.channelinfo = summary
        return summary

    def record_script_plaintext(self, plaintext: bytes, *, source: str) -> dict[str, Any]:
        plain_path = self._write_bytes("script_plain.bin", plaintext)
        self._artifact("script_plaintext", plain_path, plaintext, source=source)
        text_head = _safe_text_preview(plaintext)
        markers = sorted(set(m.decode("ascii", errors="replace") for m in re.findall(rb"\[[^\]\r\n\t ]+\]", plaintext[:200000])))
        pvf_file_markers = len(re.findall(rb"#PVF_File", plaintext[:200000]))
        summary = {
            "timestamp": _iso_now(),
            "profile": self.context.get("script_profile"),
            "source": source,
            "plaintext_path": str(plain_path),
            "plaintext_len": len(plaintext),
            "plaintext_sha256": _sha256_hex(plaintext),
            "plaintext_preview_hex": _hex_preview(plaintext, 256),
            "plaintext_preview_text": text_head,
            "section_markers_first_200k": markers[:80],
            "section_marker_count_first_200k": len(markers),
            "pvf_file_marker_count_first_200k": pvf_file_markers,
        }
        self.script = summary
        return summary

    def add_note(self, text: str) -> None:
        self.notes.append(f"{_iso_now()} {text}")

    def request_sequence(self) -> str:
        if not self.requests:
            return "(none)"
        return " -> ".join(f"{item['flag_hex'][2:]} {item['opcode_hex'][2:]}" for item in self.requests)

    def response_sequence(self) -> str:
        if not self.responses:
            return "(none)"
        return " -> ".join(f"{item['flag_hex'][2:]} {item['opcode_hex'][2:]}" for item in self.responses)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "remote": self.remote,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "error": self.error,
            "context": self.context,
            "request_sequence": self.request_sequence(),
            "response_sequence": self.response_sequence(),
            "requests": self.requests,
            "responses": self.responses,
            "channelinfo": self.channelinfo,
            "script": self.script,
            "artifacts": self.artifacts,
            "notes": self.notes,
        }

    def finish(self, *, status: str = "ok", error: str | None = None) -> tuple[Path, Path]:
        self.status = status
        self.error = error
        self.ended_at = _iso_now()
        data = self.to_dict()
        json_path = self.log_dir / f"channel_diary_{self.prefix}.json"
        md_path = self.doc_dir / f"channel_diary_{self.prefix}.md"
        if self.write_artifacts:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.doc_dir.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            md_path.write_text(self._markdown(data), encoding="utf-8")
        return json_path, md_path

    def _markdown(self, data: dict[str, Any]) -> str:
        lines: list[str] = []
        lines.append(f"# Channel diary `{self.prefix}`")
        lines.append("")
        lines.append(f"- remote: `{self.remote}`")
        lines.append(f"- status: `{data['status']}`")
        if data.get("error"):
            lines.append(f"- error: `{data['error']}`")
        lines.append(f"- started_at: `{data['started_at']}`")
        lines.append(f"- ended_at: `{data['ended_at']}`")
        lines.append(f"- request_sequence: `{data['request_sequence']}`")
        lines.append(f"- response_sequence: `{data['response_sequence']}`")
        lines.append("")
        lines.append("## Context")
        for key, value in sorted((data.get("context") or {}).items()):
            lines.append(f"- {key}: `{value}`")
        lines.append("")
        lines.append("## Requests")
        lines.append("| # | label | flag/op | total | payload | body sha256 | body preview | capture |")
        lines.append("|---:|---|---|---:|---:|---|---|---|")
        for idx, item in enumerate(data.get("requests") or []):
            lines.append(
                "| {idx} | `{label}` | `{flag} {op}` | `{total}` | `{payload}` | `{sha}` | `{preview}` | `{path}` |".format(
                    idx=idx,
                    label=item["label"],
                    flag=item["flag_hex"][2:],
                    op=item["opcode_hex"][2:],
                    total=item["total_length_hex"],
                    payload=item["payload_len_hex"],
                    sha=item["body_sha256"][:16],
                    preview=item["body_preview_hex"][:180].replace("|", "\\|"),
                    path=item["packet_path"],
                )
            )
        lines.append("")
        lines.append("## Responses")
        lines.append("| # | label | semantic | flag/op | total | payload | body sha256 | body preview | capture |")
        lines.append("|---:|---|---|---|---:|---:|---|---|---|")
        for idx, item in enumerate(data.get("responses") or []):
            lines.append(
                "| {idx} | `{label}` | `{semantic}` | `{flag} {op}` | `{total}` | `{payload}` | `{sha}` | `{preview}` | `{path}` |".format(
                    idx=idx,
                    label=item["label"],
                    semantic=item.get("semantic") or "",
                    flag=item["flag_hex"][2:],
                    op=item["opcode_hex"][2:],
                    total=item["total_length_hex"],
                    payload=item["payload_len_hex"],
                    sha=item["body_sha256"][:16],
                    preview=item["body_preview_hex"][:180].replace("|", "\\|"),
                    path=item["packet_path"],
                )
            )
        lines.append("")
        if data.get("script"):
            script = data["script"]
            lines.append("## Script plaintext")
            lines.append(f"- source: `{script.get('source')}`")
            lines.append(f"- plaintext_len: `{script.get('plaintext_len')}`")
            lines.append(f"- sha256: `{script.get('plaintext_sha256')}`")
            lines.append(f"- path: `{script.get('plaintext_path')}`")
            lines.append(f"- #PVF_File markers(first 200k): `{script.get('pvf_file_marker_count_first_200k')}`")
            lines.append(f"- section markers(first 200k): `{', '.join(script.get('section_markers_first_200k') or [])}`")
            lines.append("")
            lines.append("```text")
            lines.append((script.get("plaintext_preview_text") or "").replace("```", "'''"))
            lines.append("```")
            lines.append("")
        if data.get("channelinfo"):
            info = data["channelinfo"]
            lines.append("## ChannelInfo plaintext")
            lines.append(f"- profile: `{info.get('profile')}`")
            lines.append(f"- envelope: `{info.get('envelope')}`")
            lines.append(f"- plaintext_len: `{info.get('plaintext_len')}`")
            lines.append(f"- sha256: `{info.get('plaintext_sha256')}`")
            lines.append(f"- path: `{info.get('plaintext_path')}`")
            if info.get("parse_error"):
                lines.append(f"- parse_error: `{info.get('parse_error')}`")
            parsed = info.get("parsed") or {}
            if parsed:
                lines.append(f"- group_count: `{parsed.get('group_count')}`")
                lines.append(f"- bytes_consumed: `{parsed.get('bytes_consumed')}`")
                lines.append("")
                for group in parsed.get("groups") or []:
                    lines.append(f"### Group {group.get('index')}: `{group.get('name')}` channels={group.get('channel_count')}")
                    lines.append("")
                    lines.append("| # | name | max | cur | ip | port |")
                    lines.append("|---:|---|---:|---:|---|---:|")
                    for ch in group.get("channels") or []:
                        lines.append(
                            f"| {ch.get('index')} | `{ch.get('name')}` | {ch.get('max_users')} | "
                            f"{ch.get('cur_users')} | `{ch.get('ip')}` | {ch.get('port')} |"
                        )
                    lines.append("")
        if data.get("notes"):
            lines.append("## Notes")
            for note in data["notes"]:
                lines.append(f"- {note}")
            lines.append("")
        lines.append("## Artifacts")
        for item in data.get("artifacts") or []:
            lines.append(f"- `{item.get('kind')}` `{item.get('path')}` size={item.get('size')} sha256={str(item.get('sha256'))[:16]}")
        lines.append("")
        return "\n".join(lines)
