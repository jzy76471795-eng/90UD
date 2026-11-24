"""创建角色 (C->S 0x0005) + 0x0355 pending refresh + debug gate

从 ``game_server.py`` 拆出的角色相关补包链。通过 ``self.server`` 复用
GameServer 的 DB/codec/日志等基础设施，避免循环依赖。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import LOG_DIR
from game.character.character_db import has_available_slot, parse_create_character_body
from game.character.initial_equipment import get_initial_equipment_for_job, get_initial_grow_type_for_job
from game.character.new_character_contract import (
    apply_new_character_contract,
    format_new_character_contract_report,
)

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return int(default)
    return int(raw.strip(), 0)


def _env_optional_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return int(str(raw).strip(), 0)


def _env_hex_bytes(name: str) -> bytes | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace("0x", "").replace("0X", "")
    normalized = "".join(ch for ch in normalized if ch not in " \t\r\n:_-")
    if len(normalized) % 2:
        raise ValueError(f"{name} must contain an even number of hex digits: {raw!r}")
    return bytes.fromhex(normalized)


class CharCreateCharacterService:
    """创建角色 (C->S 0x0005) + 0x0355 pending refresh + debug gate"""

    def __init__(self, server: Any) -> None:
        self.server = server

    def maybe_wait_create_debug_gate(self, stage: str, context: dict[str, object]) -> str:
        """Optional local debug synchronization before create/roster responses.

        This is deliberately file-based and disabled by default.  It lets a
        separate watcher attach x32dbg and plant CharCard breakpoints only
        after the client has clicked Create/OK and the server has received the
        command, but before the server sends data that drives the character-list
        redraw.
        """

        if not _env_bool("USDOF_CREATE_CHARACTER_DEBUG_GATE", False):
            return ""

        wanted = os.environ.get("USDOF_CREATE_CHARACTER_DEBUG_GATE_STAGE", "create-ack")
        wanted_stages = {item.strip().lower() for item in wanted.replace(";", ",").split(",") if item.strip()}
        normalized_stage = stage.strip().lower()
        if "all" not in wanted_stages and normalized_stage not in wanted_stages:
            return ""

        trigger_path = Path(
            os.environ.get(
                "USDOF_CREATE_CHARACTER_DEBUG_TRIGGER_FILE",
                str(LOG_DIR / "create_character_debug_gate_trigger.json"),
            )
        )
        ready_path = Path(
            os.environ.get(
                "USDOF_CREATE_CHARACTER_DEBUG_READY_FILE",
                str(LOG_DIR / "create_character_debug_gate_ready.json"),
            )
        )
        timeout_ms = max(0, _env_int("USDOF_CREATE_CHARACTER_DEBUG_GATE_TIMEOUT_MS", 15000))
        poll_seconds = max(0.05, _env_int("USDOF_CREATE_CHARACTER_DEBUG_GATE_POLL_MS", 100) / 1000.0)
        nonce = f"{int(time.time() * 1000)}-{os.getpid()}-{threading.get_ident()}"
        trigger = {
            "nonce": nonce,
            "stage": normalized_stage,
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "timeout_ms": timeout_ms,
            "context": context,
        }

        try:
            trigger_path.parent.mkdir(parents=True, exist_ok=True)
            ready_path.parent.mkdir(parents=True, exist_ok=True)
            if ready_path.exists():
                ready_path.unlink()
            trigger_path.write_text(json.dumps(trigger, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            return f" debug_gate stage={normalized_stage} trigger_write_failed={type(exc).__name__}:{exc}"

        self.server.log(
            f"[Game/DebugGate] stage={normalized_stage} nonce={nonce} "
            f"trigger={trigger_path} ready={ready_path} timeout_ms={timeout_ms}"
        )
        deadline = time.time() + timeout_ms / 1000.0
        last_error = ""
        while time.time() < deadline:
            if ready_path.exists():
                try:
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    last_error = f"ready_parse_failed={type(exc).__name__}:{exc}"
                    time.sleep(poll_seconds)
                    continue
                if ready.get("nonce") != nonce:
                    last_error = f"ready_nonce_mismatch={ready.get('nonce')!r}"
                    time.sleep(poll_seconds)
                    continue
                ok = bool(ready.get("ok", False))
                note = str(ready.get("note") or "")
                self.server.log(
                    f"[Game/DebugGate] stage={normalized_stage} nonce={nonce} "
                    f"ready ok={ok} note={note}"
                )
                return (
                    f" debug_gate stage={normalized_stage} ready ok={ok} "
                    f"prefix={ready.get('prefix', '')} runner_pid={ready.get('runner_pid', '')} note={note}"
                )
            time.sleep(poll_seconds)

        return (
            f" debug_gate stage={normalized_stage} timeout_ms={timeout_ms} "
            f"trigger={trigger_path} ready={ready_path} {last_error}".rstrip()
        )


    def handle_create_character(self, body: bytes) -> tuple[list[bytes], str]:
        repo = self.server._repo()
        template = repo.load_userinfo_template()
        count = repo.count_by_account(self.server.account_id)
        if not has_available_slot(count, template.gate_or_count1, template.gate_or_count2):
            packet = self.server._codec_response(1, 0x0005, b"\x00\x04")
            return [packet], (
                f"CREATE_CHARACTER rejected: no free slot account_id={self.server.account_id} "
                f"count={count} limit={template.slot_limit}; sent codec5 failure status=0x04 wire={len(packet)}"
            )

        parsed = parse_create_character_body(body)
        fallback_enabled = os.environ.get("USDOF_CREATE_FALLBACK_ON_UNPARSEABLE", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if parsed is None:
            if not fallback_enabled:
                return [], (
                    "CREATE_CHARACTER body not decoded/plausible and fallback disabled; "
                    f"no DB write; body={body.hex(' ')}"
                )
            fallback_name = os.environ.get("USDOF_CREATE_FALLBACK_NAME") or f"Hero{repo.next_character_id()}"
            base = fallback_name
            suffix = 1
            while repo.get_by_name(fallback_name) is not None:
                fallback_name = f"{base}_{suffix}"
                suffix += 1
            parsed_note = f"fallback name={fallback_name!r} job=0 because client command body is not decoded yet"
            name = fallback_name
            job = int(os.environ.get("USDOF_CREATE_FALLBACK_JOB", "0"), 0) & 0xFF
            appearance_blob = body
        else:
            if repo.get_by_name(parsed.name) is not None:
                packet = self.server._codec_response(1, 0x0005, b"\x00\x02")
                return [packet], (
                    f"CREATE_CHARACTER duplicate name={parsed.name!r} source={parsed.source}; "
                    f"sent codec5 failure status=0x02 wire={len(packet)}"
                )
            parsed_note = (
                f"parsed source={parsed.source} name={parsed.name!r} job={parsed.job} "
                f"name_len={parsed.raw_name_len} tail={parsed.body_after_name.hex(' ')}"
            )
            name = parsed.name
            job = parsed.job
            appearance_blob = parsed.raw_body

        initial_grow_type = get_initial_grow_type_for_job(job)
        initial_equipment = get_initial_equipment_for_job(job)
        record = repo.create_character(
            account_id=self.server.account_id,
            name=name,
            job=job,
            grow_type=initial_grow_type,
            level=1,
            appearance_blob=appearance_blob,
            equipped_entries=initial_equipment,
        )
        try:
            contract = apply_new_character_contract(
                repo,
                int(record.character_id),
                strict_new=True,
                expected_equipment=initial_equipment,
            )
            contract_note = format_new_character_contract_report(contract)
        except Exception as exc:
            cleanup_note = "cleanup=ok"
            try:
                repo.physical_delete(int(record.character_id))
            except Exception as cleanup_exc:
                cleanup_note = "cleanup_failed=%s:%s" % (
                    type(cleanup_exc).__name__,
                    cleanup_exc,
                )
            packet = self.server._codec_response(1, 0x0005, b"\x00\x04")
            return [packet], (
                "CREATE_CHARACTER contract failed character_id=%s name=%r "
                "reason=%s:%s %s; sent existing codec5 failure status=0x04 wire=%s"
                % (
                    record.character_id,
                    record.display_name,
                    type(exc).__name__,
                    exc,
                    cleanup_note,
                    len(packet),
                )
            )
        equipment_note = ",".join(f"{slot}:{item_id}" for slot, item_id in initial_equipment) or "<none>"
        success_body = (
            b"\x01"
            + (record.slot_index & 0xFFFF).to_bytes(2, "little")
            + len(record.name_bytes).to_bytes(4, "little")
            + record.name_bytes
        )
        debug_gate_note = self.maybe_wait_create_debug_gate(
            "create-ack",
            {
                "character_id": record.character_id,
                "slot": record.slot_index,
                "name": record.display_name,
                "job": record.job,
                "grow_type": record.grow_type,
                "level": record.level,
            },
        )
        packets = [self.server._codec_response(1, 0x0005, success_body)]
        refresh_note = (
            "create USERINFO refresh deferred until C2S 0x0355; current EXE S2C 0x0005 success handler "
            "reads slot/name first, then asks the server-side refresh chain through follow-up requests"
        )
        if _env_bool("USDOF_CREATE_CHARACTER_SEND_USERINFO_REFRESH", False):
            userinfo_packets, userinfo_note = self.server._db_userinfo_packet_stream(
                "create-character-refresh",
                include_followups=_env_bool("USDOF_CREATE_CHARACTER_USERINFO_FOLLOWUPS", True),
            )
            packets.extend(userinfo_packets)
            refresh_note = userinfo_note
        return packets, (
            f"CREATE_CHARACTER stored character_id={record.character_id} slot={record.slot_index} "
            f"name={record.display_name!r} job={record.job} grow_type={record.grow_type} "
            f"initial_equipment={equipment_note}; {contract_note}; {parsed_note}; "
            f"sent codec5 success body={success_body.hex(' ')} wire={len(packets[0])}"
            f"{debug_gate_note} + {refresh_note}"
        )


    def handle_create_character_0355_refresh(self) -> tuple[list[bytes], str]:
        if not _env_bool("USDOF_CREATE_CHARACTER_REFRESH_ON_0355", True):
            return [], (
                "CREATE_CHARACTER pending 0x0355 refresh skipped by "
                "USDOF_CREATE_CHARACTER_REFRESH_ON_0355=0"
            )
        debug_gate_note = self.maybe_wait_create_debug_gate(
            "0355-refresh",
            {
                "reason": "create-character-0355-refresh",
                "account_id": self.server.account_id,
            },
        )
        packets, note = self.server._db_userinfo_packet_stream(
            "create-character-0355-refresh",
            include_followups=_env_bool("USDOF_CREATE_CHARACTER_0355_USERINFO_FOLLOWUPS", True),
        )
        return packets, f"CREATE_CHARACTER pending 0x0355 refresh sent{debug_gate_note} {note}"

