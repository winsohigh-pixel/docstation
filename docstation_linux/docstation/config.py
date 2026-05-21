from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List


@dataclass
class StationConfig:
    station_id: str = "Dock-01"
    storage_root: str = "/var/lib/docstation/archive"
    database_path: str = "data/docstation.sqlite3"
    mount_root: str = "/mnt/docstation"
    device_ini_path: str = "Device.ini"
    dock_slots_path: str = "DockSlots.json"
    storage_slots_path: str = "StorageSlots.json"
    vendor_vid: str = "4255"
    vendor_pid: str = "0001"
    switch_to_disk_delay_ms: int = 5000
    disk_settle_ms: int = 2500
    switch_timeout_sec: int = 75
    max_parallel_imports: int = 6
    max_parallel_imports_per_hub: int = 3
    copy_buffer_mb: int = 4
    hash_during_copy: bool = True
    import_delete_source: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    file_extensions: List[str] = field(default_factory=lambda: [
        ".mp4", ".mov", ".avi", ".mkv", ".jpg", ".jpeg", ".png", ".wav", ".aac"
    ])

    @staticmethod
    def load(path: str | Path = "StationConfig.linux.json") -> "StationConfig":
        p = Path(path)
        cfg = StationConfig()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            keymap = {k.lower(): k for k in cfg.__dataclass_fields__}
            for raw_key, value in data.items():
                key = keymap.get(raw_key.lower())
                if key:
                    setattr(cfg, key, value)
        return cfg

    def save(self, path: str | Path = "StationConfig.linux.json") -> None:
        p = Path(path)
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def vendor_vid_int(self) -> int:
        return int(str(self.vendor_vid).replace("0x", ""), 16)

    @property
    def vendor_pid_int(self) -> int:
        return int(str(self.vendor_pid).replace("0x", ""), 16)


def resolve_relative(path: str | Path, base: Path | None = None) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (base or Path.cwd()) / p
