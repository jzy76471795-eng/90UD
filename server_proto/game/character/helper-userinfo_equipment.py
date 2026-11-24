"""Stage3 USERINFO equipment list helpers (0x018CC430).

One feature module: build/normalize equipped rows for USERINFO0 appearance
(enter-room / equip-change / roster projection helpers).

外观渲染优先级（客户端纸娃娃，服务端按同序下发）：
  装扮(avatar 0..11，含 weapon avatar=10 / aura skin=11)
  > 装备(body，weapon=12 / title=13 / armor=14..)
  > 默认角色装扮（无穿戴行时客户端回落）

同一视觉位若同时有装扮与装备，**两条都下发**（slot 不同），由客户端按
装扮>装备 选模型；禁止为“省事”丢掉装备行（USERINFO1/属性仍依赖真实装备）。
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from game.character.adventure_group import (
    calculate_adventure_group_for_character,
)
from game.character.item_metadata import (
    AURA_ATTRIBUTE_EQUIP_SLOT,
    AURA_SKIN_EQUIP_SLOT,
)
from game.chain_loader import load_chain
from game.guild.guild_benefits import (
    has_active_guild_membership,
    pack_guild_membership_status_rows,
)
from game.guild.guild_repository import GuildRepository

_quest_completion = load_chain("helper-quest_completion_snapshot.py")


# Stage3 外观槽（对照 S4A12 EquipmentType；身体装备相对其 weapon 起 +1）
# 装扮 0..11（10 = weapon avatar，11 = aura skin）；武器 12；称号 13；甲/饰品 14..
APPEARANCE_AVATAR_SLOT_MIN = 0
APPEARANCE_AVATAR_SLOT_MAX = AURA_SKIN_EQUIP_SLOT
APPEARANCE_WEAPON_AVATAR_SLOT = 10
APPEARANCE_WEAPON_SLOT = 12
APPEARANCE_TITLE_SLOT = 13


def pack_userinfo0_equipment_state(
    slot: Any,
    option_value: Any,
    extra_json: Any = None,
) -> int:
    """Pack the Stage3 town-appearance reinforce/amplify byte.

    ``sub_18CC430`` stores ``packed >> 1`` as the per-slot reinforcement
    level and ``packed & 1`` as the per-slot amplification state.
    ``option_value`` keeps unrelated upper bits, so only its low five-bit
    reinforcement level belongs in this USERINFO0 projection. Avatar slots
    use ``option_value`` for their ability selection and therefore stay zero.
    """
    try:
        slot_value = int(slot or 0) & 0xFF
    except (TypeError, ValueError):
        slot_value = 0
    if is_avatar_appearance_slot(slot_value):
        return 0

    try:
        reinforcement_level = max(0, int(option_value or 0)) & 0x1F
    except (TypeError, ValueError):
        reinforcement_level = 0

    from game.character.amplify_option import parse_amplify_state

    amplify_type, _amplify_value = parse_amplify_state(extra_json)
    return (reinforcement_level << 1) | int(amplify_type != 0)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "n"}


def _strip_support_weapon_body(entries: Sequence[Any]) -> tuple[tuple[Any, ...], str]:
    """默认从 USERINFO0 身体线体去掉副手（support weapon）。

    真实实现在 helper-knight_shield.filter_body_equipment_entries；
    本函数额外返回 note 供进房日志对照 n=10 / n=11。
    """
    before = tuple(entries or ())
    try:
        from game.chain_loader import load_chain

        after = tuple(
            load_chain("helper-knight_shield.py").filter_body_equipment_entries(before)
        )
    except Exception as exc:
        return before, " strip=fail(%s)" % type(exc).__name__
    if len(after) == len(before):
        return after, ""
    return after, " strip_support=%s" % (len(before) - len(after))


def build_userinfo_equipment_row(
    slot: int,
    item_id: int,
    *,
    flag: int = 0,
    aux1: int = 0,
    aux2: int = 0,
    unknown: int = 0,
    custom_blob: bytes = b"",
) -> bytes:
    """Encode one sub_18CC430 equipment entry（custom 空时 19B）.

    必含 u32 custom_len（sub_1AAEBB0→sub_2BF4BC0 读取）；禁止改 15B。
    """
    blob = bytes(custom_blob or b"")
    return (
        bytes([int(slot) & 0xFF])
        + (int(item_id) & 0xFFFFFFFF).to_bytes(4, "little")
        + len(blob).to_bytes(4, "little")
        + blob
        + bytes([int(flag) & 0xFF])
        + (int(aux1) & 0xFFFFFFFF).to_bytes(4, "little")
        + (int(aux2) & 0xFFFFFFFF).to_bytes(4, "little")
        + bytes([int(unknown) & 0xFF])
    )


def normalize_equipment_entries(entries: Any) -> list[tuple[int, int, int, int, int, int]]:
    """Normalize DB/PVF equipment into (slot, item_id, flag, aux1, aux2, unknown)."""
    if not entries:
        return []
    result: list[tuple[int, int, int, int, int, int]] = []
    for entry in entries:
        if isinstance(entry, dict):
            slot = entry.get("slot", 0)
            item_id = entry.get("item_id", 0)
            flag = entry.get("flag", 0)
            aux1 = entry.get("aux1", entry.get("expire_time", 0))
            aux2 = entry.get("aux2", 0)
            unknown = entry.get("unknown", 0)
        else:
            slot = getattr(entry, "slot", None)
            item_id = getattr(entry, "item_id", None)
            flag = getattr(entry, "flag", 0)
            aux1 = getattr(entry, "aux1", getattr(entry, "expire_time", 0))
            aux2 = getattr(entry, "aux2", 0)
            unknown = getattr(entry, "unknown", 0)
            if slot is None or item_id is None:
                values = list(entry)
                slot = values[0] if len(values) > 0 else 0
                item_id = values[1] if len(values) > 1 else 0
                flag = values[2] if len(values) > 2 else 0
                aux1 = values[3] if len(values) > 3 else 0
                aux2 = values[4] if len(values) > 4 else 0
                unknown = values[5] if len(values) > 5 else 0
        slot_i = int(slot) & 0xFF
        item_i = int(item_id) & 0xFFFFFFFF
        if item_i <= 0:
            continue
        result.append(
            (
                slot_i,
                item_i,
                int(flag) & 0xFF,
                int(aux1) & 0xFFFFFFFF,
                int(aux2) & 0xFFFFFFFF,
                int(unknown) & 0xFF,
            )
        )
    # Keep stable slot order for deterministic wire / model refresh.
    result.sort(key=lambda row: (row[0], row[1]))
    return result


def encode_userinfo_equipment_list(entries: Any) -> bytes:
    """u8 count + 0x018CC430 rows."""
    rows = normalize_equipment_entries(entries)
    if len(rows) > 0xFF:
        raise ValueError("USERINFO equipment count exceeds u8")
    body = bytearray()
    body.append(len(rows) & 0xFF)
    for slot, item_id, flag, aux1, aux2, unknown in rows:
        body += build_userinfo_equipment_row(
            slot,
            item_id,
            flag=flag,
            aux1=aux1,
            aux2=aux2,
            unknown=unknown,
        )
    return bytes(body)


def _entry_slot(entry: Any) -> int:
    if isinstance(entry, dict):
        return int(entry.get("slot", 0) or 0) & 0xFF
    slot = getattr(entry, "slot", None)
    if slot is not None:
        return int(slot) & 0xFF
    values = list(entry)
    return int(values[0] if values else 0) & 0xFF


def _entry_item_id(entry: Any) -> int:
    if isinstance(entry, dict):
        return int(entry.get("item_id", 0) or 0) & 0xFFFFFFFF
    item_id = getattr(entry, "item_id", None)
    if item_id is not None:
        return int(item_id) & 0xFFFFFFFF
    values = list(entry)
    return int(values[1] if len(values) > 1 else 0) & 0xFFFFFFFF


def is_avatar_appearance_slot(slot: int) -> bool:
    """装扮外观槽 0..11（含 weapon avatar=10 / aura skin=11）。"""
    s = int(slot) & 0xFF
    return APPEARANCE_AVATAR_SLOT_MIN <= s <= APPEARANCE_AVATAR_SLOT_MAX


def is_body_equipment_slot(slot: int) -> bool:
    """身体装备槽（武器/称号/甲/饰…），不含装扮 0..11。"""
    return not is_avatar_appearance_slot(int(slot))


def apply_appearance_priority(entries: Any) -> tuple[Any, ...]:
    """按 装扮>装备 整理外观行，保持稳定 slot 序。

    规则：
    - 装扮槽与装备槽**都保留**（不同 slot）；客户端按装扮优先渲染模型。
    - 同 slot 多行时保留 item_id>0 的最后一条（DB 不应重复，防御用）。
    - 不把装扮 item 写进装备 slot，也不删除真实武器行。
    """
    by_slot: dict[int, Any] = {}
    for entry in entries or ():
        item_id = _entry_item_id(entry)
        if item_id <= 0:
            continue
        slot = _entry_slot(entry)
        by_slot[slot] = entry
    ordered = tuple(by_slot[s] for s in sorted(by_slot.keys()))
    return ordered


def describe_appearance_priority(entries: Any) -> str:
    """调试摘要：武器装扮与幻化光环的客户端模型优先级。"""
    rows = normalize_equipment_entries(entries)
    by_slot = {s: i for s, i, *_rest in rows}
    avatar_w = by_slot.get(APPEARANCE_WEAPON_AVATAR_SLOT)
    body_w = by_slot.get(APPEARANCE_WEAPON_SLOT)
    if avatar_w and body_w:
        weapon = "weapon_model=avatar(%s)>equip(%s)" % (avatar_w, body_w)
    elif avatar_w:
        weapon = "weapon_model=avatar(%s)" % avatar_w
    elif body_w:
        weapon = "weapon_model=equip(%s)" % body_w
    else:
        weapon = "weapon_model=default"

    aura = by_slot.get(AURA_ATTRIBUTE_EQUIP_SLOT)
    aura_skin = by_slot.get(AURA_SKIN_EQUIP_SLOT)
    if aura_skin and aura:
        aura_model = "aura_model=skin(%s)>aura(%s)" % (aura_skin, aura)
    elif aura_skin:
        aura_model = "aura_model=skin(%s)" % aura_skin
    elif aura:
        aura_model = "aura_model=aura(%s)" % aura
    else:
        aura_model = "aura_model=default"
    return "%s %s" % (weapon, aura_model)


def _load_live_equipped_from_repo(repo: Any, character_id: int) -> tuple[Any, ...] | None:
    """从 repo 重读穿戴（避免 session/record 快照过期）。失败返回 None。"""
    cid = int(character_id or 0)
    if repo is None or cid <= 0:
        return None
    try:
        rows = repo.list_equipped_items(cid)
    except Exception:
        return None
    if not rows:
        return ()
    out: list[Any] = []
    for row in rows:
        slot = int(row.get("slot", 0) or 0) & 0xFF
        item_id = int(row.get("item_id", 0) or 0) & 0xFFFFFFFF
        if item_id <= 0:
            continue
        expire = int(row.get("expire_time", 0) or 0) & 0xFFFFFFFF
        option_value = int(row.get("option_value", 0) or 0) & 0xFF
        marker_16 = int(row.get("marker_16", 0) or 0) & 0xFFFFFFFF
        clear_avatar_id = int(row.get("clear_avatar_id", 0) or 0) & 0xFFFFFFFF
        extra_json = row.get("extra_json")
        out.append(
            type(
                "E",
                (),
                {
                    "slot": slot,
                    "item_id": item_id,
                    "expire_time": expire,
                    "option_value": option_value,
                    "marker_16": marker_16,
                    "clear_avatar_id": clear_avatar_id,
                    "extra_json": extra_json,
                    "aux1": expire,
                    "flag": pack_userinfo0_equipment_state(
                        slot,
                        option_value,
                        extra_json,
                    ),
                    "aux2": 0,
                    "unknown": 0,
                },
            )()
        )
    return tuple(out)


def resolve_enter_room_equipment(
    selected: Any,
    *,
    enabled: bool | None = None,
    backfill_from_pvf: bool | None = None,
    repo: Any = None,
    prefer_live_db: bool | None = None,
) -> tuple[tuple[Any, ...], str]:
    """Resolve equipped rows for USERINFO0 appearance.

    Priority:
      1. live DB via repo.list_equipped_items（换装后必须，避免 record 快照过期）
      2. selected.equipped_entries
      3. if empty and backfill enabled: PVF [create equipment list] for job

    外观优先级：装扮>装备>默认（见 apply_appearance_priority / describe_appearance_priority）。
    Inventory 槽对象创建见 helper-userinfo1_equipment（USERINFO1 / sub_166AED0）；
    栏格子更新见 helper-equip_itemlist（S0/000E list_type=3）。
    """
    if enabled is None:
        enabled = _env_bool("USDOF_GAME_ENTER_SERIA_SEND_EQUIPMENT", True)
    if not enabled:
        return (), "equip=off"

    if prefer_live_db is None:
        prefer_live_db = _env_bool("USDOF_GAME_EQUIP_PREFER_LIVE_DB", True)

    character_id = int(getattr(selected, "character_id", 0) or 0)
    source = "db"
    entries: tuple[Any, ...] = ()

    if prefer_live_db and repo is not None and character_id > 0:
        live = _load_live_equipped_from_repo(repo, character_id)
        if live is not None and live:
            entries = apply_appearance_priority(live)
            entries, strip_note = _strip_support_weapon_body(entries)
            source = "db_live"
            prio = describe_appearance_priority(entries)
            note = "equip=%s n=%s slots=%s %s%s" % (
                source,
                len(entries),
                ",".join(
                    "%s:%s" % (int(getattr(e, "slot", 0)), int(getattr(e, "item_id", 0)))
                    for e in entries
                ),
                prio,
                strip_note,
            )
            return entries, note
        if live is not None and not live:
            # DB 明确为空，再走 selected / backfill
            entries = ()
            source = "db_live_empty"

    if not entries:
        entries = tuple(getattr(selected, "equipped_entries", ()) or ())
        if entries:
            entries = apply_appearance_priority(entries)
            entries, strip_note = _strip_support_weapon_body(entries)
            source = "db"
            prio = describe_appearance_priority(entries)
            note = "equip=%s n=%s slots=%s %s%s" % (
                source,
                len(entries),
                ",".join("%s:%s" % (int(e.slot), int(e.item_id)) for e in entries),
                prio,
                strip_note,
            )
            return entries, note

    if backfill_from_pvf is None:
        backfill_from_pvf = _env_bool("USDOF_GAME_ENTER_SERIA_BACKFILL_INITIAL_EQUIPMENT", True)
    if not backfill_from_pvf:
        return (), "equip=empty(no_db_no_backfill)"

    # Avoid cold full-PVF preload on the enter-room hot path. Create-character
    # already seeds DB while the server has Script.pvf preloaded; only backfill
    # here when the shared reader is already memory-backed (or force env).
    try:
        from pvf import pvf_runtime
        from game.character.initial_equipment import (
            get_initial_equipment_for_job,
            resolve_initial_equipment_pvf_path,
        )
    except Exception as exc:  # pragma: no cover - import path only
        return (), "equip=empty(pvf_import_fail:%s)" % exc

    force_pvf = _env_bool("USDOF_GAME_ENTER_SERIA_FORCE_PVF_EQUIPMENT_BACKFILL", False)
    pvf_path = resolve_initial_equipment_pvf_path()
    if not force_pvf and (pvf_path is None or not pvf_runtime.is_runtime_ready(pvf_path)):
        return (), "equip=empty(pvf_not_runtime_ready)"

    job = int(getattr(selected, "job", 0) or 0) & 0xFF
    try:
        initial = tuple(get_initial_equipment_for_job(job, pvf_path=pvf_path) or ())
    except Exception as exc:
        return (), "equip=empty(pvf_read_fail job=%s err=%s)" % (job, exc)

    if not initial:
        return (), "equip=empty(pvf_none job=%s)" % job

    # Persist so next enter does not re-hit PVF hot path.
    if repo is not None and _env_bool("USDOF_GAME_ENTER_SERIA_PERSIST_BACKFILL_EQUIPMENT", True):
        try:
            if character_id > 0:
                persisted = repo.replace_equipped_entries(character_id, initial)
                entries = apply_appearance_priority(tuple(persisted or ()))
                source = "pvf+db"
            else:
                entries = apply_appearance_priority(
                    tuple(
                        type("E", (), {"slot": int(s), "item_id": int(i)})()
                        for s, i in initial
                    )
                )
                source = "pvf"
        except Exception as exc:
            entries = apply_appearance_priority(
                tuple(
                    type("E", (), {"slot": int(s), "item_id": int(i)})()
                    for s, i in initial
                )
            )
            source = "pvf(persist_fail:%s)" % exc
    else:
        entries = apply_appearance_priority(
            tuple(
                type("E", (), {"slot": int(s), "item_id": int(i)})()
                for s, i in initial
            )
        )
        source = "pvf"

    prio = describe_appearance_priority(entries)
    note = "equip=%s n=%s slots=%s %s" % (
        source,
        len(entries),
        ",".join(
            "%s:%s" % (int(getattr(e, "slot", 0)), int(getattr(e, "item_id", 0)))
            for e in entries
        ),
        prio,
    )
    return entries, note


def _resolve_exp_level_for_view(
    view: Any,
    *,
    repo: Any,
    character_id: int,
) -> tuple[int, int]:
    """从 live DB 取 (exp, level)；失败回落 record。"""
    level = max(1, int(getattr(view, "level", 1) or 1))
    exp = int(getattr(view, "exp", 0) or 0) & 0xFFFFFFFF
    cid = int(character_id or 0)
    if repo is None or cid <= 0:
        return exp, level
    try:
        exp_db, lv_db = repo.get_character_exp_level(cid)
        exp = int(exp_db or 0) & 0xFFFFFFFF
        if int(lv_db or 0) > 0:
            level = max(1, int(lv_db))
    except Exception:
        pass
    return exp, level


def _session_in_dungeon_run(session: Any) -> bool:
    """副本 run 中（含结算 UI）为 True。"""
    if session is None:
        return False
    try:
        return bool(getattr(session, "in_dungeon_run", False))
    except Exception:
        return False


def build_userinfo0_appearance_packets(
    server: Any,
    *,
    character_id: int,
    record: Any,
    repo: Any = None,
    session: Any = None,
    allow_dungeon_userinfo0: bool = False,
    include_visibility: bool = True,
) -> tuple[list[bytes], str]:
    """换装/外观变更后构造 S0/0002 USERINFO0（模型同步）[+ 可选 USERINFO1 经验回写]。

    对照 S4A12 InventoryHandler.Move → SendNoti2AppearanceUpdate：
    客户端收到 0013 ACK / 000E 后**不会**自行重建纸娃娃，必须跟发 subtype0。

    Stage3 live（2026-07-19）：仅发 USERINFO0 时，底部经验条会变成十几亿级脏值。
    原因：type0 外观刷新会触达角色对象/UI 绑定，但 **不写** exp store；
    exp 真源是 USERINFO1 ``sub_18CE440`` / ``vtable+2612``。
    因此**城镇**默认在 U0 后跟发 U1（DB exp + 同套 equipment）。

    **副本内硬约束**（2026-07-21 live v2）：
      mid-dungeon 禁止 USERINFO0/USERINFO1 reprime。
      - 070535：0013 → U0+U1 → 客户端 002A leave → 002D reject=not_in_dungeon_run
      - 071519：0013 → 仅 U0（U1 已 skip）→ 清房后客户端**不发 002D**（卡死；
        仅 01DB keepalive），同样无法进下一房。
      结论：副本内 ``sub_18CC430`` 外观包也会弄脏换房状态机；换装只靠
      ACK0013 + 000D list0 + 000E list3 即可（栏 UI 更新）；纸娃娃下房/下一房
      下次建立角色主对象时由完整 USERINFO1 重建。
      默认：``in_dungeon_run`` 时**整段跳过**（空包）。
      关逃 U0：``USDOF_GAME_APPEARANCE_RESEND_USERINFO0_IN_DUNGEON=1``
      关逃 U1：``USDOF_GAME_APPEARANCE_RESEND_USERINFO1_IN_DUNGEON=1``
      （U1 关逃仅在 U0 也放行时生效）
    城镇 U1 总开关：``USDOF_GAME_APPEARANCE_RESEND_USERINFO1=0``。

    **骑士主盾例外**（2026-07-22 live）：
      副本技能切盾 / 预备槽切主 时，000E list3 能刷装备栏 slot24，但属性面板与
      纸娃娃读的是 USERINFO0 装备段。``allow_dungeon_userinfo0=True`` 时放行 **仅 U0**
      （U1 仍禁，避免 002D 换房卡死）。仅 0013 骑士盾 ``equipment_changed`` 路径传。
    """
    if record is None:
        return [], "USERINFO0_skip=no_record"

    in_dungeon = _session_in_dungeon_run(session)
    dungeon_u0_escape = _env_bool(
        "USDOF_GAME_APPEARANCE_RESEND_USERINFO0_IN_DUNGEON", False
    ) or bool(allow_dungeon_userinfo0)
    # 副本内默认整段 appearance 跳过（U0+U1）；紧急 A/B 或骑士主盾路径可放行 U0
    if in_dungeon and not dungeon_u0_escape:
        return [], "USERINFO0_skip=in_dungeon_run"

    try:
        from game.character.payloads import (
            build_userinfo_subtype0_min,
            build_userinfo_subtype1_min,
        )
    except Exception as exc:
        return [], "USERINFO0_fail:import:%s" % type(exc).__name__

    use_repo = repo if repo is not None else getattr(server, "_repo", lambda: None)()
    # 优先用 live DB；若 record 账号可扫，仍以 character_id 对齐
    view = record
    try:
        account_id = int(getattr(record, "account_id", 0) or 0)
        if use_repo is not None and account_id > 0 and int(character_id) > 0:
            for r in use_repo.list_by_account(account_id) or ():
                if int(getattr(r, "character_id", 0) or 0) == int(character_id):
                    view = r
                    break
    except Exception:
        view = record

    equipment, enote = resolve_enter_room_equipment(
        view,
        enabled=True,
        backfill_from_pvf=False,
        repo=use_repo,
        prefer_live_db=True,
    )
    guild_id = 0
    guild_name = ""
    guild_db_path = getattr(use_repo, "db_path", None)
    if guild_db_path is not None:
        guild = GuildRepository(guild_db_path).get_guild_for_character(
            int(character_id)
        )
        if guild is not None:
            guild_id = int(guild.guild_id)
            guild_name = guild.display_name
    packets: list[bytes] = []
    notes: list[str] = []
    try:
        body_u0 = build_userinfo_subtype0_min(
            view,
            equipment,
            guild_name=guild_name,
            guild_id=guild_id,
        )
        packet_u0 = server._codec_response(0, 0x0002, body_u0)
        if session is not None and not in_dungeon:
            from game.raid.local_bindings import enabled as raid_local_enabled
            raid_service = getattr(server, 'raid_service', None)
            if (raid_local_enabled() and raid_service is not None
                    and getattr(session, 'selected_channel_id', None) in (23, 32)):
                raid_service.native_lobby.prepare_appearance(session, packet_u0, body_u0)
        packets.append(packet_u0)
        notes.append(
            "USERINFO0 %s plain_len=%s guild_id=%s guild=%r"
            % (enote, len(body_u0), guild_id, guild_name)
        )
        if include_visibility:
            visibility_helper = load_chain("helper-avatar_visibility.py")
            packets.append(visibility_helper.pack_avatar_visibility(server, view))
            notes.append(
                "S0/0165 avatar_visibility bits=0x%02x"
                % (int(getattr(view, "visibility_bits", 3)) & 0xFF)
            )
    except Exception as exc:
        return [], "USERINFO0_fail:%s" % type(exc).__name__

    allow_u1 = _env_bool("USDOF_GAME_APPEARANCE_RESEND_USERINFO1", True)
    # 副本内默认禁 U1（即使 U0 关逃放行）；仅紧急 A/B 可开 IN_DUNGEON
    if in_dungeon and not _env_bool(
        "USDOF_GAME_APPEARANCE_RESEND_USERINFO1_IN_DUNGEON", False
    ):
        allow_u1 = False
        notes.append("USERINFO1_skip=in_dungeon_run")

    # 跟发 USERINFO1：把 DB 累计经验绝对写回客户端 store，避免 U0 后经验条脏读
    if allow_u1:
        try:
            exp, level = _resolve_exp_level_for_view(
                view, repo=use_repo, character_id=int(character_id)
            )
            try:
                view.level = level
                view.exp = exp
            except Exception:
                pass

            skills: tuple = ()
            skill_n = 0
            skill_note = "skills=none"
            if _env_bool("USDOF_GAME_APPEARANCE_USERINFO1_SKILLS", True):
                try:
                    skills_helper = load_chain("helper-character_skills.py")
                    skill_list, skill_note = skills_helper.resolve_skills_for_character(
                        use_repo,
                        int(character_id),
                        int(getattr(view, "job", 0) or 0),
                        seed_if_empty=False,
                    )
                    skills = tuple(skill_list or ())
                    skill_n = len(skills)
                except Exception as skill_exc:
                    skill_note = "skills_fail=%r" % (skill_exc,)
                    skills = ()
                    skill_n = 0

            quest_snapshot = _quest_completion.load_finished_quest_snapshot(
                use_repo, int(character_id)
            )
            from game.quest.time_fragment_rewards import finished_time_fragment_count

            time_fragment_count = finished_time_fragment_count(
                quest_snapshot.finished_quest_ids,
                job_id=int(getattr(view, "job", 0) or 0),
            )
            adventure_progression = calculate_adventure_group_for_character(
                use_repo,
                view,
            )
            adventure_group_level = int(adventure_progression.adventure_level)
            guild_membership_active = has_active_guild_membership(use_repo, view)
            body_u1 = build_userinfo_subtype1_min(
                # USERINFO1 and its personality subset must share one DB read.
                view,
                exp=int(exp),
                skills=skills,
                entry_flag=0,
                equipment=equipment,
                finished_quest_ids=quest_snapshot.finished_quest_ids,
                adventure_group_level=adventure_group_level,
                guild_membership_active=guild_membership_active,
            )
            packets.append(server._codec_response(0, 0x0002, body_u1))
            guild_status_rows = pack_guild_membership_status_rows(
                server,
                guild_membership_active,
            )
            packets.extend(packet for packet, _note in guild_status_rows)
            notes.extend(note for _packet, note in guild_status_rows)
            notes.append(
                "USERINFO1 exp=%s lv=%s equip_n=%s skill_n=%s plain_len=%s %s "
                "time_fragments=%s adventure_level=%s %s"
                % (
                    exp,
                    level,
                    len(equipment),
                    skill_n,
                    len(body_u1),
                    skill_note,
                    time_fragment_count,
                    adventure_group_level,
                    quest_snapshot.describe(),
                )
            )
        except Exception as exc:
            notes.append("USERINFO1_fail:%s" % type(exc).__name__)

    return packets, "; ".join(notes)


__all__ = [
    "APPEARANCE_AVATAR_SLOT_MAX",
    "APPEARANCE_AVATAR_SLOT_MIN",
    "APPEARANCE_WEAPON_AVATAR_SLOT",
    "APPEARANCE_TITLE_SLOT",
    "APPEARANCE_WEAPON_SLOT",
    "apply_appearance_priority",
    "build_userinfo0_appearance_packets",
    "build_userinfo_equipment_row",
    "describe_appearance_priority",
    "encode_userinfo_equipment_list",
    "is_avatar_appearance_slot",
    "is_body_equipment_slot",
    "normalize_equipment_entries",
    "pack_userinfo0_equipment_state",
    "resolve_enter_room_equipment",
]
