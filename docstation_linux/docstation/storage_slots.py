from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional


# ── Disk info ─────────────────────────────────────────────────────────────────

@dataclass
class DiskInfo:
    dev: str            # e.g. /dev/sdb
    by_path: str        # e.g. pci-0000:00:17.0-ata-2
    model: str = ""
    serial: str = ""
    size_bytes: int = 0
    fs_uuid: str = ""
    fs_label: str = ""
    mountpoint: str = ""

    @property
    def size_human(self) -> str:
        n = float(self.size_bytes or 0)
        for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
            if n < 1024 or unit == "ТБ":
                return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
            n /= 1024
        return str(self.size_bytes)

    @property
    def free_bytes(self) -> int:
        if not self.mountpoint:
            return 0
        try:
            st = os.statvfs(self.mountpoint)
            return st.f_bavail * st.f_frsize
        except Exception:
            return 0

    @property
    def free_human(self) -> str:
        n = float(self.free_bytes)
        for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
            if n < 1024 or unit == "ТБ":
                return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
            n /= 1024
        return "0"

    @property
    def label_short(self) -> str:
        parts = [self.model or self.serial or self.dev]
        if self.size_bytes:
            parts.append(self.size_human)
        return "  ".join(parts)


# ── Slot mapping ──────────────────────────────────────────────────────────────

@dataclass
class StorageSlotMapping:
    slot: int
    label: str
    path_match: str        # substring of by-path, e.g. "ata-1" or full "pci-0000:00:17.0-ata-1"
    enabled: bool = True   # operator selected this disk for archive
    notes: str = ""        # optional: disk model saved at calibration time


@dataclass
class StorageSlotMap:
    storage_slots: List[StorageSlotMapping] = field(default_factory=list)

    @staticmethod
    def load(path: str | Path) -> "StorageSlotMap":
        p = Path(path)
        if not p.exists():
            return StorageSlotMap()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            slots = []
            for item in data.get("storage_slots", []):
                slots.append(StorageSlotMapping(
                    slot=int(item.get("slot", 0)),
                    label=str(item.get("label", "")),
                    path_match=str(item.get("path_match", "")),
                    enabled=bool(item.get("enabled", True)),
                    notes=str(item.get("notes", "")),
                ))
            return StorageSlotMap(storage_slots=slots)
        except Exception:
            return StorageSlotMap()

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"storage_slots": [asdict(s) for s in self.storage_slots]}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def find_slot_for_disk(self, disk: DiskInfo) -> Optional[StorageSlotMapping]:
        """Return the slot mapping that matches this disk's by-path."""
        for s in self.storage_slots:
            if s.path_match and s.path_match in disk.by_path:
                return s
        return None

    def enabled_slots(self) -> List[StorageSlotMapping]:
        return [s for s in self.storage_slots if s.enabled]


# ── Disk enumeration ──────────────────────────────────────────────────────────

def _run(cmd: List[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def list_by_path_disks() -> Dict[str, str]:
    """Return {by_path_name: /dev/sdX} from /dev/disk/by-path/."""
    result: Dict[str, str] = {}
    by_path = Path("/dev/disk/by-path")
    if not by_path.exists():
        return result
    for entry in by_path.iterdir():
        name = entry.name
        # skip partitions
        if "part" in name:
            continue
        try:
            target = entry.resolve()
            result[name] = str(target)
        except Exception:
            pass
    return result


def _udev_props(dev: str) -> Dict[str, str]:
    out = _run(["udevadm", "info", "--query=property", f"--name={dev}"])
    props: Dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip()
    return props


def _lsblk_mounts() -> Dict[str, str]:
    """Return {/dev/sdX: mountpoint} from lsblk."""
    out = _run(["lsblk", "-J", "-o", "NAME,PATH,MOUNTPOINTS"])
    mounts: Dict[str, str] = {}
    try:
        import json as _json
        data = _json.loads(out)
        def walk(nodes):
            for n in nodes:
                path = n.get("path") or ("/dev/" + str(n.get("name", "")))
                mp_list = n.get("mountpoints") or []
                if isinstance(mp_list, list):
                    mp = next((str(x) for x in mp_list if x), "")
                else:
                    mp = str(mp_list or "")
                if mp:
                    mounts[path] = mp
                walk(n.get("children") or [])
        walk(data.get("blockdevices") or [])
    except Exception:
        pass
    return mounts


def list_storage_disks() -> List[DiskInfo]:
    """Return all physical SATA/NVMe/USB disks with their by-path names."""
    by_path_map = list_by_path_disks()   # by_path_name → /dev/sdX
    mounts = _lsblk_mounts()
    disks: List[DiskInfo] = []
    seen_devs: set[str] = set()

    for by_path_name, dev in sorted(by_path_map.items()):
        if dev in seen_devs:
            continue
        seen_devs.add(dev)
        props = _udev_props(dev)
        size_bytes = 0
        try:
            sz_path = Path(f"/sys/block/{Path(dev).name}/size")
            if sz_path.exists():
                size_bytes = int(sz_path.read_text().strip()) * 512
        except Exception:
            pass

        # mountpoint: check the disk itself, then its first partition
        mp = mounts.get(dev, "")
        if not mp:
            # try first partition
            for pdev, pmp in mounts.items():
                if pdev.startswith(dev) and pdev != dev:
                    mp = pmp
                    break

        disks.append(DiskInfo(
            dev=dev,
            by_path=by_path_name,
            model=(props.get("ID_MODEL") or props.get("ID_MODEL_ENC") or "").replace("\\x20", " ").strip(),
            serial=props.get("ID_SERIAL_SHORT") or props.get("ID_SERIAL") or "",
            size_bytes=size_bytes,
            fs_uuid=props.get("ID_FS_UUID") or "",
            fs_label=props.get("ID_FS_LABEL") or "",
            mountpoint=mp,
        ))

    return disks


def snapshot_by_path() -> set[str]:
    """Snapshot current set of by-path names (disks only, no partitions)."""
    return set(list_by_path_disks().keys())


def wait_for_new_disk(before: set[str], timeout_sec: int = 60) -> Optional[DiskInfo]:
    """Block until a new disk appears in /dev/disk/by-path/. Returns DiskInfo or None."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        time.sleep(0.5)
        current = list_by_path_disks()
        new_names = set(current.keys()) - before
        if new_names:
            # pick the one with the shortest/most meaningful name
            name = sorted(new_names)[0]
            dev = current[name]
            time.sleep(1.0)   # settle
            props = _udev_props(dev)
            size_bytes = 0
            try:
                sz_path = Path(f"/sys/block/{Path(dev).name}/size")
                if sz_path.exists():
                    size_bytes = int(sz_path.read_text().strip()) * 512
            except Exception:
                pass
            return DiskInfo(
                dev=dev,
                by_path=name,
                model=(props.get("ID_MODEL") or "").replace("\\x20", " ").strip(),
                serial=props.get("ID_SERIAL_SHORT") or props.get("ID_SERIAL") or "",
                size_bytes=size_bytes,
            )
    return None


def wait_for_disk_removal(dev: str, timeout_sec: int = 60) -> bool:
    """Block until the given /dev/sdX disappears."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if not Path(dev).exists():
            return True
    return False


# ── CLI calibration (non-GTK, used by scripts) ───────────────────────────────

def calibrate_storage_slots_cli(path: str | Path, count: int = 4) -> StorageSlotMap:
    """Interactive CLI calibration of SATA storage slots."""
    sm = StorageSlotMap.load(path)
    existing = {s.slot: s for s in sm.storage_slots}
    print(f"\n=== Калибровка дисковых слотов ({count} слотов) ===")
    print("Все архивные диски должны быть извлечены до начала.\n")

    new_slots: List[StorageSlotMapping] = []

    for slot_num in range(1, count + 1):
        print(f"--- Слот {slot_num} ---")
        print(f"Вставьте диск в физический слот {slot_num} и нажмите Enter...")
        input()
        before = snapshot_by_path()
        print("Ожидание появления диска (60 сек)...")
        disk = wait_for_new_disk(before, timeout_sec=60)
        if disk is None:
            print(f"  Диск не обнаружен за 60 сек. Слот {slot_num} пропущен.")
            continue
        print(f"  Обнаружен: {disk.dev}")
        print(f"  by-path:   {disk.by_path}")
        print(f"  Модель:    {disk.model or '—'}")
        print(f"  Серийный:  {disk.serial or '—'}")
        print(f"  Размер:    {disk.size_human}")
        label = f"Диск {slot_num}"
        new_slots.append(StorageSlotMapping(
            slot=slot_num,
            label=label,
            path_match=disk.by_path,
            enabled=existing.get(slot_num, StorageSlotMapping(slot_num, label, "")).enabled,
            notes=f"{disk.model} {disk.serial} {disk.size_human}".strip(),
        ))
        print(f"  Слот {slot_num} записан: {disk.by_path}")
        if slot_num < count:
            print(f"  Извлеките диск из слота {slot_num}...")
            wait_for_disk_removal(disk.dev, timeout_sec=60)
            print("  Диск извлечён.\n")

    result = StorageSlotMap(storage_slots=new_slots)
    result.save(path)
    print(f"\nКалибровка завершена. Сохранено в {path}")
    for s in result.storage_slots:
        print(f"  Слот {s.slot}: {s.path_match}  ({s.notes})")
    return result


# ── Best storage root selection ───────────────────────────────────────────────

def best_storage_root(slot_map: StorageSlotMap, subfolder: str = "docstation_archive") -> Optional[str]:
    """Return the mountpoint with the most free space among enabled, mounted slots."""
    disks = list_storage_disks()
    by_path_map = {d.by_path: d for d in disks}

    candidates: List[tuple[int, str]] = []  # (free_bytes, mountpoint)
    for slot in slot_map.enabled_slots():
        # find matching disk
        disk = next((d for bp, d in by_path_map.items() if slot.path_match in bp), None)
        if disk and disk.mountpoint:
            free = disk.free_bytes
            root = str(Path(disk.mountpoint) / subfolder)
            candidates.append((free, root))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]
