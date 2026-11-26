"""Game-owned live connection targeting, policy and mailbox notifications.

No registration, CDK worker, scheduler or public launcher listener runs here.
"""
import functools
import socket
import threading
import time
from game.mail_store import MailStore, MailError, number


def install_channels(store):
    from channel import channel_info
    if getattr(channel_info, '_manager_channel_policy', False):
        return
    original = channel_info._filter_and_order_server_channel_rows
    def filtered(rows):
        with store.connect() as conn:
            disabled = {int(r[0]) for r in conn.execute('SELECT channel_id FROM gmkit_channel_policy WHERE enabled=0')}
        return [row for row in original(rows) if int(row.channel_id) not in disabled]
    channel_info._filter_and_order_server_channel_rows = filtered
    channel_info._manager_channel_policy = True


def install_game(game):
    from control.contracts import ControlApiError
    from data import DB_PATH
    # Do not initialize CharacterRepository before lazy PVF wallet limits warm up.
    store = MailStore(getattr(game, 'db_path', None) or DB_PATH)
    install_channels(store)
    tracked = {}
    tracker_lock = threading.RLock()
    selected = threading.local()
    from game.character import management_commands
    management_commands.install(game)
    native_handlers = dict(game._control_handlers)
    original_register = game.register_control_game_client
    original_current = game._current_control_context
    original_snapshot = game.get_control_session_snapshot
    original_submit = game.submit_control_command

    def live_contexts():
        with tracker_lock:
            for generation, entry in list(tracked.items()):
                try:
                    valid = int(entry["context"].client.fileno()) >= 0
                except Exception:
                    valid = False
                if not valid:
                    tracked.pop(generation, None)
            return list(tracked.values())

    def register(client, session, *, connection_id):
        aid = int(game.account_id)
        peer = client.getpeername()[0] if hasattr(client,"getpeername") else ""
        with store.connect() as conn:
            allowed = store.allows(conn, aid, peer)
        if not allowed:
            client.close()
            raise ControlApiError("ACCESS_DENIED","账号、来源IP封禁或维护规则拒绝新游戏连接",status=403)
        context = original_register(client, session, connection_id=connection_id)
        with tracker_lock:
            tracked[context.generation] = {"context": context, "account_id": int(game.account_id), "remote_ip": peer, "connected_at": time.time()}
        return context

    def current():
        return getattr(selected, "context", None) or original_current()

    def is_current(context):
        return any(entry["context"] is context for entry in live_contexts())

    def snapshot():
        document = original_snapshot()
        document["all_connections_complete"] = True
        document["connections"] = [{"account_id": entry["account_id"],
            "character_id": int(getattr(entry["context"].session, "selected_character_id", 0) or 0),
            "session_epoch": entry["context"].generation, "connected_at": entry["connected_at"], "remote_ip": entry["remote_ip"]}
            for entry in live_contexts()]
        document["management_adapter_version"] = 1
        return document

    def kick(account_id):
        count = 0
        for entry in live_contexts():
            if entry["account_id"] != int(account_id):
                continue
            client = entry["context"].client
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
                count += 1
            except OSError:
                pass
        from login.session_registry import get_login_registry
        registry = get_login_registry()
        with registry._lock:
            digest = registry._digest_by_account_id.get(int(account_id))
            if digest:
                registry._remove_locked(digest)
        return {"account_id": int(account_id), "disconnected": count, "sessions_revoked": bool(digest)}

    def claim_mail(params, session):
        if set(params) != {"mail_id"}:
            raise ControlApiError("INVALID_PARAMS", "mail_id is required", status=400)
        selected_character = getattr(session, "selected_character_id", None)
        entry = next((entry for entry in live_contexts() if entry["context"].session is session), None)
        if selected_character is None or entry is None:
            raise ControlApiError("SESSION_NOT_READY", "请进入收件角色后再领取", status=409)
        from game.native_mailbox import claim
        try:packets=claim(game,session,[number(params['mail_id'],'邮件ID',1)])
        except (MailError,ValueError) as error:
            raise ControlApiError('MAIL_REJECTED',str(error),status=getattr(error,'status',409)) from error
        return packets,{'mail_id':int(params['mail_id']),'claimed':True},'native mail claimed'

    def wrap_handler(handler):
        @functools.wraps(handler)
        def guarded(params, session):
            clean = dict(params)
            expected_character = clean.pop("_manager_expected_character", None)
            expected_epoch = clean.pop("_manager_expected_epoch", None)
            if expected_character is not None:
                context = next((e["context"] for e in live_contexts() if e["context"].session is session), None)
                if (context is None or int(context.generation) != int(expected_epoch)
                        or int(getattr(session, "selected_character_id", 0) or 0) != int(expected_character)):
                    raise ControlApiError("TARGET_CHANGED", "目标角色或游戏会话已经改变，未执行变更", status=409)
            return handler(clean, session)
        return guarded

    def refresh_mailbox(params,session):
        if params:raise ControlApiError('INVALID_PARAMS','mailbox refresh takes no parameters',status=400)
        from game.native_mailbox import snapshot_packets
        packets,count=snapshot_packets(game,session)
        return packets,{'mail_count':count},'native mailbox refreshed'

    def adventure_season1_status(params, session):
        if params:
            raise ControlApiError("INVALID_PARAMS", "赛季状态不接收参数", status=400)
        from game.quest.adventure_season1 import status
        try:
            state = status(game, session)
        except ValueError as exc:
            raise ControlApiError("SEASON1_STATUS_ERROR", str(exc), status=409) from exc
        return [], {"active": state is not None, "season": state}, "adventure season 1 status"

    def adventure_season1_complete(params, session):
        if set(params) != {"period", "bucket", "mission_id"}:
            raise ControlApiError("INVALID_PARAMS", "需要指定赛季任务", status=400)
        from game.quest.adventure_season1 import complete_mission
        try:
            completed = complete_mission(game, session, params["period"], params["bucket"], params["mission_id"])
        except ValueError as exc:
            raise ControlApiError("SEASON1_COMPLETE_ERROR", str(exc), status=409) from exc
        return [], {"completed": completed}, "adventure season 1 mission completion"

    def adventure_season1_claim(params, session):
        if set(params) != {"level"}:
            raise ControlApiError("INVALID_PARAMS", "需要指定奖励等级", status=400)
        from game.quest.adventure_season1 import claim_basic_reward
        try:
            delivered = claim_basic_reward(game, session, params["level"])
        except ValueError as exc:
            raise ControlApiError("SEASON1_CLAIM_ERROR", str(exc), status=409) from exc
        return [], {"delivered": delivered}, "adventure season 1 basic claim"

    native_handlers["adventure_season1.status"] = adventure_season1_status
    native_handlers["adventure_season1.complete"] = adventure_season1_complete
    native_handlers["adventure_season1.claim_basic"] = adventure_season1_claim

    def adventure_season1_claim_all(params, session):
        if params:
            raise ControlApiError("INVALID_PARAMS", "全部领取不接收参数", status=400)
        from game.quest.adventure_season1 import claim_all_basic_rewards
        try:
            levels = claim_all_basic_rewards(game, session)
        except ValueError as exc:
            raise ControlApiError("SEASON1_CLAIM_ERROR", str(exc), status=409) from exc
        return [], {"claimed_levels": levels}, "adventure season 1 basic claim all"

    native_handlers["adventure_season1.claim_all_basic"] = adventure_season1_claim_all
    native_handlers["manager.mailbox_refresh"] = refresh_mailbox
    native_handlers["manager.claim_mail"] = claim_mail
    from game import player_assist
    player_assist.install(game, native_handlers, live_contexts)
    from game.character import cn2018_operations
    cn2018_operations.install(game, native_handlers, live_contexts)
    for name, handler in native_handlers.items():
        game._control_handlers[name] = wrap_handler(handler)

    def submit(*, request_id, command, params, timeout):
        if command == "manager.kick":
            if set(params) != {"account_id"}:
                raise ControlApiError("INVALID_PARAMS", "account_id required", status=400)
            return kick(int(params["account_id"]))
        if command == "manager.snapshot":
            return snapshot()
        expected_character = params.get("_manager_expected_character")
        expected_epoch = params.get("_manager_expected_epoch")
        if expected_character is not None:
            context = next((entry["context"] for entry in live_contexts()
                if int(entry["context"].generation) == int(expected_epoch)
                and int(getattr(entry["context"].session, "selected_character_id", 0) or 0) == int(expected_character)), None)
            if context is None:
                raise ControlApiError("TARGET_CHANGED", "目标角色不在指定的有效游戏会话中", status=409)
            selected.context = context
        try:
            return original_submit(request_id=request_id, command=command, params=params, timeout=timeout)
        finally:
            if hasattr(selected, "context"):
                del selected.context

    game.register_control_game_client = register
    game._current_control_context = current
    game._is_current_control_context = is_current
    game.get_control_session_snapshot = snapshot
    game.submit_control_command = submit
    game._manager_target_guard = True
    game._manager_disconnect = kick

