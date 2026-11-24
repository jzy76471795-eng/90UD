from __future__ import annotations

import json
from pathlib import Path

from pvf.nkpi_reader import PvfReader


def write_basic_index_cache(reader: PvfReader, out_path: str | Path, *, limit: int = 5000) -> None:
    paths = reader.list_paths()[:limit]
    payload = {
        "pvf": str(reader.path),
        "file_count": reader.file_count,
        "group_count": reader.group_count,
        "cached_path_count": len(paths),
        "paths": paths,
    }
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

