from __future__ import annotations

import socket
import struct
import threading
import os
import hashlib
import time
import sys
from datetime import datetime
from pathlib import Path

from config import (
    CAPTURE_DIR,
    CHANNEL_PORT,
    CHANNEL_RESPONSE_FLAG,
    DOC_DIR,
    GAME_PORT,
    LOCAL_HOST,
    LOG_DIR,
    PVF_PATH,
    SCRIPT_RESPONSE_OPCODE,
    ensure_dirs,
)
from channel.channel_crypto import build_encrypted_packed_payload
from channel.channel_diary import ChannelConnectionDiary
from channel.channel_info import advertise_game_ports, build_channelinfo_plaintext_by_profile
from channel.channel_script import build_script_plaintext
from channel.channel_wire import (
    ChannelHeader,
    build_channel_packet,
    describe_header,
    parse_channel_header,
    read_channel_packet,
)
from game.chain_loader import load_chain
from login.session_registry import get_login_registry, is_loopback_address
process_enter_channel_chain = load_chain('flow-enter_channel.py', area="channel").process_enter_channel_chain
_chain_channel_script_response_reason = load_chain('flow-script_decision.py', area="channel").channel_script_response_reason
from packet_sequence_log import build_packet_extra, get_packet_sequence_logger


CHANNELINFO_ENV_CACHE_KEYS = (
    "USDOF_CHANNELINFO_GAME_IP_TEXT",
    "USDOF_CHANNELINFO_GAME_IP_LENGTH",
    "USDOF_CHANNELINFO_MAX_USERS",
    "USDOF_CHANNELINFO_CUR_USERS",
    "USDOF_CHANNELINFO_MAX_CHANNELS",
    "USDOF_CHANNELINFO_MAX_GROUPS",
    "USDOF_CHANNELINFO_MAX_CHANNELS_PER_GROUP",
    "USDOF_CHANNELINFO_REGION_NAME",
    "USDOF_CHANNELINFO_CHANNEL_NAME",
    "USDOF_CHANNELINFO_USER_FIELD_ORDER",
    "USDOF_CHANNELINFO_FIRST_CHANNEL_ID",
    "USDOF_CHANNELINFO_INCLUDE_KINDS",
    "USDOF_CHANNELINFO_EXCLUDE_KINDS",
    "USDOF_CHANNELINFO_INCLUDE_GROUPS",
    "USDOF_CHANNELINFO_EXCLUDE_GROUPS",
    "USDOF_CHANNELINFO_APPEND_CHANNEL_ID",
)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() not in {"0", "false", "no", "off", "n"}


def channel_script_response_reason(
    followups: list[int] | tuple[int, ...],
    *,
    implicit_on_channelinfo: bool | None = None,
) -> str | None:
    """Return why the Channel server should send opcode 0x09 script.

    The normal cold-start Channel pull sends ``00 0B -> 00 09 -> 00 01``.
    Clicking the channel button from the character-select page has been
    observed to reconnect with only ``00 0B -> 00 01``.  Dynamic x32dbg trace
    on 2026-07-08 showed why: the current EXE's ``sub_15AB3E0`` skips sending
    ``00 09`` when its script cache is already loaded, and ``sub_15AB9D0``
    then also skips reading a 7C09 script response.  Injecting an implicit
    7C09 in this state shifts the next packet away from the 7C03 ChannelInfo
    that ``sub_15AC610`` expects, making ``sub_15AD860`` fail and the wrapper
    show localized prompt 0x1B59.  Therefore the source default only sends
    7C09 when the client explicitly requested opcode 00 09.
    """

    return _chain_channel_script_response_reason(
        followups,
        implicit_on_channelinfo=implicit_on_channelinfo,
    )


class ChannelServer:
    def __init__(
        self,
        host: str = LOCAL_HOST,
        port: int = CHANNEL_PORT,
        *,
        game_host: str = LOCAL_HOST,
        game_port: int = GAME_PORT,
        prefer_pvf_script: bool = False,
        pvf_path: str = PVF_PATH,
        channelinfo_profile: str | None = None,
        script_profile: str | None = None,
        channelinfo_envelope: str | None = None,
        response_flag: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.game_host = game_host
        self.game_port = game_port
        self.prefer_pvf_script = prefer_pvf_script
        self.pvf_path = pvf_path
        self.channelinfo_profile = channelinfo_profile or os.environ.get("USDOF_CHANNELINFO_PROFILE") or "cain-pvf-paired"
        self.channelinfo_envelope = channelinfo_envelope or os.environ.get("USDOF_CHANNELINFO_ENVELOPE")
        self.script_profile = script_profile or os.environ.get("USDOF_SCRIPT_PROFILE") or "cain-pvf-paired"
        self.response_flag = (
            int(os.environ.get("USDOF_CHANNEL_RESPONSE_FLAG", str(CHANNEL_RESPONSE_FLAG)), 0) & 0xFF
            if response_flag is None
            else response_flag & 0xFF
        )
        ensure_dirs()
        self.internal_protocol_log = _env_bool(
            "USDOF_INTERNAL_PROTOCOL_LOG", True
        )
        self.log_path = (
            LOG_DIR / f"channel_server_{_timestamp()}.log"
            if self.internal_protocol_log
            else None
        )
        self.protocol_traffic_log = self.internal_protocol_log and _env_bool(
            "USDOF_PROTOCOL_TRAFFIC_LOG", True
        )
        self.packet_sequence_logger = (
            get_packet_sequence_logger() if self.internal_protocol_log else None
        )
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self.legacy_game_ports: dict[int, int] = {}
        self._channelinfo_plain_cache: dict[tuple, bytes] = {}
        self._cache_lock = threading.Lock()
        self._login_identity = threading.local()

    def log(self, msg: str) -> None:
        if not self.internal_protocol_log:
            return
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        console_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        console_line = line.encode(console_encoding, errors="replace").decode(console_encoding, errors="replace")
        print(console_line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def log_traffic(self, msg: str) -> None:
        if self.protocol_traffic_log:
            self.log(msg)

    def serve_forever(self) -> None:
        self._preload_script_pvf()
        self._prewarm_channelinfo()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((self.host, self.port))
                self.port = int(server.getsockname()[1])
                server.listen(16)
                server.settimeout(1.0)
                self._ready.set()
                self.log(
                    f"[Channel] listening on {self.host}:{self.port} "
                    f"response_flag=0x{self.response_flag:02X}"
                )
                if self.packet_sequence_logger is not None:
                    self.log(f"[PacketSeq] enabled dir={self.packet_sequence_logger.log_dir}")
                while not self._stop.is_set():
                    try:
                        client, addr = server.accept()
                    except socket.timeout:
                        continue
                    thread = threading.Thread(target=self.handle_client, args=(client, addr), daemon=True)
                    thread.start()
        finally:
            self._stopped.set()

    def stop(self) -> None:
        self._stop.set()

    def wait_ready(self, timeout: float) -> bool:
        return bool(self._ready.wait(max(0.0, float(timeout))))

    def effective_game_host(self) -> str:
        from login.native_tunnel import local_game_endpoints
        if local_game_endpoints():return "127.0.0.1"
        identity = getattr(self._login_identity, "value", None)
        return str(getattr(identity, "advertise_host", "") or self.game_host)

    def _build_channelinfo_plain(self) -> bytes:
        profile = self.channelinfo_profile
        envelope = self.channelinfo_envelope or "none"
        game_host = self.effective_game_host()
        env_sig = "|".join(f"{name}={os.environ.get(name, '')}" for name in CHANNELINFO_ENV_CACHE_KEYS)
        identity = getattr(self._login_identity, "value", None)
        mapping = self.legacy_game_ports if identity is not None and not identity.device_id else None
        key = (profile, envelope, game_host, int(self.game_port), str(self.pvf_path), env_sig,
               tuple(mapping.items()) if mapping is not None else None)
        with self._cache_lock:
            cached = self._channelinfo_plain_cache.get(key)
        if cached is not None:
            return cached
        with advertise_game_ports(mapping, game_host):
            plain = build_channelinfo_plaintext_by_profile(
                game_ip_text=game_host,
                game_port=self.game_port,
                pvf_path=self.pvf_path,
                profile=profile,
                envelope=self.channelinfo_envelope,
            )
        with self._cache_lock:
            self._channelinfo_plain_cache[key] = plain
        return plain

    def _preload_script_pvf(self) -> None:
        # Normal start.py has already prepared and frozen the shared selective
        # reader.  A standalone Channel process retains the legacy full
        # fallback, while launcher quick mode stays explicitly on-demand.
        try:
            from pvf import pvf_runtime
        except Exception as exc:
            self.log(f"[Channel] Script.pvf preload import failed: {type(exc).__name__}: {exc}")
            return
        try:
            boot_mode = os.environ.get("USDOF_PVF_BOOT_MODE", "").strip().lower()
            quick = boot_mode == "quick"
            if quick:
                stats = pvf_runtime.open_script_pvf_on_demand(self.pvf_path)
                self.log(
                    f"[Channel] quick-start Script.pvf reader ready path={stats.path} "
                    f"profile={stats.profile_name} sha256={stats.source_sha256} "
                    f"groups={stats.group_count} group_cache=16 elapsed_ms={stats.elapsed_ms}"
                )
                return
            if boot_mode == "lazy" and pvf_runtime.is_memory_prepared(self.pvf_path):
                stats = pvf_runtime.last_preload_stats()
                self.log(
                    f"[Channel] lazy Script.pvf shared reader ready path={self.pvf_path} "
                    f"profile={getattr(stats, 'profile_name', 'unknown')} "
                    "domains=background"
                )
                return
            if boot_mode == "lazy":
                self.log("[Channel] lazy Script.pvf disk reader ready; background memory preparation pending")
                return
            if pvf_runtime.is_runtime_ready(self.pvf_path):
                stats = pvf_runtime.last_preload_stats()
                if stats is not None:
                    self.log(
                        f"[Channel] reusing Script.pvf runtime path={stats.path} "
                        f"group_residency={stats.group_residency} "
                        f"resident_groups={stats.resident_group_count}/{stats.group_count}"
                    )
                return
            stats = pvf_runtime.preload_script_pvf(self.pvf_path)
            self.log(
                f"[Channel] preloaded Script.pvf path={stats.path} "
                f"profile={stats.profile_name} guarded={int(stats.guarded)} "
                f"sha256={stats.source_sha256} "
                f"file_mb={stats.file_size / (1024 * 1024):.1f} files={stats.file_count} "
                f"groups={stats.group_count} groups_preloaded={int(stats.groups_preloaded)} "
                f"decompressed_mb={stats.decompressed_bytes / (1024 * 1024):.1f} "
                f"memory_backed={int(stats.memory_backed)} elapsed_ms={stats.elapsed_ms}"
            )
        except Exception as exc:
            self.log(f"[Channel] Script.pvf preload failed: {type(exc).__name__}: {exc}")

    def _prewarm_channelinfo(self) -> None:
        # First PVF open can take several seconds.  Prewarming at server start
        # keeps the user's first UI Refresh from looking like a receive hang.
        disabled = (
            os.environ.get("USDOF_DISABLE_CHANNELINFO_PREWARM", "").lower()
            in {"1", "true", "yes", "on"}
            or os.environ.get("USDOF_PVF_BOOT_MODE", "").strip().lower() in {"quick", "lazy"}
        )
        if disabled:
            boot_mode = os.environ.get("USDOF_PVF_BOOT_MODE", "").strip().lower()
            if boot_mode in {"quick", "lazy"}:
                self.log(f"[Channel] ChannelInfo prewarm deferred by PVF {boot_mode} mode")
            else:
                self.log("[Channel] ChannelInfo prewarm disabled by USDOF_DISABLE_CHANNELINFO_PREWARM")
            return
        started = time.perf_counter()
        try:
            plain = self._build_channelinfo_plain()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.log(
                f"[Channel] prewarmed ChannelInfo profile={self.channelinfo_profile} "
                f"envelope={self.channelinfo_envelope or 'none'} plain={len(plain)} elapsed_ms={elapsed_ms}"
            )
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.log(f"[Channel] ChannelInfo prewarm failed elapsed_ms={elapsed_ms}: {type(exc).__name__}: {exc}")

    def _capture_packet(self, prefix: str, label: str, header: ChannelHeader, body: bytes) -> None:
        (CAPTURE_DIR / f"{prefix}_{label}_header.bin").write_bytes(header.raw)
        if body:
            (CAPTURE_DIR / f"{prefix}_{label}_body.bin").write_bytes(body)
        (CAPTURE_DIR / f"{prefix}_{label}.bin").write_bytes(header.raw + body)

    def _log_channel_sequence(
        self,
        *,
        direction: str,
        connection_id: str,
        header: ChannelHeader,
        wire_body: bytes,
        plain_body: bytes | None = None,
        semantic: str = "",
        note: str = "",
    ) -> None:
        if self.packet_sequence_logger is None:
            return
        if plain_body is None:
            plain_body = wire_body
        seq = self.packet_sequence_logger.log_packet(
            protocol="channel",
            direction=direction,
            connection_id=connection_id,
            raw=header.raw + wire_body,
            plain_body=plain_body,
            flag=header.flag,
            opcode=header.opcode,
            total_len=header.length,
            header_len=len(header.raw),
            wire_body_len=len(wire_body),
            tail_or_seq=header.tail_or_version,
            semantic=semantic,
            note=note,
            extra=build_packet_extra(
                protocol="channel",
                direction=direction,
                flag=header.flag,
                opcode=header.opcode,
                plain_body=plain_body,
                semantic=semantic,
                note=note,
            ),
        )
        if seq is not None:
            self.log(
                f"[PacketSeq] seq={seq:06d} channel {direction} "
                f"opcode=0x{header.opcode:02X} plain={len(plain_body)} raw={header.length}"
            )

    def _log_channel_sequence_packet(
        self,
        *,
        direction: str,
        connection_id: str,
        packet: bytes,
        plain_body: bytes | None = None,
        semantic: str = "",
        note: str = "",
    ) -> None:
        if self.packet_sequence_logger is None:
            return
        header = parse_channel_header(packet[:0x0B])
        self._log_channel_sequence(
            direction=direction,
            connection_id=connection_id,
            header=header,
            wire_body=packet[0x0B:header.length],
            plain_body=plain_body,
            semantic=semantic,
            note=note,
        )

    def _recv_expected(
        self,
        sock: socket.socket,
        prefix: str,
        opcode: int,
        label: str,
        *,
        diary: ChannelConnectionDiary | None = None,
    ) -> tuple[ChannelHeader, bytes]:
        header, body = read_channel_packet(sock)
        connection_id = prefix
        self._log_channel_sequence(
            direction="C->S",
            connection_id=connection_id,
            header=header,
            wire_body=body,
            semantic=f"request_{label}",
        )
        if diary is not None:
            diary.record_request(label, header, body)
        else:
            self._capture_packet(prefix, label, header, body)
        if header.opcode != opcode:
            self.log(f"[Channel] recv unexpected opcode for {label}: {describe_header(header)} expected=0x{opcode:02x}")
        else:
            self.log_traffic(
                f"[Channel] recv 00 {opcode:02X}: {describe_header(header)} "
                f"body_sha256={hashlib.sha256(body).hexdigest()[:16]}"
            )
        return header, body

    @staticmethod
    def build_server_key_packet(*, response_flag: int = CHANNEL_RESPONSE_FLAG) -> bytes:
        from config import CHANNEL_SESSION_KEY

        payload = struct.pack("<I", 0) + CHANNEL_SESSION_KEY
        return build_channel_packet(0x0C, payload, flag=response_flag)

    def handle_client(self, client: socket.socket, addr) -> None:
        from login.native_tunnel import identify, enter_tunnel, leave_tunnel
        try:identity,tunneled=identify(client,addr[0],get_login_registry())
        except (ValueError,OSError):
            client.close()
            return
        require_external = _env_bool("USDOF_LOGIN_REQUIRE_EXTERNAL_SESSION", True)
        if identity is None and require_external and not is_loopback_address(addr[0]):
            self.log(
                "[Channel/Auth] rejected external client without HTTP login "
                f"remote={addr[0]}:{addr[1]}"
            )
            try:
                client.close()
            except OSError:
                pass
            return
        if identity is not None:
            self.log(
                "[Channel/Auth] bound HTTP login "
                f"account_id={identity.account_id} m_id={identity.account_m_id!r} "
                f"session={identity.login_session_id[:12]}"
            )
        self._login_identity.value = identity
        tunnel_context=enter_tunnel(tunneled)
        try:
            # The refresh flow builds its own ChannelInfo on the same connection.
            mapping = self.legacy_game_ports if identity is not None and not tunneled else None
            with advertise_game_ports(mapping, self.effective_game_host()):
                self._handle_client_bound(client, addr)
        finally:
            leave_tunnel(tunnel_context)
            try:
                del self._login_identity.value
            except AttributeError:
                pass

    def _handle_client_bound(self, client: socket.socket, addr) -> None:
        prefix = f"channel_{_timestamp()}_{addr[0].replace('.', '_')}_{addr[1]}"
        effective_game_host = self.effective_game_host()
        diary = ChannelConnectionDiary(
            prefix=prefix,
            remote=addr,
            capture_dir=CAPTURE_DIR,
            log_dir=LOG_DIR,
            doc_dir=DOC_DIR,
            write_artifacts=self.protocol_traffic_log,
            context={
                "listen": f"{self.host}:{self.port}",
                "game": f"{effective_game_host}:{self.game_port}",
                "response_flag": f"0x{self.response_flag:02X}",
                "script_profile": self.script_profile or ("pvf-text" if self.prefer_pvf_script else "cain-pvf-paired"),
                "channelinfo_profile": self.channelinfo_profile,
                "channelinfo_envelope": self.channelinfo_envelope or "none",
                "channelinfo_game_ip_text": (
                    os.environ.get("USDOF_CHANNELINFO_GAME_IP_TEXT")
                    or (
                        f"length:{os.environ.get('USDOF_CHANNELINFO_GAME_IP_LENGTH', '')}"
                        if os.environ.get("USDOF_CHANNELINFO_GAME_IP_LENGTH")
                        else effective_game_host
                    )
                ),
                "channelinfo_user_field_order": os.environ.get("USDOF_CHANNELINFO_USER_FIELD_ORDER", "max-cur"),
                "channelinfo_first_channel_id": os.environ.get("USDOF_CHANNELINFO_FIRST_CHANNEL_ID", ""),
                "channelinfo_include_kinds": os.environ.get("USDOF_CHANNELINFO_INCLUDE_KINDS", ""),
                "pvf_path": self.pvf_path,
            },
        )
        with client:
            client.settimeout(15.0)
            self.log(f"[Channel] accepted client from {addr[0]}:{addr[1]}")
            try:
                # ChannelServer 只保留 socket 入口、拆包日志和链路调度。
                # 进入频道链路在 channel/chains/flow-enter_channel.py；
                # 刷新频道列表 00 11 在 channel/chains/flow-refresh_channel_list.py。
                process_enter_channel_chain(self, client, prefix, diary)
                json_path, md_path = diary.finish(status="ok")
                if self.protocol_traffic_log:
                    self.log(f"[Channel] diary written json={json_path} md={md_path}")
            except Exception as exc:
                try:
                    json_path, md_path = diary.finish(status="error", error=f"{type(exc).__name__}: {exc}")
                    if self.protocol_traffic_log:
                        self.log(f"[Channel] diary written json={json_path} md={md_path}")
                except Exception as diary_exc:
                    self.log(f"[Channel] diary write error: {type(diary_exc).__name__}: {diary_exc}")
                self.log(f"[Channel] client handler error: {type(exc).__name__}: {exc}")
