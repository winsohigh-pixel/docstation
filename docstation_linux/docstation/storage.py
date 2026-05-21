from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass
class UsbDisk:
    path: str
    name: str
    size: int
    model: str = ""
    serial: str = ""
    vendor: str = ""
    tran: str = ""
    hotplug: str = ""
    rm: str = ""
    mountpoint: str = ""
    children: List[dict] = None

    @property
    def human(self) -> str:
        size_gib = self.size / (1024 ** 3) if self.size else 0
        label = " ".join(x for x in [self.vendor, self.model, self.serial] if x).strip()
        return f"{self.path} | {size_gib:.1f} GiB" + (f" | {label}" if label else "")


def run_cmd(cmd: List[str], timeout: Optional[int] = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=check)


def lsblk_usb_disks() -> Dict[str, UsbDisk]:
    cp = run_cmd(["lsblk", "-J", "-b", "-o", "NAME,PATH,PKNAME,TYPE,TRAN,SIZE,MODEL,SERIAL,VENDOR,HOTPLUG,RM,FSTYPE,MOUNTPOINTS"], timeout=10)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "lsblk failed")
    data = json.loads(cp.stdout)
    out: Dict[str, UsbDisk] = {}

    def first_mount(n: dict) -> str:
        m = n.get("mountpoints") or []
        if isinstance(m, list):
            return next((str(x) for x in m if x), "")
        return str(m or "")

    def walk(nodes: Iterable[dict]) -> None:
        for n in nodes:
            if n.get("type") == "disk":
                path = n.get("path") or ("/dev/" + str(n.get("name", "")))
                tran = str(n.get("tran") or "")
                hotplug = str(n.get("hotplug") or "")
                rm = str(n.get("rm") or "")
                if tran.lower() == "usb" or hotplug == "1" or rm == "1":
                    try:
                        size = int(n.get("size") or 0)
                    except Exception:
                        size = 0
                    children = n.get("children") or []
                    out[path] = UsbDisk(
                        path=path,
                        name=str(n.get("name") or ""),
                        size=size,
                        model=str(n.get("model") or "").strip(),
                        serial=str(n.get("serial") or "").strip(),
                        vendor=str(n.get("vendor") or "").strip(),
                        tran=tran,
                        hotplug=hotplug,
                        rm=rm,
                        mountpoint=first_mount(n),
                        children=children,
                    )
            children = n.get("children") or []
            if children:
                walk(children)
    walk(data.get("blockdevices") or [])
    return out


def wait_for_new_usb_disk(before: Dict[str, UsbDisk], timeout_sec: int, logger=None) -> Optional[UsbDisk]:
    deadline = time.monotonic() + timeout_sec
    last_seen = None
    while time.monotonic() < deadline:
        now = lsblk_usb_disks()
        new_paths = sorted(set(now) - set(before))
        if new_paths:
            disk = now[new_paths[0]]
            if logger:
                logger.log(f"New USB disk detected: {disk.human}")
            return disk
        count = len(now)
        if logger and count != last_seen:
            logger.log(f"Waiting for new USB disk... current USB disks: {count}")
            last_seen = count
        time.sleep(1.0)
    return None


def block_to_mount_device(disk: UsbDisk) -> str:
    # Prefer the first partition. If there is no partition table, mount the disk itself.
    for ch in disk.children or []:
        if ch.get("type") == "part":
            return ch.get("path") or f"/dev/{ch.get('name')}"
    return disk.path


def mounted_path_for_device(dev_path: str) -> str:
    cp = run_cmd(["lsblk", "-J", "-o", "PATH,MOUNTPOINTS", dev_path], timeout=10)
    if cp.returncode != 0:
        return ""
    try:
        data = json.loads(cp.stdout)
        nodes = data.get("blockdevices") or []
        for n in nodes:
            mounts = n.get("mountpoints") or []
            if isinstance(mounts, list):
                for m in mounts:
                    if m:
                        return str(m)
            elif mounts:
                return str(mounts)
    except Exception:
        pass
    return ""


def mount_device_readonly(dev_path: str, mount_root: str | Path, logger=None) -> str:
    existing = mounted_path_for_device(dev_path)
    if existing:
        return existing
    mount_root = Path(mount_root)
    mount_root.mkdir(parents=True, exist_ok=True)

    # Prefer udisksctl because it picks correct filesystem helpers and works well on Ubuntu Desktop.
    cp = run_cmd(["udisksctl", "mount", "-b", dev_path, "-o", "ro"], timeout=30)
    if cp.returncode == 0:
        text = (cp.stdout + "\n" + cp.stderr).strip()
        # Example: Mounted /dev/sdd1 at /media/user/VOLUME.
        marker = " at "
        if marker in text:
            mountpoint = text.split(marker, 1)[1].strip().rstrip(".")
            return mountpoint
        mp = mounted_path_for_device(dev_path)
        if mp:
            return mp

    safe_name = Path(dev_path).name.replace("/", "_")
    target = mount_root / safe_name
    target.mkdir(parents=True, exist_ok=True)
    cp2 = run_cmd(["mount", "-o", "ro", dev_path, str(target)], timeout=30)
    if cp2.returncode != 0:
        raise RuntimeError((cp.stderr + "\n" + cp2.stderr).strip() or f"Cannot mount {dev_path}")
    return str(target)


def unmount_path(mountpoint: str, logger=None) -> None:
    if not mountpoint:
        return
    cp = run_cmd(["udisksctl", "unmount", "-b", mountpoint], timeout=20)
    if cp.returncode == 0:
        return
    run_cmd(["umount", mountpoint], timeout=20)


def find_import_files(root: str | Path, extensions: Iterable[str]) -> List[Path]:
    root = Path(root)
    exts = {e.lower() for e in extensions}
    result: List[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            result.append(p)
    return sorted(result)


def sha256_file(path: Path, buffer_mb: int = 4) -> str:
    h = hashlib.sha256()
    buf_size = max(1, buffer_mb) * 1024 * 1024
    with path.open("rb") as f:
        while True:
            b = f.read(buf_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def unique_dest(base_dir: Path, file_name: str) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    candidate = base_dir / file_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    i = 2
    while True:
        c = base_dir / f"{stem}_{i}{suffix}"
        if not c.exists():
            return c
        i += 1


def copy_file(src: Path, dest: Path, buffer_mb: int = 4) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    buf_size = max(1, buffer_mb) * 1024 * 1024
    with src.open("rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout, length=buf_size)
        fout.flush()
        os.fsync(fout.fileno())
    shutil.copystat(src, dest, follow_symlinks=True)
