"""Serve Adventure Group actions on the authenticated game connection."""

import time

from control.contracts import ControlApiError
from game.chain_loader import load_chain
from game.character import cn2018_capsule, cn2018_shop
from game.character.adventure_group_features import build_main_inventory_refresh_packet, selected_target_key
from game.player_assist import _town_target
from game.quest.weekly_attendance import Action, AttendanceError


WEEKLY_ACTIONS = {
    "weekly_status": Action.STATUS,
    "weekly_sign": Action.SIGN_WEEK,
    "weekly_make_up": Action.MAKE_UP,
    "weekly_claim": Action.CLAIM_CUMULATIVE,
}


def install(game, handlers, live_contexts):
    def handle(params, session):
        if not _town_target(session):
            raise ControlApiError("SESSION_NOT_READY", "请在当前角色的城镇中使用")
        weekly_action = WEEKLY_ACTIONS.get(params["action"])
        if weekly_action is not None:
            from game.player_exchange import service_for

            service = service_for(game)
            attendance = load_chain("opcode0458-weekly_attendance.py")
            with service.lock:
                if weekly_action != Action.STATUS and service.busy(service.owned(session)):
                    raise ControlApiError("TRADE_ACTIVE", "交易期间无法签到")
                try:
                    status, packets, note = attendance.settle_request(game, session, weekly_action, params.get("week", 0))
                except AttendanceError as error:
                    raise ControlApiError("ATTENDANCE_REJECTED", str(error)) from None
            return packets, {
                "character_id": session.selected_character_id,
                "connection_generation": params["connection_generation"],
                "attendance": list(status[1:]),
            }, note
        if not cn2018_shop.enabled():
            raise ControlApiError("ADVENTURE_UNAVAILABLE", "冒险团商店暂不可用", status=503)
        result = {
            "character_id": session.selected_character_id,
            "connection_generation": params["connection_generation"],
            "character_level": game._repo().get_character_progression(session.selected_character_id)[0],
        }
        if params["action"] == "state":
            return [], result, "adventure target resolved"
        if params["action"] == "refresh_expedition_wallet":
            packets, note, _ = game.contents_play_info_0578.build_full_state_refresh_0578(
                target_key=selected_target_key(game, session), session=session, reason="cn-expedition-claim",
            )
            return packets, result, note

        from game.player_exchange import service_for

        repo = game._repo()
        now = int(time.time())
        capsule = params["action"] == "use_capsule"
        character = session.selected_character_id
        exchange = service_for(game)
        with exchange.lock:
            if exchange.busy(exchange.owned(session)):
                raise ControlApiError("TRADE_ACTIVE", "交易期间无法购买")
            try:
                if capsule:
                    progression = cn2018_capsule.use(repo, session.account_id, character, params["quantity"],
                                                     params["request_id"], now)
                else:
                    shop = cn2018_shop.open_shop(repo)
                    shop.buy(session.account_id, character, params["item_id"], params["quantity"],
                             params["request_id"], now)
            except cn2018_capsule.CapsuleRejected as error:
                raise ControlApiError("CAPSULE_REJECTED", str(error)) from None
            except cn2018_shop.ShopRejected as error:
                raise ControlApiError("SHOP_REJECTED", str(error)) from None
            except (KeyError, FileNotFoundError, ValueError):
                raise ControlApiError("SHOP_UNAVAILABLE", "商店配置不可用", status=503) from None

        packets, note, _ = game.contents_play_info_0578.build_full_state_refresh_0578(
            target_key=selected_target_key(game, session), session=session,
            reason="cn-capsule-use" if capsule else "cn-shop-purchase",
        )
        if capsule:
            if progression is None:
                level, exp, expertise = repo.get_character_progression(character)
                progression = {"new_level": level, "exp": exp, "expertise_exp": expertise}
            session.character_level = progression["new_level"]
            session.character_exp = progression["exp"]
            session.character_expertise_exp = progression["expertise_exp"]
            exp_service = load_chain("opcode001e-increase_status.py").IncreaseStatus001EService(game)
            updates, exp_note = exp_service._experience_refresh_packets(
                repo=repo, character_id=character, session=session, result=progression,
            )
            return [*packets, *updates], result, f"{note}; {exp_note}"
        inventory, inventory_note = build_main_inventory_refresh_packet(game, repo, character)
        return [*packets, inventory], result, f"{note}; {inventory_note}"

    def request(identity, body):
        contexts = [
            entry["context"] for entry in live_contexts()
            if entry["account_id"] == identity.account_id
            and entry["context"].session.login_session_id == identity.login_session_id
            and entry["context"].session.selected_character_id
        ]
        if len(contexts) != 1:
            raise ControlApiError("SESSION_NOT_READY", "请先进入游戏角色")
        context = contexts[0]
        character = context.session.selected_character_id
        if "character_id" in body and (body["character_id"], body["connection_generation"]) != (
                character, context.generation):
            raise ControlApiError("TARGET_CHANGED", "游戏角色已改变，请重新打开面板")
        params = {
            **body,
            "connection_generation": context.generation,
            "_manager_expected_character": character,
            "_manager_expected_epoch": context.generation,
        }
        if body["action"] in WEEKLY_ACTIONS:
            tag = "weekly-attendance"
        else:
            tag = "adventure-expedition-wallet" if body["action"] == "refresh_expedition_wallet" else "adventure"
        return game.submit_control_command(
            command="player.adventure_group", params=params, timeout=8,
            request_id=f'{tag}:{identity.login_session_id}:{body["request_id"]}',
        )

    def refresh_expedition_wallet(identity, run_id):
        try:
            request(identity, {"action": "refresh_expedition_wallet", "request_id": run_id})
        except ControlApiError:
            # The claim is already committed. A subsequent profile refresh
            # will load the wallet if the client disconnected in the meantime.
            pass

    handlers["player.adventure_group"] = handle
    game._adventure_group_operation = request
    game._refresh_adventure_expedition_wallet = refresh_expedition_wallet
