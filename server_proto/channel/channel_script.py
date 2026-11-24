from __future__ import annotations

from pathlib import Path

import os

from config import CHANNEL_RESPONSE_FLAG, PVF_PATH, SCRIPT_RESPONSE_OPCODE
from channel.channel_crypto import build_encrypted_packed_payload
from channel.channel_wire import build_channel_packet


SCRIPT_PATH_CANDIDATES = (
    "channel_info.etc",
    "etc/channel_info.etc",
    "server/channel_info.etc",
    "channel/channel_info.etc",
    "system/channel_info.etc",
)

SCRIPT_PROFILES = (
    "minimal-zero",
    "minimal-valid-server",
    "minimal-valid-server-tail-id",
    "minimal-valid-server-post-label",
    "minimal-valid-server-post-group",
    "minimal-valid-server-zero-int",
    "minimal-valid-server-int-ones",
    "minimal-valid-server-ones",
    "minimal-valid-server-lines",
    "minimal-valid-server-recommend",
    "minimal-valid-server-ch12",
    "us-pvf-startarea-label",
    "pvf-text",
    "pvf-raw",
    "backup-text",
    "backup-channel-bundle",
    "tw-channel-r",
    "cain-pvf-paired",
    "pvf-cain-paired",
    "usdof-multi-channel",
)

CHANNEL_BUNDLE_FILENAMES = (
    # Core channel/server rows.  This one is already accepted by the Stage3
    # script parser and is still the first file in the bundled profile.
    "channel_info.etc",
    # UI/category and recommended-level side tables that can influence whether
    # a region/channel is shown as selectable or greyed out.
    "channeluiinfo.etc",
    "channelrecommendlevel.etc",
    "channelmovereservablelist.etc",
    # Link tables are not expected to affect selection, but keeping them last
    # makes this profile a faithful “all channel-related PVF etc files” probe.
    "channelingnexon.etc",
    "channelingnhn.etc",
)


def build_minimal_script_plaintext() -> bytes:
    # 0x015AB9D0 currently requires a non-empty packed payload and then calls
    # the Rijndael decrypt path.  The exact parser is still a reverse target,
    # so the safest synthetic minimum is one zero block after decrypt.
    return b"\x00" * 16


def _build_synthetic_one_channel_script_plaintext(
    *,
    channel_id: int = 1,
    channel_name: str = "Channel 1",
    first_value: str = "5",
    trailing_fields: tuple[str, ...] = ("1",) * 10,
    post_float_token: str | None = None,
    append_trailing_channel_id: bool = False,
    split_row_tokens: bool = False,
) -> bytes:
    if len(trailing_fields) != 10:
        raise ValueError("Stage3 [server] row probe needs exactly 10 trailing numeric fields")
    if post_float_token is None and not append_trailing_channel_id:
        # Dynamic disassembly clarified the token after the ten float fields:
        #
        #   0x01222C97  read token into [ebp-0xe4]
        #   0x01222D0D  call 0x1218CD0
        #
        # 0x1218CD0 accepts only localization tokens like <4::...> or quoted
        # backtick strings.  A raw numeric trailing channel id ("1"/"11") sets
        # the row error flag at [ebp-0x299], which prevents 0x01222D7D/8E/95
        # from committing the row into the UI lookup table.  For the local
        # synthetic single-channel profile, use a harmless backtick label and
        # keep the actual channel id in the leading row id plus ChannelInfo
        # display digits.
        post_float_token = f"`{channel_name}`"
    row_tokens = [
        str(channel_id),
        f"`{channel_name}`",
        "0",
        "`[granfloris]`",
        first_value,
        *trailing_fields,
    ]
    if append_trailing_channel_id:
        # USDOF's extracted PVF [server] rows carry the visible/internal
        # channel id as the final token after the ten recommendation fields:
        #
        #   `Start Area A` 0
        #   `[granfloris]` 5 0 0 0 0 0 0 0 0 0 0 11
        #
        # The Stage3 parser still accepts a Taiwan-like single-line prefix
        # (leading id + name + kind + group), but dynamic breakpoints at
        # 0x01222C97/0x01222D79 show it additionally calls one more token read
        # after the ten floats.  Without this trailing id it sets the row error
        # flag and never commits the row into the UI lookup table.
        #
        # Later static work showed this was only half-right: the extra token is
        # mandatory, but in this Stage3 parser it is normalized by 0x1218CD0 and
        # therefore must be a quoted/localization string.  Keep this numeric
        # tail available as a regression A/B profile (`minimal-valid-server-tail-id`)
        # because it precisely reproduces the old failure mode.
        row_tokens.append(str(channel_id))
    elif post_float_token:
        row_tokens.append(post_float_token)
    if split_row_tokens:
        row_lines = ["[server]", "1", *row_tokens, "[/server]"]
    else:
        row_lines = ["[server]", "1", "   ".join(row_tokens), "[/server]"]

    text = "\n".join(
        [
            "[dungeon]",
            "`[granfloris]`",
            "`Start Area`",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
            "1000",
            "[/dungeon]",
            "",
            *row_lines,
            "",
        ]
    )
    return text.encode("utf-8")


def build_minimal_valid_server_script_plaintext() -> bytes:
    """Return a synthetic one-channel script accepted by the Stage3 parser.

    Dynamic breakpoints around ``0x012225B5``/``0x01222D9F`` showed that the
    USDOF client accepts the Taiwan ``df_channel_r`` style token prefix:

    ``channel_id display_name kind dungeon_group first_value 10_float_fields post_label``.

    A later dynamic/static pass added the important USDOF suffix:

    ``... 10_numeric_fields quoted_or_localization_token``.

    The old numeric tail (``... 1``) reaches ``0x01222C97`` but fails inside
    ``0x1218CD0`` because numeric text is neither ``<...>`` nor a backtick
    string.  The current default therefore appends `` `Channel 1` `` as the
    post-float token and keeps the UI lookup id in the leading row id /
    ChannelInfo display digits.

    The old ``0.0`` fields were accepted poorly by the float parser; integer
    ``1`` is parsed into real ``1.0f`` values.  The packet-level ChannelInfo
    must expose the same numeric channel id via the display string
    (``Channel 1``), because ``0x015AC980`` extracts digits from that name and
    the UI lookup at ``0x0121F950`` searches ``(server_key, channel_id)``.
    """

    return _build_synthetic_one_channel_script_plaintext()


def build_minimal_valid_server_variant_plaintext(profile: str) -> tuple[bytes, str]:
    """Return focused A/B script rows for the live 0x01222BD1 float parser.

    Dynamic traces showed ``0.0`` tokens are consumed but leave the output
    floats at the client's ``FLT_MAX`` sentinel, so the valid-row commit path is
    skipped.  These variants keep the same one-channel shape and only alter the
    ten trailing numeric tokens / row tokenization so each run cleanly answers
    whether the parser expects integer-looking tokens, non-zero values, or
    line-separated tokens.
    """

    name = profile.strip().lower()
    if name == "minimal-valid-server-tail-id":
        return (
            _build_synthetic_one_channel_script_plaintext(
                trailing_fields=("1",) * 10,
                append_trailing_channel_id=True,
            ),
            "minimal-valid-server-tail-id:synthetic-one-channel-old-numeric-tail",
        )
    if name == "minimal-valid-server-post-label":
        return (
            _build_synthetic_one_channel_script_plaintext(
                trailing_fields=("1",) * 10,
                post_float_token="`Channel 1`",
            ),
            "minimal-valid-server-post-label:synthetic-one-channel",
        )
    if name == "minimal-valid-server-post-group":
        return (
            _build_synthetic_one_channel_script_plaintext(
                trailing_fields=("1",) * 10,
                post_float_token="`[granfloris]`",
            ),
            "minimal-valid-server-post-group:synthetic-one-channel",
        )
    if name == "minimal-valid-server-zero-int":
        return (
            _build_synthetic_one_channel_script_plaintext(trailing_fields=("0",) * 10),
            "minimal-valid-server-zero-int:synthetic-one-channel",
        )
    if name == "minimal-valid-server-int-ones":
        return (
            _build_synthetic_one_channel_script_plaintext(trailing_fields=("1",) * 10),
            "minimal-valid-server-int-ones:synthetic-one-channel",
        )
    if name == "minimal-valid-server-ones":
        return (
            _build_synthetic_one_channel_script_plaintext(trailing_fields=("1.0",) * 10),
            "minimal-valid-server-ones:synthetic-one-channel",
        )
    if name == "minimal-valid-server-lines":
        return (
            _build_synthetic_one_channel_script_plaintext(trailing_fields=("1",) * 10, split_row_tokens=True),
            "minimal-valid-server-lines:synthetic-one-channel",
        )
    if name == "minimal-valid-server-recommend":
        # Non-zero integer-looking recommendation vector.  The exact semantics
        # are still unknown; values are deliberately small and deterministic.
        return (
            _build_synthetic_one_channel_script_plaintext(
                trailing_fields=("1", "3", "11", "95", "1", "3", "11", "95", "1", "3")
            ),
            "minimal-valid-server-recommend:synthetic-one-channel",
        )
    if name == "minimal-valid-server-ch12":
        return (
            _build_synthetic_one_channel_script_plaintext(
                channel_id=12,
                channel_name="Channel 12",
                trailing_fields=("1",) * 10,
            ),
            "minimal-valid-server-ch12:synthetic-one-channel",
        )
    if name == "us-pvf-startarea-label":
        return (
            _build_synthetic_one_channel_script_plaintext(
                channel_id=11,
                channel_name="Start Area A",
                first_value="5",
                trailing_fields=("0",) * 10,
                post_float_token="`Start Area A`",
            ),
            "us-pvf-startarea-label:synthetic-one-channel",
        )
    raise ValueError(f"unknown synthetic script variant: {profile!r}")


def try_load_channel_script_from_pvf(
    pvf_path: str = PVF_PATH,
    *,
    raw: bool = False,
) -> tuple[bytes | None, str | None]:
    try:
        from pvf import pvf_runtime
    except Exception:
        return None, None
    path = Path(pvf_path)
    if not path.exists():
        return None, None
    try:
        reader = pvf_runtime.get_or_open_shared_reader(str(path))
        for candidate in SCRIPT_PATH_CANDIDATES:
            try:
                if raw:
                    data = pvf_runtime.read_raw(candidate, pvf_path=str(path))
                else:
                    text = pvf_runtime.read_text(candidate, pvf_path=str(path))
                    data = text.encode("utf-8")
                if data:
                    return data, candidate
            except KeyError:
                continue
        # Fallback: search by suffix once, but do not fail Channel startup if
        # the PVF path is absent from the index.
        for item in reader.list_paths():
            lower = item.lower()
            if lower.endswith("/channel_info.etc") or lower.endswith("\\channel_info.etc"):
                if raw:
                    return pvf_runtime.read_raw(item, pvf_path=str(path)), item
                return pvf_runtime.read_text(item, pvf_path=str(path)).encode("utf-8"), item
    except Exception:
        return None, None
    return None, None


def default_backup_channel_script_path(pvf_path: str = PVF_PATH) -> Path:
    return Path(pvf_path).parent / "pvfUtility_BackUpPvf" / "script" / "etc" / "channel_info.etc"


def default_backup_channel_script_dir(pvf_path: str = PVF_PATH) -> Path:
    return Path(pvf_path).parent / "pvfUtility_BackUpPvf" / "script" / "etc"


def default_user_channel_info_dir(pvf_path: str = PVF_PATH) -> Path:
    # User-provided extracted PVF channel directory:
    #   E:\Game\DNF\Study\pvf???????
    # Use unicode escapes here so this source file remains robust even if a
    # console/codepage displays Chinese path text incorrectly.
    return Path(pvf_path).parent.parent / "pvf\u91cc\u9762\u7684\u9891\u9053\u4fe1\u606f"


def default_tw_channel_info_path(pvf_path: str = PVF_PATH) -> Path:
    return Path(os.environ.get("USDOF_TW_CHANNEL_INFO_ETC") or (Path(pvf_path).parent.parent / "neople" / "channel" / "channel_info" / "channel_info.etc"))


def try_load_channel_script_from_backup(pvf_path: str = PVF_PATH) -> tuple[bytes | None, str | None]:
    """Load the user-extracted ``script/etc/channel_info.etc`` text verbatim.

    The extracted file preserves the ``#PVF_File`` text form and tab-separated
    token layout used by existing tooling, which is a useful A/B candidate
    against ``PvfReader.read_text()`` and raw NKPI entry bytes.
    """

    path = Path(os.environ.get("USDOF_BACKUP_CHANNEL_SCRIPT") or default_backup_channel_script_path(pvf_path))
    try:
        if path.exists() and path.is_file():
            data = path.read_bytes()
            if data:
                return data, str(path)
    except Exception:
        return None, None
    return None, None


def try_load_tw_channel_script(pvf_path: str = PVF_PATH) -> tuple[bytes | None, str | None]:
    path = default_tw_channel_info_path(pvf_path)
    try:
        if path.exists() and path.is_file():
            data = path.read_bytes()
            if data:
                return data, str(path)
    except Exception:
        return None, None
    return None, None


def iter_backup_channel_dirs(pvf_path: str = PVF_PATH) -> tuple[Path, ...]:
    """Return candidate dirs that may contain user-extracted channel scripts."""

    env_dir = os.environ.get("USDOF_BACKUP_CHANNEL_SCRIPT_DIR")
    dirs: list[Path] = []
    if env_dir:
        dirs.append(Path(env_dir))
    dirs.extend(
        [
            default_user_channel_info_dir(pvf_path),
            default_backup_channel_script_dir(pvf_path),
        ]
    )
    out: list[Path] = []
    seen: set[str] = set()
    for path in dirs:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            out.append(path)
    return tuple(out)


def try_load_channel_script_bundle_from_backup(pvf_path: str = PVF_PATH) -> tuple[bytes | None, str | None]:
    """Load a bundled set of channel-related PVF etc scripts.

    `backup-text` intentionally sends only ``channel_info.etc`` because it is
    the proven minimal accepted script payload.  This bundled profile is an A/B
    probe for the grey/disabled region issue: the client may also consult
    ``channeluiinfo.etc`` or ``channelrecommendlevel.etc`` before enabling
    channel selection.  Files are concatenated in a deterministic order and
    separated by newlines; the original extracted files remain read-only.
    """

    for directory in iter_backup_channel_dirs(pvf_path):
        try:
            if not directory.exists() or not directory.is_dir():
                continue
            parts: list[bytes] = []
            used: list[str] = []
            for name in CHANNEL_BUNDLE_FILENAMES:
                path = directory / name
                if not path.exists() or not path.is_file():
                    continue
                data = path.read_bytes()
                if not data:
                    continue
                if parts and not parts[-1].endswith(b"\n"):
                    parts.append(b"\n")
                parts.append(data)
                if not data.endswith(b"\n"):
                    parts.append(b"\n")
                used.append(name)
            if parts and "channel_info.etc" in used:
                return b"".join(parts), f"backup-channel-bundle:{directory}:" + ",".join(used)
        except Exception:
            continue
    return None, None


def normalize_script_profile(profile: str | None, *, prefer_pvf: bool = False) -> str:
    """Return the effective script-response profile.

    ``--prefer-pvf-script`` is kept for compatibility with the first prototype
    and maps to ``pvf-text``.  The new explicit ``pvf-raw`` profile is useful
    because the PVF entry has a binary/token form in addition to decoded text;
    dynamic traces can now A/B both forms without editing code.
    """

    name = (profile or os.environ.get("USDOF_SCRIPT_PROFILE") or "").strip().lower()
    if not name:
        name = "pvf-text" if prefer_pvf else "cain-pvf-paired"
    aliases = {
        "zero": "minimal-zero",
        "minimal": "minimal-zero",
        "minimal-zero-block": "minimal-zero",
        "pvf": "pvf-text",
        "text": "pvf-text",
        "pvf-decoded": "pvf-text",
        "raw": "pvf-raw",
        "pvf-binary": "pvf-raw",
        "backup": "backup-text",
        "extracted": "backup-text",
        "extracted-text": "backup-text",
        "bundle": "backup-channel-bundle",
        "backup-bundle": "backup-channel-bundle",
        "channel-bundle": "backup-channel-bundle",
        "backup-channel-dir": "backup-channel-bundle",
        "tw": "tw-channel-r",
        "taiwan": "tw-channel-r",
        "df-channel-r": "tw-channel-r",
        "valid-minimal": "minimal-valid-server",
        "minimal-valid": "minimal-valid-server",
        "minimal-server": "minimal-valid-server",
        "single-channel-script": "minimal-valid-server",
        "single-server": "minimal-valid-server",
        "tail-id": "minimal-valid-server-tail-id",
        "numeric-tail": "minimal-valid-server-tail-id",
        "old-tail-id": "minimal-valid-server-tail-id",
        "post-label": "minimal-valid-server-post-label",
        "label-tail": "minimal-valid-server-post-label",
        "post-group": "minimal-valid-server-post-group",
        "group-tail": "minimal-valid-server-post-group",
        "valid-zero-int": "minimal-valid-server-zero-int",
        "minimal-zero-int": "minimal-valid-server-zero-int",
        "zero-int": "minimal-valid-server-zero-int",
        "valid-int-ones": "minimal-valid-server-int-ones",
        "int-ones": "minimal-valid-server-int-ones",
        "ones-int": "minimal-valid-server-int-ones",
        "valid-ones": "minimal-valid-server-ones",
        "ones": "minimal-valid-server-ones",
        "valid-lines": "minimal-valid-server-lines",
        "lines": "minimal-valid-server-lines",
        "line-tokens": "minimal-valid-server-lines",
        "recommend": "minimal-valid-server-recommend",
        "recommended": "minimal-valid-server-recommend",
        "ch12": "minimal-valid-server-ch12",
        "channel12": "minimal-valid-server-ch12",
        "startarea": "us-pvf-startarea-label",
        "start-area": "us-pvf-startarea-label",
        "us-pvf-startarea": "us-pvf-startarea-label",
    }
    name = aliases.get(name, name)
    if name not in SCRIPT_PROFILES:
        raise ValueError(f"unknown script profile: {profile!r}; expected one of {', '.join(SCRIPT_PROFILES)}")
    return name




def build_cain_pvf_paired_script_plaintext(
    *,
    pvf_path: str = PVF_PATH,
    max_channels: int | None = None,
) -> tuple[bytes, str]:
    """Build multi-channel Stage3 script from PVF channel_info.etc.

    Raw PVF [server] rows end with a numeric trailing channel id. Stage3's
    post-float normalizer at 0x01218CD0 (via 0x01222D0D) rejects bare numbers
    and only accepts backtick / <loc> tokens, so raw pvf-text leaves the UI
    lookup table empty even when ChannelInfo lists many rows.

    This builder rewrites each PVF row into the Taiwan-like token shape that
    the dynamic breakpoints around 0x012225B5/0x01222D9F accepted for the
    single-channel case, but with one row per PVF channel:

      channel_id  `display`  kind  `group`  first  10x floats  `display`

    The leading channel_id is the same integer that ChannelInfo embeds in the
    display name digits (0x015AC980 -> 0x0121F950 lookup).
    """

    import os
    from channel.channel_info import (
        _filter_and_order_server_channel_rows,
        _read_pvf_channel_script_text,
        extract_server_channels_from_script_text,
    )

    text = _read_pvf_channel_script_text(str(Path(pvf_path)))
    rows = extract_server_channels_from_script_text(text, include_none_group=True, max_channels=None)
    rows = _filter_and_order_server_channel_rows(rows)
    if max_channels is None:
        env_max = os.environ.get("USDOF_CHANNELINFO_MAX_CHANNELS") or os.environ.get("USDOF_SCRIPT_MAX_CHANNELS")
        if env_max and env_max.strip():
            try:
                max_channels = int(env_max, 0)
            except ValueError:
                max_channels = None
    if max_channels is not None and max_channels > 0:
        rows = rows[:max_channels]
    if not rows:
        raise ValueError("cain-pvf-paired script: no PVF channel rows extracted")

    dungeon_parts: list[str] = []
    search_from = 0
    while True:
        start = text.find("[dungeon]", search_from)
        if start < 0:
            break
        end = text.find("[/dungeon]", start)
        if end < 0:
            break
        dungeon_parts.append(text[start : end + len("[/dungeon]")])
        search_from = end + len("[/dungeon]")

    row_lines: list[str] = ["[server]", "1"]
    for row in rows:
        display = row.name.replace("`", "")
        group = row.group_key if row.group_key.startswith("`") else f"`{row.group_key}`"
        first_value = str(int(getattr(row, "first_value", 0) or 0))
        recommend = getattr(row, "recommend", ()) or ()
        trailing = [str(int(v)) for v in recommend[:10]]
        if len(trailing) < 10:
            trailing.extend(["0"] * (10 - len(trailing)))
        tokens = [
            str(row.channel_id),
            f"`{display}`",
            str(row.kind),
            group,
            first_value,
            *trailing,
            f"`{display}`",
        ]
        row_lines.append("   ".join(tokens))
    row_lines.append("[/server]")

    sections: list[str] = []
    if dungeon_parts:
        sections.extend(dungeon_parts)
    else:
        # No synthetic dungeon fallback: keep only the PVF-derived [server]
        # block so missing PVF dungeon sections stay visible in logs/source.
        pass
    sections.append("")
    sections.append("\n".join(row_lines))
    sections.append("")
    plain = "\n".join(sections).encode("utf-8")
    return plain, f"cain-pvf-paired:rows={len(rows)}"

def build_script_plaintext(
    *,
    prefer_pvf: bool = False,
    pvf_path: str = PVF_PATH,
    profile: str | None = None,
) -> tuple[bytes, str]:
    effective_profile = normalize_script_profile(profile, prefer_pvf=prefer_pvf)
    if effective_profile == "minimal-valid-server":
        return build_minimal_valid_server_script_plaintext(), "minimal-valid-server:synthetic-one-channel"
    if effective_profile.startswith("minimal-valid-server-") or effective_profile == "us-pvf-startarea-label":
        return build_minimal_valid_server_variant_plaintext(effective_profile)
    if effective_profile in {"cain-pvf-paired", "pvf-cain-paired", "usdof-multi-channel"}:
        return build_cain_pvf_paired_script_plaintext(pvf_path=pvf_path)
    if effective_profile == "pvf-text":
        loaded, source = try_load_channel_script_from_pvf(pvf_path, raw=False)
        if loaded:
            return loaded, f"pvf-text:{source}"
        return build_minimal_script_plaintext(), "minimal-zero-block:fallback-after-pvf-text"
    if effective_profile == "pvf-raw":
        loaded, source = try_load_channel_script_from_pvf(pvf_path, raw=True)
        if loaded:
            return loaded, f"pvf-raw:{source}"
        return build_minimal_script_plaintext(), "minimal-zero-block:fallback-after-pvf-raw"
    if effective_profile == "backup-text":
        loaded, source = try_load_channel_script_from_backup(pvf_path)
        if loaded:
            return loaded, f"backup-text:{source}"
        return build_minimal_script_plaintext(), "minimal-zero-block:fallback-after-backup-text"
    if effective_profile == "backup-channel-bundle":
        loaded, source = try_load_channel_script_bundle_from_backup(pvf_path)
        if loaded:
            return loaded, source or "backup-channel-bundle"
        return build_minimal_script_plaintext(), "minimal-zero-block:fallback-after-backup-channel-bundle"
    if effective_profile == "tw-channel-r":
        loaded, source = try_load_tw_channel_script(pvf_path)
        if loaded:
            return loaded, f"tw-channel-r:{source}"
        return build_minimal_script_plaintext(), "minimal-zero-block:fallback-after-tw-channel-r"
    return build_minimal_script_plaintext(), "minimal-zero-block"


def build_script_payload(
    *,
    prefer_pvf: bool = False,
    pvf_path: str = PVF_PATH,
    profile: str | None = None,
) -> tuple[bytes, str]:
    plaintext, source = build_script_plaintext(prefer_pvf=prefer_pvf, pvf_path=pvf_path, profile=profile)
    return build_encrypted_packed_payload(plaintext), source


def build_script_packet(
    *,
    prefer_pvf: bool = False,
    pvf_path: str = PVF_PATH,
    profile: str | None = None,
    response_flag: int = CHANNEL_RESPONSE_FLAG,
) -> tuple[bytes, str]:
    payload, source = build_script_payload(prefer_pvf=prefer_pvf, pvf_path=pvf_path, profile=profile)
    return build_channel_packet(SCRIPT_RESPONSE_OPCODE, payload, flag=response_flag), source
