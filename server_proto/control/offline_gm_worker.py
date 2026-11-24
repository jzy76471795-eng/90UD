"""Process-isolated direct SQLite/PVF execution for the native GM tool."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional


PROTOCOL_PREFIX = "USDOF_OFFLINE_GM="
CORE_PVF_COMMAND_PREFIXES = (
    "character.",
    "content_block.",
    "inventory.",
    "quest.",
)
CORE_PVF_COMMANDS = frozenset(
    {
        "loadout.apply",
        "loadout.read_current",
        "pvf.list_items",
    }
)


def _restore_standard_streams() -> tuple[Any, Any, Any]:
    """Restore UTF-8 pipes hidden by PyInstaller's windowed bootloader."""

    def restore(fd: int, name: str, mode: str) -> Any:
        stream = getattr(sys, name, None)
        if stream is None:
            raw = os.fdopen(os.dup(fd), mode + "b", buffering=0, closefd=True)
            stream = io.TextIOWrapper(
                raw,
                encoding="utf-8",
                errors="replace",
                newline="\n",
                line_buffering=mode == "w",
                write_through=mode == "w",
            )
            setattr(sys, name, stream)
            return stream
        try:
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
                newline="\n",
                line_buffering=mode == "w",
                write_through=mode == "w",
            )
        except (AttributeError, OSError, ValueError):
            pass
        return stream

    return (
        restore(0, "stdin", "r"),
        restore(1, "stdout", "w"),
        restore(2, "stderr", "w"),
    )


class OfflineHeadlessServer:
    """Small command owner that deliberately has no network transport."""

    def __init__(self, repo: Any) -> None:
        self.repository = repo
        self.account_id = 1
        self.gm_pvf_resources = None
        self.gm_item_commands = None
        self.gm_currency_commands = None
        self.gm_loadout_commands = None
        self.gm_profession_commands = None
        self.gm_quest_commands = None
        self.content_block_commands = None
        self.accept_quest_001f = None
        self.giveup_quest_0020 = None
        self.finish_quest_0022 = None

    def _repo(self):
        return self.repository

    @staticmethod
    def _codec_response(_selector: int, _opcode: int, _body: bytes) -> bytes:
        # Command handlers retain their validated persistence path, while the
        # offline owner discards every client projection at its codec boundary.
        return b""

    @staticmethod
    def queue_delayed_response_packets(_packets: Any) -> None:
        return None


class OfflineGmRuntime:
    """Serial command runtime with two-stage, on-demand PVF preparation."""

    def __init__(self, status_callback: Optional[Callable[..., None]] = None) -> None:
        from game.character.character_db import CharacterRepository

        database_path = Path(str(os.environ.get("USDOF_SQLITE_DB") or ""))
        pvf_path = Path(str(os.environ.get("USDOF_PVF_PATH") or ""))
        if not database_path.is_file():
            raise FileNotFoundError("离线数据库文件不存在：%s" % database_path)
        if not pvf_path.is_file():
            raise FileNotFoundError("离线 PVF 文件不存在：%s" % pvf_path)
        self._status_callback = status_callback
        self.repo = CharacterRepository(database_path)
        self.server = OfflineHeadlessServer(self.repo)
        self._commands: Dict[str, Callable[..., Any]] = {}
        self._core_ready = False
        self._catalog_ready = False
        self._command_stack_ready = False
        self._emit_status("ready", "离线数据库已就绪", 0.05)

    def _emit_status(
        self,
        state: str,
        detail: str,
        progress: float,
        *,
        error: str = "",
    ) -> None:
        callback = self._status_callback
        if callback is not None:
            callback(
                state=str(state),
                detail=str(detail),
                progress=max(0.0, min(1.0, float(progress))),
                error=str(error),
            )

    def close(self) -> None:
        resources = self.server.gm_pvf_resources
        close = getattr(resources, "close", None)
        if callable(close):
            close()

    def list_characters(self, preferred_character_id: int = 0) -> Dict[str, Any]:
        characters = []
        with self.repo.game_connection_scope():
            with self.repo.open() as conn:
                account_rows = conn.execute(
                    "SELECT account_id FROM accounts ORDER BY account_id"
                ).fetchall()
            for account_row in account_rows:
                account_id = int(account_row[0])
                for record in self.repo.list_by_account(account_id):
                    characters.append(
                        {
                            "character_id": int(record.character_id),
                            "account_id": int(record.account_id),
                            "slot_index": int(record.slot_index),
                            "name": str(record.name or ""),
                            "job": int(record.job),
                            "grow_type": int(record.grow_type),
                            "level": int(record.level),
                        }
                    )
        characters.sort(
            key=lambda row: (
                int(row["account_id"]),
                int(row["slot_index"]),
                int(row["character_id"]),
            )
        )
        valid_ids = {int(row["character_id"]) for row in characters}
        selected = int(preferred_character_id or 0)
        if selected not in valid_ids:
            selected = int(characters[0]["character_id"]) if characters else 0
        return {
            "characters": characters,
            "total": len(characters),
            "selected_character_id": selected,
        }

    def _record(self, character_id: int) -> Any:
        account_id = self.repo.get_account_id_for_character(int(character_id))
        if account_id is None:
            return None
        for record in self.repo.list_by_account(int(account_id)):
            if int(record.character_id) == int(character_id):
                return record
        return None

    def _session(self, character_id: int) -> Any:
        record = self._record(character_id)
        if record is None:
            from control.contracts import ControlApiError

            raise ControlApiError(
                "CHARACTER_NOT_FOUND",
                "离线存档中没有选中的角色",
                status=409,
            )
        self.server.account_id = int(record.account_id)
        return SimpleNamespace(
            selected_character_id=int(record.character_id),
            selected_slot=int(record.slot_index),
            account_id=int(record.account_id),
            in_dungeon_run=False,
            offline_gm=True,
            character_job=int(record.job),
            character_grow_type=int(record.grow_type),
            character_level=int(record.level),
            character_exp=max(0, int(record.exp)),
            character_expertise_exp=max(0, int(record.expertise_exp)),
            active_quest_ids=[],
            active_quest_pairs=[],
            finished_quest_ids=[],
            quest_connected=True,
            quest_hydrated=True,
            last_quest_id=None,
            account_premium_effects=None,
        )

    def _ensure_command_stack(self) -> None:
        if self._command_stack_ready:
            return
        from game.chain_loader import load_opcode
        from game.character.gm_commands import GmItemControlCommands
        from game.character.gm_currency_commands import GmCurrencyControlCommands
        from game.character.gm_loadout_commands import GmLoadoutControlCommands
        from game.character.gm_profession_commands import GmProfessionControlCommands
        from game.content_block_commands import ContentBlockControlCommands
        from game.quest.gm_commands import GmQuestControlCommands

        class OfflineLoadoutCommands(GmLoadoutControlCommands):
            @staticmethod
            def _notify_loadout_started(_loadout: Any) -> None:
                return None

            @staticmethod
            def _notify_loadout_in_progress() -> None:
                return None

            @staticmethod
            def _notify_loadout_cooldown() -> None:
                return None

        accept_module = load_opcode("opcode001f-accept_quest.py")
        giveup_module = load_opcode("opcode0020-giveup_quest.py")
        finish_module = load_opcode("opcode0022-finish_quest.py")
        self.server.accept_quest_001f = accept_module.AcceptQuest001FService(self.server)
        self.server.giveup_quest_0020 = giveup_module.GiveupQuest0020Service(self.server)
        self.server.finish_quest_0022 = finish_module.FinishQuest0022Service(self.server)
        self.server.gm_item_commands = GmItemControlCommands(self.server)
        self.server.gm_currency_commands = GmCurrencyControlCommands(self.server)
        self.server.gm_loadout_commands = OfflineLoadoutCommands(self.server)
        self.server.gm_profession_commands = GmProfessionControlCommands(self.server)
        self.server.gm_quest_commands = GmQuestControlCommands(self.server)
        self.server.content_block_commands = ContentBlockControlCommands(self.server)
        self._commands = {
            "pvf.list_items": self.server.gm_item_commands.list_items,
            "inventory.grant_item": self.server.gm_item_commands.grant_item,
            "inventory.list": self.server.gm_item_commands.list_inventory,
            "inventory.add_item": self.server.gm_item_commands.add_inventory_item,
            "inventory.delete_item": self.server.gm_item_commands.delete_inventory_item,
            "inventory.clear_scope": self.server.gm_item_commands.clear_inventory_scope,
            "currency.add_cera": self.server.gm_currency_commands.add_cera,
            "currency.add_gold": self.server.gm_currency_commands.add_gold,
            "loadout.list": self.server.gm_loadout_commands.list_loadouts,
            "loadout.save": self.server.gm_loadout_commands.save_loadout,
            "loadout.read_current": self.server.gm_loadout_commands.read_current,
            "loadout.apply": self.server.gm_loadout_commands.apply_loadout,
            "character.list_professions": self.server.gm_profession_commands.list_professions,
            "character.change_job": self.server.gm_profession_commands.change_job,
            "character.change_profession": self.server.gm_profession_commands.change_profession,
            "character.set_level": self.server.gm_profession_commands.set_level,
            "quest.list_accepted": self.server.gm_quest_commands.list_accepted,
            "quest.complete_accepted": self.server.gm_quest_commands.complete_accepted,
            "quest.giveup_accepted": self.server.gm_quest_commands.giveup_accepted,
            "quest.list": self.server.gm_quest_commands.list_quests,
            "quest.reaccept": self.server.gm_quest_commands.reaccept_finished,
            "quest.accept_unfinished": self.server.gm_quest_commands.accept_unfinished,
            "content_block.list": self.server.content_block_commands.list_rules,
            "content_block.resolve": self.server.content_block_commands.resolve_ids,
            "content_block.candidates": self.server.content_block_commands.list_candidates,
            "content_block.apply": self.server.content_block_commands.apply_rules,
            "content_block.remove": self.server.content_block_commands.remove_rules,
        }
        self._command_stack_ready = True

    @staticmethod
    def _needs_core_pvf(command: str) -> bool:
        return command in CORE_PVF_COMMANDS or command.startswith(
            CORE_PVF_COMMAND_PREFIXES
        )

    def _ensure_core_pvf(self) -> None:
        if self._core_ready:
            return
        self._emit_status("preparing", "正在加载离线 PVF 核心索引", 0.08)
        from data import preload_script_pvf_and_warmup

        completed_domains = [0]

        def domain_status(name: str, payload: Dict[str, Any]) -> None:
            completed_domains[0] += 1
            progress = min(0.62, 0.10 + completed_domains[0] * 0.018)
            state = "完成" if bool(payload.get("ok")) else "检查"
            self._emit_status(
                "preparing",
                "PVF 核心索引%s：%s" % (state, name),
                progress,
            )

        try:
            preload_script_pvf_and_warmup(
                status_callback=domain_status,
                warmup_profile="core",
            )
        except Exception as exc:
            detail = "离线 PVF 核心索引加载失败：%s" % exc
            self._emit_status("failed", detail, 1.0, error=detail)
            raise
        self._core_ready = True
        self._emit_status("ready", "离线 PVF 核心索引已就绪", 0.65)

    def _ensure_item_catalog(self) -> None:
        if self._catalog_ready:
            return
        self._ensure_core_pvf()
        self._emit_status("preparing", "正在准备离线全物品目录", 0.66)
        from control.gm_catalog_worker import (
            create_worker_request,
            execute_worker_request,
        )
        from game.character.gm_pvf_resources import GmPvfResources
        from pvf.index_cache import create_cache_context

        cache = create_cache_context(clear=False)
        resources = GmPvfResources()

        def build(progress_callback: Callable[[float, str], None]) -> Dict[str, Any]:
            request = create_worker_request(cache)

            def report(progress: float, detail: str) -> None:
                progress_callback(progress, detail)
                self._emit_status(
                    "preparing",
                    str(detail),
                    0.66 + max(0.0, min(1.0, float(progress))) * 0.32,
                )

            return execute_worker_request(request, progress_callback=report)

        try:
            resources.preload_from_server_cache(cache_builder=build)
        except Exception as exc:
            resources.close()
            detail = "离线全物品目录加载失败：%s" % exc
            self._emit_status("failed", detail, 1.0, error=detail)
            raise
        self.server.gm_pvf_resources = resources
        self._catalog_ready = True
        self._emit_status("ready", "离线数据库和 PVF 已就绪", 1.0)

    def snapshot(self, character_id: int) -> Dict[str, Any]:
        with self.repo.game_connection_scope():
            record = self._record(character_id) if int(character_id or 0) > 0 else None
        status = "ready" if self._core_ready else "database_ready"
        return {
            "health": {
                "ok": True,
                "mode": "offline",
                "state": status,
                "pvf_core_ready": bool(self._core_ready),
                "item_catalog_ready": bool(self._catalog_ready),
            },
            "session": {
                "connected": False,
                "character_ready": record is not None,
                "character_id": int(record.character_id) if record is not None else 0,
                "character_name": str(record.name or "") if record is not None else "",
                "character_level": int(record.level) if record is not None else 0,
            },
        }

    def execute(
        self,
        command: str,
        params: Dict[str, Any],
        *,
        character_id: int,
    ) -> Any:
        command = str(command or "").strip()
        if command == "native.offline.characters":
            return self.list_characters(character_id)
        if command == "snapshot":
            return self.snapshot(character_id)
        if command.startswith("dungeon."):
            from control.contracts import ControlApiError

            raise ControlApiError(
                "OFFLINE_COMMAND_UNAVAILABLE",
                "即时副本控制只在在线模式运行",
                status=409,
            )
        self._ensure_command_stack()
        handler = self._commands.get(command)
        if handler is None:
            from control.contracts import ControlApiError

            raise ControlApiError(
                "UNKNOWN_COMMAND",
                "未知离线 GM 命令：%s" % command,
                status=404,
            )
        if self._needs_core_pvf(command):
            self._ensure_core_pvf()
        if command == "pvf.list_items":
            self._ensure_item_catalog()
        with self.repo.game_connection_scope():
            session = self._session(character_id)
            _packets, result, _note = handler(dict(params or {}), session)
        if isinstance(result, dict):
            result = dict(result)
            result["packet_flow"] = []
            result["execution_mode"] = "offline_direct"
        return result


def _protocol_writer(stream: Any, payload: Dict[str, Any]) -> None:
    stream.write(
        PROTOCOL_PREFIX
        + json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    stream.flush()


def main() -> int:
    request_stream, protocol_stream, diagnostic_stream = _restore_standard_streams()

    def emit_status(**payload: Any) -> None:
        _protocol_writer(protocol_stream, {"type": "status", **payload})

    runtime = None
    try:
        # Existing PVF/domain code may print startup diagnostics. Keep stdout a
        # strict framed channel and redirect those diagnostics away from it.
        with contextlib.redirect_stdout(diagnostic_stream):
            runtime = OfflineGmRuntime(status_callback=emit_status)
        for line in request_stream:
            request_id = ""
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                request_id = str(request.get("id") or "")
                command = str(request.get("command") or "").strip()
                if command == "__shutdown__":
                    _protocol_writer(
                        protocol_stream,
                        {"type": "response", "id": request_id, "ok": True, "result": {}},
                    )
                    break
                params = request.get("params")
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                with contextlib.redirect_stdout(diagnostic_stream):
                    result = runtime.execute(
                        command,
                        params,
                        character_id=int(request.get("character_id") or 0),
                    )
                _protocol_writer(
                    protocol_stream,
                    {
                        "type": "response",
                        "id": request_id,
                        "ok": True,
                        "result": result,
                    },
                )
            except Exception as exc:
                try:
                    from control.contracts import ControlApiError
                except Exception:
                    ControlApiError = ()
                if ControlApiError and isinstance(exc, ControlApiError):
                    code = exc.code
                    message = exc.message
                    status = exc.status
                else:
                    code = "OFFLINE_COMMAND_FAILED"
                    message = "%s: %s" % (type(exc).__name__, exc)
                    status = 500
                _protocol_writer(
                    protocol_stream,
                    {
                        "type": "response",
                        "id": request_id,
                        "ok": False,
                        "error": {
                            "code": str(code),
                            "message": str(message),
                            "status": int(status),
                        },
                    },
                )
                if int(status) >= 500:
                    traceback.print_exc(file=diagnostic_stream)
    except Exception as exc:
        detail = "离线数据源初始化失败：%s: %s" % (type(exc).__name__, exc)
        _protocol_writer(
            protocol_stream,
            {
                "type": "status",
                "state": "failed",
                "detail": detail,
                "progress": 1.0,
                "error": detail,
            },
        )
        traceback.print_exc(file=diagnostic_stream)
        return 2
    finally:
        if runtime is not None:
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CORE_PVF_COMMAND_PREFIXES",
    "CORE_PVF_COMMANDS",
    "OfflineGmRuntime",
    "OfflineHeadlessServer",
    "PROTOCOL_PREFIX",
    "_restore_standard_streams",
    "main",
]
