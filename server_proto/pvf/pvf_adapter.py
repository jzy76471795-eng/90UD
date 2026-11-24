from __future__ import annotations

from dataclasses import dataclass

from config import PVF_PATH
from pvf.nkpi_reader import PvfEntry, PvfReader
from pvf import pvf_runtime


@dataclass
class PvfAdapter:
    reader: PvfReader

    @classmethod
    def open_default(cls) -> "PvfAdapter":
        return cls(pvf_runtime.ensure_shared_reader(PVF_PATH))

    def list_town_related(self, limit: int = 200) -> list[str]:
        prefixes = ("world/", "worldmap/", "map/", "town/", "region/")
        out: list[str] = []
        for prefix in prefixes:
            for path in self.reader.list_paths(prefix):
                out.append(path)
                if len(out) >= limit:
                    return out
        return out

    def read_script(self, path: str) -> str:
        return self.reader.read_text(path)

    def get_entry(self, path: str) -> PvfEntry:
        return self.reader.get_entry(path)
