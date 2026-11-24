# -*- coding: utf-8 -*-
"""USDOF 本地模拟服务端总入口。

启动（必须 CPython 3.12.10 x64，与 Win10/Win11 分享包一致）::

    # 推荐：仓库内 Win10 构建缓存
    server-pack-win10\\cache\\python-3.12.10-full\\python.exe server_proto\\start.py
    # 或任意入口；版本不匹配时自动 re-exec 到仓库内 3.12.10
    python server_proto/start.py

流程：版本门禁 → standard_local_flow env → PVF preload/warmup（或 Win10 快速按需模式）
→ 冒险团原生提示同步 → Channel + Game → 额外端口拦截。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
from pathlib import Path

_PROCESS_ENTRY_NS = time.perf_counter_ns()

# --- 路径 + 版本门禁必须先于业务 import（embed ._pth 不带脚本目录）---
_SERVER_DIR = Path(__file__).resolve().parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from python_runtime import (  # noqa: E402
    describe_runtime,
    ensure_server_dir_on_sys_path,
    ensure_server_python,
    harden_stdio,
    safe_print,
)

ensure_server_dir_on_sys_path()
harden_stdio()
# 非 3.12.10 在此 re-exec；通过后才加载业务模块。
if __name__ == "__main__":
    ensure_server_python(reexec=True)
    ensure_server_dir_on_sys_path()
    harden_stdio()

from channel.channel_info import CHANNELINFO_ENVELOPES, CHANNELINFO_PROFILES  # noqa: E402
from channel.channel_script import SCRIPT_PROFILES  # noqa: E402
from channel.channel_server import ChannelServer  # noqa: E402
from config import (  # noqa: E402
    CHANNEL_PORT,
    CONTROL_HOST,
    CONTROL_PORT,
    DEFAULT_CHANNELINFO_PROFILE,
    DEFAULT_EXTRA_TCP_PORTS,
    DEFAULT_EXTRA_UDP_PORTS,
    DEFAULT_SCRIPT_PROFILE,
    GAME_PORT,
    LOGIN_HOST,
    LOGIN_PORT,
    LOCAL_HOST,
    LOG_DIR,
    PVF_PATH,
    SERVER_HOST,
    ensure_dirs,
)
from game.game_server import GameServer, parse_plaintext_probe_specs, parse_probe_specs  # noqa: E402
from control.gm_server import GmApiServer  # noqa: E402
from login.login_server import LoginApiServer  # noqa: E402
from net_interceptor import PortInterceptorManager, parse_port_list  # noqa: E402
from game.raid.native_udp import native_udp_handler_for_game  # noqa: E402
from standard_local_flow import apply_standard_local_flow_env, describe_standard_local_flow  # noqa: E402
from startup_log import format_init_log  # noqa: E402
from startup_metrics import ServerStartupMetrics  # noqa: E402
from pvf.town_structure_preprocessor import (  # noqa: E402
    format_town_preprocess_result,
    preprocess_town_structure,
)


def log_pvf_preload_failure(exc: BaseException) -> None:
    """Keep the original PVF parse/decode failure visible at the boot boundary."""
    source = Path(PVF_PATH)
    safe_print("[PVF] source file: %s" % source, flush=True)
    try:
        stat = source.stat()
        safe_print("[PVF] source bytes: %s" % stat.st_size, flush=True)
    except OSError as stat_exc:
        safe_print(
            "[PVF] source file unavailable: %s: %s"
            % (type(stat_exc).__name__, stat_exc),
            flush=True,
        )
    safe_print(
        "[PVF] preload/warmup failed: %s: %s" % (type(exc).__name__, exc),
        flush=True,
    )
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    if detail:
        safe_print("[PVF] preload/warmup traceback:\n%s" % detail, flush=True)


def pvf_boot_mode() -> str:
    """Return the launcher-selected PVF boot mode.

    ``lazy`` binds listeners after the verified shared reader is ready and
    finishes the core runtime indexes in the background. ``core`` blocks on
    the same player-runtime domains, ``full`` includes the GM catalog, and
    ``quick`` remains the explicit PVF editing mode.
    """
    raw = os.environ.get("USDOF_PVF_BOOT_MODE", "lazy").strip().lower()
    if raw in ("", "lazy"):
        return "lazy"
    if raw == "full":
        return "full"
    if raw == "core":
        return "core"
    if raw == "quick":
        return "quick"
    raise ValueError("invalid USDOF_PVF_BOOT_MODE: %s" % raw)


def preprocess_startup_pvf() -> None:
    """Normalize the selected PVF before any profile detection or preload."""
    expected_sha = os.environ.get("USDOF_EXPECTED_PVF_SHA256", "").strip().lower()
    prepared_sha = os.environ.get(
        "USDOF_TOWN_STRUCTURE_PREPROCESSED_SHA256", ""
    ).strip().lower()
    if expected_sha and prepared_sha == expected_sha:
        safe_print(
            "[PVF] town preprocessor context=server-start "
            "verified_by_launcher=1 sha256=%s" % expected_sha,
            flush=True,
        )
        return
    result = preprocess_town_structure(PVF_PATH)
    safe_print(format_town_preprocess_result(result, "server-start"), flush=True)
    if not expected_sha:
        os.environ["USDOF_EXPECTED_PVF_SHA256"] = result.source_sha256
    os.environ["USDOF_TOWN_STRUCTURE_PREPROCESSED_SHA256"] = result.source_sha256


def wait_for_services_ready(
    services,
    metrics: ServerStartupMetrics,
    *,
    timeout: float,
) -> bool:
    """Wait for actual bind/listen completion and record each service once."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    for name, service in services:
        remaining = max(0.0, deadline - time.monotonic())
        wait_ready = getattr(service, "wait_ready", None)
        if wait_ready is None or not wait_ready(remaining):
            metrics.record(
                "services_ready_failed",
                service=str(name),
                timeout_seconds=float(timeout),
            )
            return False
        fields = {
            "host": str(getattr(service, "host", "")),
            "port": int(getattr(service, "port", 0) or 0),
        }
        listen_ports = getattr(service, "listen_ports", None)
        if listen_ports is not None:
            fields["listen_ports"] = [int(port) for port in listen_ports]
        metrics.record("%s_listening" % name, **fields)
    return True


def main() -> int:
    # 被 import 调用时也校验版本（channel.py / game.py 转调）
    ensure_server_python(reexec=True)
    ensure_server_dir_on_sys_path()
    harden_stdio()
    apply_standard_local_flow_env()
    startup_metrics = ServerStartupMetrics(
        _SERVER_DIR,
        process_entry_ns=_PROCESS_ENTRY_NS,
    )
    startup_metrics.record_at("process_entry", _PROCESS_ENTRY_NS)
    startup_metrics.record(
        "python_gate_ok",
        implementation=sys.implementation.name,
        version="%d.%d.%d" % sys.version_info[:3],
        bits=64 if sys.maxsize > 2**32 else 32,
    )
    safe_print("[python] runtime %s" % describe_runtime(), flush=True)
    safe_print("[standard] %s" % describe_standard_local_flow(), flush=True)
    runtime_bits = 64 if sys.maxsize > 2**32 else 32
    safe_print(
        format_init_log(
            "USDOF Python Runtime",
            "CPython %d.%d.%d x%s" % (*sys.version_info[:3], runtime_bits),
            success=True,
        ),
        flush=True,
    )
    parser = argparse.ArgumentParser(description="USDOF Stage3 local Channel/Game server prototype")
    parser.add_argument("--host", default=SERVER_HOST)
    parser.add_argument(
        "--advertise-host",
        default=os.environ.get("USDOF_LOGIN_ADVERTISE_HOST", LOCAL_HOST),
        help="IPv4 serialized into ChannelInfo when no HTTP login session overrides it",
    )
    parser.add_argument("--channel-port", type=int, default=CHANNEL_PORT)
    parser.add_argument("--game-port", type=int, default=GAME_PORT)
    parser.add_argument("--control-host", default=CONTROL_HOST)
    parser.add_argument("--control-port", type=int, default=CONTROL_PORT)
    parser.add_argument("--disable-control", action="store_true")
    parser.add_argument("--login-host", default=LOGIN_HOST)
    parser.add_argument("--login-port", type=int, default=LOGIN_PORT)
    parser.add_argument("--disable-login", action="store_true")
    parser.add_argument(
        "--prefer-pvf-script",
        action="store_true",
        help="try to load channel_info.etc from PVF for script response",
    )
    parser.add_argument(
        "--script-profile",
        default=os.environ.get("USDOF_SCRIPT_PROFILE", DEFAULT_SCRIPT_PROFILE),
        choices=SCRIPT_PROFILES,
        help="script response plaintext profile",
    )
    parser.add_argument(
        "--channelinfo-profile",
        default=os.environ.get("USDOF_CHANNELINFO_PROFILE", DEFAULT_CHANNELINFO_PROFILE),
        choices=CHANNELINFO_PROFILES,
        help="ChannelInfo plaintext profile",
    )
    parser.add_argument(
        "--channelinfo-envelope",
        default=None,
        choices=CHANNELINFO_ENVELOPES,
        help="optional ChannelInfo plaintext envelope for A/B tracing",
    )
    parser.add_argument(
        "--game-probes",
        default=None,
        help="comma-separated Game server-first probes",
    )
    parser.add_argument(
        "--game-flow-profile",
        default=None,
        choices=["enter-game-p1"],
        help="preconfigure Game-stage follow-ups (enter-game-p1 enables DB USERINFO)",
    )
    parser.add_argument("--game-ack-responses", default=None)
    parser.add_argument("--game-ack-plaintext-responses", default=None)
    parser.add_argument("--game-piv-responses", default=None)
    parser.add_argument("--game-piv-plaintext-responses", default=None)
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite inventory database path. Default: server_proto/data/inventory_usdof_local.db",
    )
    parser.add_argument(
        "--game-roster-profile",
        default=os.environ.get("USDOF_USERINFO_ROSTER_PROFILE", "us-trace-dstr"),
        choices=[
            "us-trace-entry-full",
            "us-trace-entry-prefix",
            "us-trace-dstr",
            "us-trace-fixed",
            "s4a12-body",
        ],
    )
    parser.add_argument("--game-db-userinfo", action="store_true")
    parser.add_argument("--channel-only", action="store_true")
    parser.add_argument("--game-only", action="store_true")
    parser.add_argument("--extra-tcp-ports", default=None)
    parser.add_argument("--extra-udp-ports", default=None)
    args = parser.parse_args()
    safe_print(
        format_init_log(
            "USDOF Standard Profile",
            "%s / %s" % (args.script_profile, args.channelinfo_profile),
            success=True,
        ),
        flush=True,
    )

    ensure_dirs()
    lazy_warmup_thread = None
    lazy_warmup_target = None
    lazy_warmup_failed = threading.Event()
    try:
        from native_core_loader import (
            configure_native_core_logger,
            native_core_status,
        )

        core_status = native_core_status()
        configure_native_core_logger(lambda line: safe_print(line, flush=True))
        startup_metrics.record("native_core_gate_ok", **core_status)
        safe_print(
            format_init_log(
                "USDOF Native Core",
                "%s / ABI %s / %s"
                % (
                    core_status["mode"],
                    core_status.get("abi_version", "python"),
                    core_status.get("build_version", "not-loaded"),
                ),
                success=True,
            ),
            flush=True,
        )
        mode = pvf_boot_mode()
        pvf_source = Path(PVF_PATH)
        pvf_profile = os.environ.get("USDOF_EXPECTED_PVF_PROFILE", "").strip()
        pvf_detail = "%s / %s" % (pvf_profile or "auto", mode)
        safe_print(format_init_log("Script.pvf", pvf_detail), flush=True)
        from pvf.native_loader import native_module_status

        native_status = native_module_status()
        startup_metrics.record("native_gate_ok", **native_status)
        if os.environ.get("USDOF_SKIP_PVF_STARTUP_PREPROCESS", "0").strip() != "1":
            startup_metrics.record("pvf_preprocess_begin", path=str(pvf_source))
            preprocess_startup_pvf()
            startup_metrics.record("pvf_preprocess_end", path=str(pvf_source))
        else:
            safe_print("[PVF] startup preprocessor skipped by share launcher", flush=True)
        from data import (
            finish_lazy_script_pvf_warmup,
            format_pvf_preload_line,
            format_pvf_quick_start_line,
            open_script_pvf_for_quick_start,
            prepare_script_pvf_for_lazy_start,
            preload_script_pvf_and_warmup,
        )

        def log_pvf_domain_status(name, payload):
            safe_print(format_pvf_domain_status_line(name, payload), flush=True)

        def log_pvf_timing_event(event, payload):
            startup_metrics.record(event, **dict(payload))

        safe_print("[PVF] loading source file: %s" % pvf_source, flush=True)
        try:
            safe_print("[PVF] loading source bytes: %s" % pvf_source.stat().st_size, flush=True)
        except OSError as stat_exc:
            safe_print(
                "[PVF] loading source unavailable: %s: %s"
                % (type(stat_exc).__name__, stat_exc),
                flush=True,
            )

        if mode == "quick":
            stats = open_script_pvf_for_quick_start()
            warm = {"ok": 1, "profile": "quick", "domains": {}}
            print(format_pvf_quick_start_line(stats), flush=True)
            safe_print(
                format_init_log("Script.pvf", pvf_detail, success=True),
                flush=True,
            )
            safe_print(
                format_init_log(
                    "PVF On-Demand Reader",
                    pvf_profile or "auto",
                    success=True,
                ),
                flush=True,
            )
            print(
                "[PVF] quick-start mode: domain indexes, drop self-check, and "
                "default-unlock backfill are deferred",
                flush=True,
            )
        elif mode == "lazy":
            from pvf import boot_state
            from pvf.boot_warmup import format_pvf_domain_status_line, format_warmup_report

            boot_state.reset()
            stats = prepare_script_pvf_for_lazy_start()
            warm = {"ok": 0, "profile": "core", "domains": {}}
            print(format_pvf_preload_line(stats), flush=True)
            safe_print(
                format_init_log("Script.pvf", pvf_detail, success=True),
                flush=True,
            )
            safe_print(
                format_init_log(
                    "PVF Lazy Reader",
                    "%s / listeners-first" % (pvf_profile or "auto"),
                    success=True,
                ),
                flush=True,
            )
            print(
                "[PVF] lazy mode: listeners start now; native parallel group "
                "load and core domain warmup continue in background",
                flush=True,
            )

            def run_lazy_warmup():
                try:
                    worker_count = int(os.environ.get("USDOF_PVF_LOAD_WORKERS", "0") or 0)
                    if worker_count <= 0:
                        worker_count = min(32, max(2, os.cpu_count() or 2))
                    startup_metrics.record(
                        "pvf_lazy_background_begin",
                        workers=worker_count,
                        profile="core",
                    )
                    safe_print(
                        "[PVF] lazy background begin workers=%d profile=core"
                        % worker_count,
                        flush=True,
                    )
                    lazy_stats, lazy_warm = finish_lazy_script_pvf_warmup(
                        status_callback=log_pvf_domain_status,
                        timing_callback=log_pvf_timing_event,
                        warmup_profile="core",
                    )
                    from game.drop.drop_warmup import is_drop_index_ready

                    if not is_drop_index_ready():
                        raise RuntimeError("drop_index self-check failed after lazy warmup")
                    from game.character.character_db import CharacterRepository
                    from game.character.new_character_contract import (
                        backfill_new_character_contracts,
                    )

                    character_repository = CharacterRepository(args.db_path)
                    contracts = backfill_new_character_contracts(character_repository)
                    safe_print(
                        "[NEW_CHARACTER_CONTRACT] backfill characters=%s contracts_upgraded=%s"
                        % (
                            contracts["characters"],
                            contracts["contracts_upgraded"],
                        ),
                        flush=True,
                    )
                    if not args.channel_only:
                        from data import CLIENT_DIR
                        from game.character.character_db import DEFAULT_ACCOUNT_ID
                        from startup_adventure_buff_dstr import (
                            sync_adventure_buff_tooltip_or_default,
                        )

                        account_id = int(
                            os.environ.get("USDOF_ACCOUNT_ID", str(DEFAULT_ACCOUNT_ID)),
                            0,
                        )
                        adventure_tooltip = sync_adventure_buff_tooltip_or_default(
                            Path(CLIENT_DIR) / "dstr.dat",
                            character_repository,
                            account_id,
                        )
                        safe_print(
                            "[ADVENTURE_BUFF] %s" % adventure_tooltip.describe(),
                            flush=True,
                        )
                    safe_print(format_pvf_preload_line(lazy_stats), flush=True)
                    safe_print(
                        "[PVF] lazy background warmup %s"
                        % format_warmup_report(lazy_warm),
                        flush=True,
                    )
                    startup_metrics.record(
                        "pvf_lazy_background_ready",
                        workers=worker_count,
                        domain_count=len(lazy_warm.get("domains") or {}),
                    )
                    boot_state.mark_ready()
                    safe_print(
                        format_init_log(
                            "PVF Runtime Index",
                            "%s Domains / Background Ready"
                            % len(lazy_warm.get("domains") or {}),
                            success=True,
                        ),
                        flush=True,
                    )
                except Exception as exc:
                    detail = "%s: %s" % (type(exc).__name__, exc)
                    startup_metrics.record("pvf_lazy_background_failed", error=detail)
                    boot_state.mark_failed(detail)
                    lazy_warmup_failed.set()
                    log_pvf_preload_failure(exc)

            lazy_warmup_target = run_lazy_warmup
        else:
            from game.drop.drop_warmup import is_drop_index_ready
            from pvf.boot_warmup import format_pvf_domain_status_line, format_warmup_report

            # 开服硬顺序：共享内存 PVF 结构 -> 必需领域预热（含 drop_index）
            # drop 失败与 quest/item 一样 hard-fail，禁止半开服接客户端。
            stats, warm = preload_script_pvf_and_warmup(
                status_callback=log_pvf_domain_status,
                timing_callback=log_pvf_timing_event,
                warmup_profile=mode,
            )
            print(format_pvf_preload_line(stats), flush=True)
            print(f"[PVF] boot warmup {format_warmup_report(warm)}", flush=True)
            drop_dom = (warm.get("domains") or {}).get("drop_index") or {}
            print(
                "[DROP] boot warm "
                f"ok={int(bool(drop_dom.get('ok')))} "
                f"equip={drop_dom.get('equip_count')} "
                f"stackable={drop_dom.get('stackable_count')} "
                f"indep={drop_dom.get('independent_entries')} "
                f"world_lv={drop_dom.get('world_levels')} "
                f"monseter_rows={drop_dom.get('drop_prob_rows')} "
                f"ms={drop_dom.get('elapsed_ms')}",
                flush=True,
            )
            if not is_drop_index_ready():
                print("[DROP] boot warm self-check FAILED (indexes not loaded)", flush=True)
                return 1
            safe_print(
                format_init_log("Script.pvf", pvf_detail, success=True),
                flush=True,
            )
            if bool(warm.get("ok")):
                safe_print(
                    format_init_log(
                        "PVF Runtime Index",
                        "%s Domains" % len(warm.get("domains") or {}),
                        success=True,
                    ),
                    flush=True,
                )
            from game.character.character_db import CharacterRepository
            from game.character.new_character_contract import (
                backfill_new_character_contracts,
            )

            character_repository = CharacterRepository(args.db_path)
            contracts = backfill_new_character_contracts(character_repository)
            print(
                "[NEW_CHARACTER_CONTRACT] backfill characters=%s contracts_upgraded=%s"
                % (
                    contracts["characters"],
                    contracts["contracts_upgraded"],
                ),
                flush=True,
            )
            if not args.channel_only:
                from data import CLIENT_DIR
                from game.character.character_db import DEFAULT_ACCOUNT_ID
                from startup_adventure_buff_dstr import (
                    sync_adventure_buff_tooltip_or_default,
                )

                account_id = int(
                    os.environ.get("USDOF_ACCOUNT_ID", str(DEFAULT_ACCOUNT_ID)),
                    0,
                )
                adventure_tooltip = sync_adventure_buff_tooltip_or_default(
                    Path(CLIENT_DIR) / "dstr.dat",
                    character_repository,
                    account_id,
                )
                if adventure_tooltip.used_client_default:
                    safe_print(
                        "[ADVENTURE_BUFF] dstr sync skipped; client default retained: %s"
                        % adventure_tooltip.sync_error,
                        flush=True,
                    )
                safe_print(
                    "[ADVENTURE_BUFF] %s" % adventure_tooltip.describe(),
                    flush=True,
                )
                safe_print(
                    format_init_log(
                        "Adventure Buff Tooltip",
                        "Lv.%d / %s"
                        % (
                            adventure_tooltip.adventure_level,
                            (
                                "Client default DSTR"
                                if adventure_tooltip.used_client_default
                                else adventure_tooltip.tooltip_text
                            ),
                        ),
                        success=True,
                    ),
                    flush=True,
                )
        if os.environ.get('USDOF_RAID_LOCAL', '').strip().lower() in ('1', 'true', 'yes', 'on'):
            from game.raid.bootstrap import validate_local_settings, preload_runtime
            period = validate_local_settings(args, mode)
            raid_data = preload_runtime()
            safe_print('[RAID] local dependencies ready channels=23,32 families=anton,luke pvf=%s period=%s' %
                       (raid_data.source_sha256, period), flush=True)
        elif os.environ.get('USDOF_RAID_BATTLE', '').strip().lower() in ('1', 'true', 'yes', 'on'):
            from game.raid.bootstrap import validate_public_settings, validate_public_listeners, preload_runtime
            from game.raid.ledger import RaidLedger
            validate_public_settings(args, mode)
            validate_public_listeners()
            raid_data = preload_runtime()
            RaidLedger(character_repository).ensure_schema()
            safe_print('[RAID] owned public battle dependencies ready channels=23,32 pvf=%s period=server-local-calendar' %
                       raid_data.source_sha256, flush=True)
    except Exception as exc:
        startup_metrics.record(
            "startup_failed",
            error="%s: %s" % (type(exc).__name__, exc),
        )
        log_pvf_preload_failure(exc)
        return 1

    servers = []
    threads = []
    ready_services = []
    interceptor: PortInterceptorManager | None = None

    if not args.game_only:
        channel = ChannelServer(
            args.host,
            args.channel_port,
            game_host=args.advertise_host,
            game_port=args.game_port,
            prefer_pvf_script=args.prefer_pvf_script,
            channelinfo_profile=args.channelinfo_profile,
            script_profile=args.script_profile,
            channelinfo_envelope=args.channelinfo_envelope,
        )
        servers.append(channel)
        ready_services.append(("channel", channel))
        threads.append(threading.Thread(target=channel.serve_forever, daemon=True))

    if not args.channel_only:
        initial_probes = parse_probe_specs(args.game_probes) if args.game_probes is not None else None
        ack_response_probes = None
        piv_response_probes = None
        dynamic_userinfo = args.game_db_userinfo
        if (
            args.game_flow_profile is not None
            or args.game_ack_responses is not None
            or args.game_ack_plaintext_responses is not None
        ):
            ack_response_probes = []
            if args.game_ack_responses:
                ack_response_probes.extend(parse_probe_specs(args.game_ack_responses))
            if args.game_ack_plaintext_responses:
                ack_response_probes.extend(
                    parse_plaintext_probe_specs(args.game_ack_plaintext_responses)
                )
            if args.game_flow_profile == "enter-game-p1" and not ack_response_probes:
                dynamic_userinfo = True
        if args.game_piv_responses is not None or args.game_piv_plaintext_responses is not None:
            piv_response_probes = []
            if args.game_piv_responses:
                piv_response_probes.extend(parse_probe_specs(args.game_piv_responses))
            if args.game_piv_plaintext_responses:
                piv_response_probes.extend(
                    parse_plaintext_probe_specs(args.game_piv_plaintext_responses)
                )
        game = GameServer(
            args.host,
            args.game_port,
            initial_probes=initial_probes,
            ack_response_probes=ack_response_probes,
            piv_response_probes=piv_response_probes,
            dynamic_userinfo=dynamic_userinfo,
            db_path=args.db_path,
            userinfo_roster_profile=args.game_roster_profile,
        )
        try:
            from channel.channel_info import collect_game_listen_ports

            listen_ports = collect_game_listen_ports(
                profile=args.channelinfo_profile,
                game_port=args.game_port,
            )
            game.set_listen_ports(listen_ports)
            if os.environ.get("USDOF_INTERNAL_PROTOCOL_LOG", "1").strip().lower() not in {
                "0", "false", "no", "off", "n",
            }:
                port_note = "..." if len(listen_ports) > 12 else ""
                print(
                    "[Game] unique-port listen map count=%s ports=%s%s"
                    % (len(listen_ports), listen_ports[:12], port_note),
                    flush=True,
                )
        except Exception as exc:
            if os.environ.get("USDOF_INTERNAL_PROTOCOL_LOG", "1").strip().lower() not in {
                "0", "false", "no", "off", "n",
            }:
                print(
                    f"[Game] unique-port listen map skipped: {type(exc).__name__}: {exc}",
                    flush=True,
                )
            else:
                safe_print(
                    "[startup] service listen-map preparation failed: %s: %s"
                    % (type(exc).__name__, exc),
                    flush=True,
                )
        # Install live game targeting before any service thread starts.
        from game.management_runtime import install_game
        install_game(game)
        servers.append(game)
        ready_services.append(("game", game))
        threads.append(threading.Thread(target=game.serve_forever, daemon=True))
        if not args.disable_control:
            control = GmApiServer(
                game,
                host=args.control_host,
                port=args.control_port,
                endpoint_host=LOCAL_HOST,
                endpoint_path=LOG_DIR / "control_endpoint.json",
                startup_metric=startup_metrics.record,
                audit_prefix="Control",
                service_name="usdof-control",
                include_token_in_endpoint=True,
            )
            servers.append(control)
            ready_services.append(("control", control))
            threads.append(threading.Thread(target=control.serve_forever, daemon=True))
    elif not args.disable_control:
        print("[Control] skipped because --channel-only has no Game session owner", flush=True)

    if (
        not args.disable_login
        and not args.channel_only
        and not args.game_only
    ):
        login = LoginApiServer(
            game,
            channel,
            host=args.login_host,
            port=args.login_port,
            db_path=args.db_path,
            pvf_profile=str(getattr(stats, "profile_name", args.script_profile)),
            pvf_sha256=str(getattr(stats, "source_sha256", "")),
        )
        servers.append(login)
        ready_services.append(("login", login))
        threads.append(threading.Thread(target=login.serve_forever, daemon=True))
    elif not args.disable_login:
        print(
            "[Login] skipped because launcher login requires combined Channel/Game mode",
            flush=True,
        )

    disable_default_extra = os.environ.get("USDOF_DISABLE_DEFAULT_EXTRA_PORTS", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    extra_tcp_ports = parse_port_list(args.extra_tcp_ports)
    extra_udp_ports = parse_port_list(args.extra_udp_ports)
    if args.extra_tcp_ports is None and not disable_default_extra:
        extra_tcp_ports = list(DEFAULT_EXTRA_TCP_PORTS)
    if args.extra_udp_ports is None and not disable_default_extra:
        extra_udp_ports = list(DEFAULT_EXTRA_UDP_PORTS)
    if extra_tcp_ports or extra_udp_ports:
        interceptor = PortInterceptorManager(
            args.host, reserved_tcp_ports={args.channel_port, args.game_port},
            udp_datagram_handler=native_udp_handler_for_game(game),
        )
        interceptor.start_tcp_ports(extra_tcp_ports, "start.py startup extra TCP intercept ports")
        interceptor.start_udp_ports(extra_udp_ports, "start.py startup extra UDP intercept ports")

    if not args.channel_only:
        from game.raid.native_nat_profile import install_profile as install_native_nat_profile
        install_native_nat_profile(game, interceptor, listen_ports=extra_udp_ports)

    for thread in threads:
        thread.start()
    if lazy_warmup_target is not None:
        lazy_warmup_thread = threading.Thread(
            target=lazy_warmup_target,
            name="usdof-pvf-lazy-warmup",
            daemon=True,
        )
        lazy_warmup_thread.start()
    ready_timeout = float(
        os.environ.get("USDOF_STARTUP_READY_TIMEOUT_SECONDS", "15") or "15"
    )
    if not wait_for_services_ready(
        ready_services,
        startup_metrics,
        timeout=ready_timeout,
    ):
        for server in servers:
            server.stop()
        if interceptor:
            interceptor.stop()
        safe_print("[startup] service bind/listen readiness timed out", flush=True)
        return 1
    startup_metrics.record(
        "services_ready",
        boot_mode=mode,
        pvf_profile=str(getattr(stats, "profile_name", "")),
        pvf_sha256=str(getattr(stats, "source_sha256", "")),
        directory_cache=str(
            getattr(stats, "directory_cache_status", "uninitialized")
        ),
        services=[name for name, _service in ready_services],
        hard_domain_count=len((warm.get("domains") or {})) if mode not in ("quick", "lazy") else 0,
    )
    print("USDOF local prototype running. Ctrl+C to stop.", flush=True)
    try:
        while True:
            if lazy_warmup_failed.is_set():
                for server in servers:
                    server.stop()
                if interceptor:
                    interceptor.stop()
                safe_print("[startup] lazy PVF background warmup failed", flush=True)
                return 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        for server in servers:
            server.stop()
        if interceptor:
            interceptor.stop()
        print("stopping...", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
