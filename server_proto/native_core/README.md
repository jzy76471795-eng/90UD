# Server native compute core

`usdof_core.cp312-win_amd64.pyd` is the x64 CPython extension for server-side
CPU-heavy, byte-heavy, and high-frequency homogeneous loops. It is separate
from both `pvf/native/` and the Win32 client components under `patches/`.

Private ABI version `10` currently exports:

```text
abi_version()
build_info()
character_flow_dispatch(opcode, body) -> (operation_id, handled)
plan_select_character(body, slot_indexes) -> (requested_slot, parse_source, selected_index, selection_kind)
concat_buffers(parts)
pack_u32le(values)
pack_userinfo1_rows(rows)
unpack_u32le(data)
build_game_header(flag, opcode, total, checksum, sub_or_seq, reserved, compact)
parse_game_header(data) -> (flag, opcode, total, checksum, sub_or_seq, header_view_size)
aes128_ecb_encrypt(data, key)
aes128_ecb_decrypt(data, key)
codec2_build_state(key_material)
codec2_encode(state, data)
codec2_decode(state, data)
codec13_build_state(key_material)
codec13_encode(state, data)
codec13_decode(state, data)
```

The Python facade in `native_core_loader.py` owns `USDOF_NATIVE_CORE` mode
selection, differential comparison, fallback accounting, timing, PE/CPython
gates, and the smoke vectors. `game/game_wire.py` uses the facade for the
fixed 16-byte and compact 13-byte Game header reader/writer; the Python output remains
authoritative in `shadow` mode. Business modules must call the facade rather
than importing `usdof_core` directly.

`channel/channel_crypto.py` uses the same facade for the existing AES-128 ECB
byte transform used by Channel Script/ChannelInfo response bodies and Game
codec4. Padding, zlib packing, response ordering, and all Channel/Game state
transitions remain Python-owned.

`game/game_codec.py` uses the facade for the existing codec2 key schedule and
fixed 16-byte block loop used by USERINFO and other opcode-index-2 bodies.
Python still owns key-material selection, length validation, the byte-for-byte
reference implementation, protocol field interpretation, and all state
transitions; native code only builds the deterministic 0x4C-byte state and
transforms aligned bytes.

The same facade now owns codec13's fixed 0x28-byte key schedule and aligned
0x08-byte ARX64 block loop. Python remains the byte-for-byte reference and
keeps padding, key selection, and opcode routing; native mode only replaces
the homogeneous transform.

`game/character/helper-userinfo1_equipment.py` uses the same facade for the
homogeneous Stage3 `USERINFO1/sub_166AED0` row writer. Python still resolves
metadata, filters equipment, derives enchant/amplify fields, creates avatar
socket blobs, sorts rows, and controls the packet sequence. The native
`pack_userinfo1_rows` kernel receives only normalized fixed-width fields and
already encoded avatar blobs, and returns the byte-identical `u8 count + rows`
body. The old single-row Python packer remains available as the reference
implementation.

`character_flow_dispatch` is the dual-branch routing ABI. SELECT_CHARACTER is
the first `handled=1` operation; the remaining role-page/enter-Seria opcodes
still return `handled=0`. `plan_select_character` reproduces the frozen
request-body and roster rules: exact DB slot match, legacy list-index match,
then first-character fallback. Python retains SQLite, socket/PIV waits, and
the complete fallback handler while native action-plan coverage expands.

Modes:

```text
USDOF_NATIVE_CORE=0       Python implementation
USDOF_NATIVE_CORE=shadow  Python result with native byte-for-byte comparison
USDOF_NATIVE_CORE=1       Native result with measured Python fallback on error
```

Build and verify with the fixed server interpreter:

```powershell
powershell -File server_proto\native_core\build_with_llvm_mingw.ps1 -Verify
```

The build fixes CPython 3.12.10 x64, AMD64 PE, LLVM-MinGW, optimization level,
output name, and import library. The verifier checks the interpreter, PE
machine, exact module path, ABI, build metadata, buffer interoperability, and
deterministic differential vectors.
