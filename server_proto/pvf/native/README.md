# Server PVF native module

`usdof_pvf_native.cp312-win_amd64.pyd` is the server-side CPython extension
for deterministic PVF byte transforms and directory queries. It is unrelated
to the Stage3 client injection DLLs.

The private ABI version is `1` and exports:

```text
abi_version()
decrypt_lcg(data, seed, increment)
build_directory(entry_table, utf8_pool, utf16_pool)
load_directory(blob)
dump_directory(directory)
decode_token_text(data, utf8_pool, utf16_pool)
```

`NativeDirectory` owns immutable entries, paths, and a native lookup table. It
supports `lookup`, `path_at`, `entry_at`, `iter_prefix`, `search`,
`first_by_type`, `validate`, and `len`.
Duplicate normalized paths keep their original order while lookup resolves to
the last entry, matching the existing Python reader.

The production reader persists `dump_directory()` inside a checksummed
`pvf_directory.bin` envelope bound to canonical path, complete PVF SHA-256,
profile, counts, CPython version, native ABI, and directory semantic version.
Cache recovery repeats native entry/group range validation before publishing
the immutable directory.

Build and verify with the fixed server interpreter:

```powershell
powershell -File server_proto\pvf\native\build_with_llvm_mingw.ps1 -Verify
```

The build script fixes the output name, x64 target, CPython include/import
library, compiler family, and optimization level. The verifier checks CPython
3.12.10 x64, PE AMD64, exact import path, ABI, and frozen smoke vectors.
