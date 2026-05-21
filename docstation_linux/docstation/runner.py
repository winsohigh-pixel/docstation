from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import amba
from .config import StationConfig, resolve_relative
from .db import StationDatabase
from .loggingx import Logger
from .slots import SlotMap, format_candidate
from .storage import (
    UsbDisk, block_to_mount_device, copy_file, find_import_files, lsblk_usb_disks,
    mount_device_readonly, sha256_file, unmount_path, wait_for_new_usb_disk,
)


@dataclass
class ImportResult:
    disk: str
    ok: bool
    files: int = 0
    bytes: int = 0
    error: str = ""


class DocStationRunner:
    def __init__(self, config: StationConfig, db: StationDatabase, logger: Logger):
        self.config = config
        self.db = db
        self.logger = logger
        self.password = amba.load_default_password(config.device_ini_path)
        self.slot_map = SlotMap.load(config.dock_slots_path)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def set_header(self, total: int = 0, queue: int = 0, connected: int = 0, importing: int = 0, ready: int = 0, errors: int = 0) -> None:
        self.db.upsert_status("header", f"Регистраторы: всего {total} · очередь скачивания {queue} · подключены {connected} · скачивание {importing} · готовы {ready}" + (f" · ошибки {errors}" if errors else ""))
        self.db.upsert_status("total", str(total))
        self.db.upsert_status("queue", str(queue))
        self.db.upsert_status("connected", str(connected))
        self.db.upsert_status("importing", str(importing))
        self.db.upsert_status("ready", str(ready))
        self.db.upsert_status("errors", str(errors))

    def _slot_number_for_candidate(self, cand: amba.AmbarellaCandidate) -> Optional[int]:
        return self.slot_map.slot_for_candidate(cand)

    def clear_slot_statuses(self) -> None:
        # The operator screen is slot-based. Clear mapped slots at the start of a new cycle.
        for row in self.slot_map.rows():
            n = row.slot
            self.db.upsert_status(f"slot_{n}_state", "пусто")
            self.db.upsert_status(f"slot_{n}_device", "")
            self.db.upsert_status(f"slot_{n}_officer", "")
            self.db.upsert_status(f"slot_{n}_detail", row.usb_path)
            self.db.upsert_status(f"slot_{n}_storage", "—")
            self.db.upsert_status(f"slot_{n}_storage_progress", "0")
            self.db.upsert_status(f"slot_{n}_total_progress", "0")
            self.db.upsert_status(f"slot_{n}_file_progress", "0")
            self.db.upsert_status(f"slot_{n}_current_file", "")
            self.db.upsert_status(f"slot_{n}_speed", "")
            self.db.upsert_status(f"slot_{n}_eta", "")
            self.db.upsert_status(f"slot_{n}_battery", "нет устройства")

    @staticmethod
    def _parse_storage_pair(storage: str) -> tuple[float, float]:
        try:
            text = (storage or "").replace(" ", "").replace(",", ".").upper()
            if "/" not in text:
                return 0.0, 0.0
            left, right = text.split("/", 1)

            def num(x: str) -> float:
                keep = ''.join(ch for ch in x if ch.isdigit() or ch == '.')
                return float(keep) if keep else 0.0

            return num(left), num(right)
        except Exception:
            return 0.0, 0.0

    @staticmethod
    def _storage_progress_percent(storage: str) -> str:
        # Ambarella returns "free/total" here. The operator needs useful occupied
        # recording volume, so show total-free, not free space.
        free, total = DocStationRunner._parse_storage_pair(storage)
        if total <= 0:
            return "0"
        used = max(0.0, min(total, total - free))
        return str(max(0, min(100, int(round((used / total) * 100)))))

    @staticmethod
    def _format_used_storage(storage: str) -> str:
        """Форматирует строку памяти из Ambarella info для отображения в ячейке."""
        free, total = DocStationRunner._parse_storage_pair(storage)
        if total <= 0:
            return storage or "—"
        used = max(0.0, min(total, total - free))
        return f"{used:.1f} / {total:.1f} ГБ"

    @staticmethod
    def _format_import_storage(total_files_bytes: int, copied_bytes: int, disk_total_bytes: int) -> str:
        """Форматирует строку памяти во время импорта.
        Показывает сколько скопировано из общего объёма файлов / объём накопителя.
        """
        if disk_total_bytes <= 0:
            # нет инфо о накопителе — просто скопировано/всего файлов
            total_mb = total_files_bytes / (1024 * 1024)
            done_mb  = copied_bytes / (1024 * 1024)
            if total_mb >= 1024:
                return f"{done_mb/1024:.1f} / {total_mb/1024:.1f} ГБ"
            return f"{done_mb:.0f} / {total_mb:.0f} МБ"
        # есть инфо о накопителе
        done_gb  = copied_bytes / (1024 ** 3)
        total_gb = total_files_bytes / (1024 ** 3)
        disk_gb  = disk_total_bytes / (1024 ** 3)
        return f"{done_gb:.1f} / {total_gb:.1f} ГБ  (диск {disk_gb:.0f} ГБ)"

    def _slot_number(self, cand: amba.AmbarellaCandidate) -> Optional[int]:
        return self._slot_number_for_candidate(cand)


    def _hub_group_for_usb_path(self, usb_path: str | None) -> str:
        """Return a stable import group for USB topology path.

        For two external hubs on different motherboard USB branches the first
        component is usually different, for example 3.* and 14.*. We keep a
        fixed per-group import limit so that one hub cannot steal all six
        workers when the other hub is empty.
        """
        text = str(usb_path or "").strip()
        if text.startswith("ports="):
            text = text.split("=", 1)[1].strip()
        if not text:
            return "unknown"
        return text.split(".", 1)[0] or text

    def _hub_group_for_candidate(self, cand: amba.AmbarellaCandidate) -> str:
        return self._hub_group_for_usb_path(getattr(cand, "port_path", ""))

    def _hub_group_label(self, group: str) -> str:
        return f"ветка {group}" if group and group != "unknown" else "ветка неизвестна"

    def _active_import_counts_by_hub(self, futures: dict) -> dict[str, int]:
        counts: dict[str, int] = {}
        for cand, _disk in futures.values():
            group = self._hub_group_for_candidate(cand)
            counts[group] = counts.get(group, 0) + 1
        return counts

    def _start_waiting_imports(self, waiting_import: list[tuple[amba.AmbarellaCandidate, UsbDisk]], futures: dict, pool: concurrent.futures.ThreadPoolExecutor) -> int:
        """Start imports with global and per-hub limits.

        Global default: 6 imports. Per USB branch default: 3 imports.
        If one branch has no waiting recorders, the other branch still stays at
        its per-hub limit instead of borrowing all global workers.
        """
        started = 0
        global_limit = max(1, int(getattr(self.config, "max_parallel_imports", 1) or 1))
        per_hub_limit = max(1, int(getattr(self.config, "max_parallel_imports_per_hub", global_limit) or global_limit))
        while waiting_import and len(futures) < global_limit and not self._stop.is_set():
            counts = self._active_import_counts_by_hub(futures)
            chosen_index = None
            chosen_group = "unknown"
            for idx, (cand, _disk) in enumerate(waiting_import):
                group = self._hub_group_for_candidate(cand)
                if counts.get(group, 0) < per_hub_limit:
                    chosen_index = idx
                    chosen_group = group
                    break
            if chosen_index is None:
                break
            cand, disk = waiting_import.pop(chosen_index)
            fut = pool.submit(self.import_one_disk, cand, disk)
            futures[fut] = (cand, disk)
            started += 1
            self.logger.log(
                f"IMPORT QUEUED {format_candidate(cand, self.slot_map)} {disk.path} "
                f"hub={self._hub_group_label(chosen_group)} "
                f"active_hub={counts.get(chosen_group, 0) + 1}/{per_hub_limit} "
                f"active_total={len(futures)}/{global_limit}"
            )
        return started

    def _reserved_import_counts_by_hub(self, waiting_import: list[tuple[amba.AmbarellaCandidate, UsbDisk]], futures: dict) -> dict[str, int]:
        """Count active and already switched disks as reserved import slots.

        In continuous mode a recorder must stay in Ambarella/vendor mode until
        there is real capacity to import it. A disk that has just appeared and
        waits for settle/import already occupies that hub slot.
        """
        counts = self._active_import_counts_by_hub(futures)
        for cand, _disk in waiting_import:
            group = self._hub_group_for_candidate(cand)
            counts[group] = counts.get(group, 0) + 1
        return counts

    def _has_import_capacity_for(self, cand: amba.AmbarellaCandidate, waiting_import: list[tuple[amba.AmbarellaCandidate, UsbDisk]], futures: dict) -> bool:
        global_limit = max(1, int(getattr(self.config, "max_parallel_imports", 1) or 1))
        per_hub_limit = max(1, int(getattr(self.config, "max_parallel_imports_per_hub", global_limit) or global_limit))
        reserved_total = len(futures) + len(waiting_import)
        if reserved_total >= global_limit:
            return False
        group = self._hub_group_for_candidate(cand)
        counts = self._reserved_import_counts_by_hub(waiting_import, futures)
        return counts.get(group, 0) < per_hub_limit

    def _pop_next_switch_candidate(self, queued: list[amba.AmbarellaCandidate], waiting_import: list[tuple[amba.AmbarellaCandidate, UsbDisk]], futures: dict) -> tuple[Optional[amba.AmbarellaCandidate], str]:
        """Pick a recorder that is still in Ambarella mode and can be imported now.

        This is the key stability change: do not convert every detected
        recorder to Mass Storage. Keep it in Ambarella mode until a real import
        slot is free on its USB branch.
        """
        for idx, cand in enumerate(queued):
            if self._has_import_capacity_for(cand, waiting_import, futures):
                return queued.pop(idx), self._hub_group_for_candidate(cand)
        return None, ""

    def set_slot_field(self, cand: amba.AmbarellaCandidate, name: str, value: str) -> None:
        slot = self._slot_number(cand)
        if slot is not None:
            self.db.upsert_status(f"slot_{slot}_{name}", str(value or ""))

    def set_slot_progress(self, cand: amba.AmbarellaCandidate, total: float = 0, file: float = 0, current_file: str = "", speed: str = "", eta: str = "") -> None:
        slot = self._slot_number(cand)
        if slot is None:
            return
        self.db.upsert_status(f"slot_{slot}_total_progress", str(max(0, min(100, int(round(total))))))
        self.db.upsert_status(f"slot_{slot}_file_progress", str(max(0, min(100, int(round(file))))))
        self.db.upsert_status(f"slot_{slot}_current_file", current_file or "")
        self.db.upsert_status(f"slot_{slot}_speed", speed or "")
        self.db.upsert_status(f"slot_{slot}_eta", eta or "")

    def set_slot_status(self, cand: amba.AmbarellaCandidate, state: str, detail: str = "") -> None:
        slot = self._slot_number_for_candidate(cand)
        if slot is None:
            return
        device_id = ""
        officer_id = ""
        try:
            device_id = str((cand.info or {}).get("device_id") or "")
            officer_id = str((cand.info or {}).get("officer_id") or "")
        except Exception:
            pass
        self.db.upsert_status(f"slot_{slot}_state", state)
        self.db.upsert_status(f"slot_{slot}_device", device_id)
        self.db.upsert_status(f"slot_{slot}_officer", officer_id)
        # Do not expose technical USB port/path on the operator screen.
        self.db.upsert_status(f"slot_{slot}_detail", detail or "")
        self.db.upsert_status(f"slot_{slot}_battery", "регистратор / зарядка" if state and state != "пусто" else "нет устройства")
        storage = str((cand.info or {}).get("storage") or "")
        if storage:
            self.db.upsert_status(f"slot_{slot}_storage", self._format_used_storage(storage))
            self.db.upsert_status(f"slot_{slot}_storage_progress", self._storage_progress_percent(storage))

    def read_vendor_info(self, candidates: List[amba.AmbarellaCandidate]) -> List[amba.AmbarellaCandidate]:
        ok: List[amba.AmbarellaCandidate] = []
        self.logger.log("--- Reading startup info in Ambarella/vendor mode ---")
        for i, cand in enumerate(candidates, 1):
            if self._stop.is_set():
                break
            dev = amba.find_device_again(cand)
            if dev is None:
                self.logger.log(f"[{i:02d}] INFO FAIL {cand.human}: device disappeared")
                continue
            try:
                self.set_slot_status(cand, "считывание")
                with amba.AmbarellaSession(dev) as s:
                    cand.info = s.read_info(self.password, full=False)
                ok.append(cand)
                self.set_slot_status(cand, "ожидает скачивания")
                info = " ".join(f"{k}={v}" for k, v in cand.info.items())
                self.logger.log(f"[{i:02d}] INFO OK {format_candidate(cand, self.slot_map)} {info}")
            except Exception as exc:
                self.set_slot_status(cand, "ошибка", str(exc))
                self.logger.log(f"[{i:02d}] INFO FAIL {format_candidate(cand, self.slot_map)}: {exc}")
        self.logger.log(f"Info read OK: {len(ok)}/{len(candidates)}")
        return ok

    def switch_sequential(self, candidates: List[amba.AmbarellaCandidate]) -> List[tuple[amba.AmbarellaCandidate, UsbDisk]]:
        disks: List[tuple[amba.AmbarellaCandidate, UsbDisk]] = []
        before = lsblk_usb_disks()
        total = len(candidates)
        self.logger.log("--- Sequential switch to Mass Storage ---")
        for index, cand in enumerate(candidates, 1):
            if self._stop.is_set():
                break
            self.set_header(total=total, queue=total - index + 1, connected=len(disks), importing=0, ready=0)
            self.set_slot_status(cand, "пауза перед подключением", f"{self.config.switch_to_disk_delay_ms} мс")
            self.logger.log(f"[{index:02d}/{total}] Queue item: {format_candidate(cand, self.slot_map)}")
            delay = max(0, self.config.switch_to_disk_delay_ms)
            self.logger.log(f"[{index:02d}] Delay before switch: {delay} ms")
            time.sleep(delay / 1000.0)
            self.set_slot_status(cand, "переключение в диск")
            dev = amba.find_device_again(cand)
            if dev is None:
                self.set_slot_status(cand, "ошибка", "пропал перед переключением")
                self.logger.log(f"[{index:02d}] SWITCH FAIL: device disappeared before switch")
                continue
            try:
                with amba.AmbarellaSession(dev) as s:
                    s.switch_to_disk(self.password)
                self.logger.log(f"[{index:02d}] SWITCH COMMAND SENT")
                disk = wait_for_new_usb_disk(before, self.config.switch_timeout_sec, logger=self.logger)
                if disk is None:
                    self.set_slot_status(cand, "ошибка", "диск не появился")
                    self.logger.log(f"[{index:02d}] SWITCH FAIL: no new USB disk")
                    continue
                disks.append((cand, disk))
                self.set_slot_status(cand, "подключён", disk.path)
                before = lsblk_usb_disks()
                self.set_header(total=total, queue=total - index, connected=len(disks), importing=0, ready=0)
                if self.config.disk_settle_ms > 0:
                    self.logger.log(f"[{index:02d}] Disk settle delay: {self.config.disk_settle_ms} ms")
                    time.sleep(self.config.disk_settle_ms / 1000.0)
            except Exception as exc:
                self.set_slot_status(cand, "ошибка", str(exc))
                self.logger.log(f"[{index:02d}] SWITCH FAIL {format_candidate(cand, self.slot_map)}: {exc}")
        return disks

    def import_one_disk(self, cand: amba.AmbarellaCandidate, disk: UsbDisk) -> ImportResult:
        dev = block_to_mount_device(disk)
        mountpoint = ""
        copied = 0
        total_bytes = 0
        try:
            self.set_slot_status(cand, "скачивание")
            self.set_slot_progress(cand, total=0, file=0, current_file="подготовка", speed="", eta="")
            mountpoint = mount_device_readonly(dev, self.config.mount_root, logger=self.logger)
            self.logger.log(f"IMPORT START {disk.path} mounted at {mountpoint}")
            files = find_import_files(mountpoint, self.config.file_extensions)
            device_id = cand.info.get("device_id") or "unknown-device"
            officer_id = cand.info.get("officer_id") or "unknown-officer"
            day = dt.datetime.now().strftime("%Y-%m-%d")
            dest_dir = resolve_relative(self.config.storage_root) / day / device_id
            total_size = 0
            file_sizes: List[int] = []
            for src in files:
                try:
                    size = src.stat().st_size
                except Exception:
                    size = 0
                file_sizes.append(size)
                total_size += size
            slot = self._slot_number(cand)
            # Объём накопителя из info (Ambarella вернул "free/total")
            raw_storage = str((cand.info or {}).get("storage") or "")
            _free_gb, disk_total_gb = self._parse_storage_pair(raw_storage)
            # Сколько байт уже было занято на накопителе до скачивания
            # (total_size — байты файлов для скачивания)
            disk_total_bytes = int(disk_total_gb * 1024 * 1024 * 1024) if disk_total_gb > 0 else 0
            if slot is not None:
                storage_str = self._format_import_storage(total_size, 0, disk_total_bytes)
                self.db.upsert_status(f"slot_{slot}_storage", storage_str)
                storage_pct = str(int(total_size / disk_total_bytes * 100)) if disk_total_bytes > 0 else "0"
                self.db.upsert_status(f"slot_{slot}_storage_progress", storage_pct)
            done_bytes = 0
            buf_size = max(1, self.config.copy_buffer_mb) * 1024 * 1024
            for idx, src in enumerate(files):
                if self._stop.is_set():
                    break
                rel_name = src.name
                dest = self._unique_dest(dest_dir, rel_name)
                expected = file_sizes[idx] if idx < len(file_sizes) else 0
                h = hashlib.sha256() if self.config.hash_during_copy else None
                file_done = 0
                started = time.monotonic()
                last_ui = 0.0
                dest.parent.mkdir(parents=True, exist_ok=True)
                with src.open("rb") as fin, dest.open("wb") as fout:
                    while True:
                        chunk = fin.read(buf_size)
                        if not chunk:
                            break
                        fout.write(chunk)
                        if h is not None:
                            h.update(chunk)
                        n = len(chunk)
                        file_done += n
                        total_bytes += n
                        now = time.monotonic()
                        if now - last_ui >= 0.5 or file_done >= expected:
                            elapsed = max(0.001, now - started)
                            speed_bps = file_done / elapsed
                            total_pct = ((done_bytes + file_done) / total_size * 100.0) if total_size else 0.0
                            file_pct = (file_done / expected * 100.0) if expected else 0.0
                            speed = self._format_speed(speed_bps)
                            remain = max(0.0, (total_size - (done_bytes + file_done)) / speed_bps) if speed_bps > 0 and total_size else 0.0
                            eta = self._format_eta(remain)
                            self.set_slot_progress(cand, total=total_pct, file=file_pct, current_file=rel_name, speed=speed, eta=eta)
                            # FIX 3: обновлять отображение памяти в процессе копирования
                            if slot is not None:
                                copied_so_far = done_bytes + file_done
                                storage_str = self._format_import_storage(total_size, copied_so_far, disk_total_bytes)
                                self.db.upsert_status(f"slot_{slot}_storage", storage_str)
                            last_ui = now
                    fout.flush()
                    os.fsync(fout.fileno())
                shutil.copystat(src, dest, follow_symlinks=True)
                digest = h.hexdigest() if h is not None else None
                size = dest.stat().st_size
                done_bytes += expected or size
                copied += 1
                self.db.add_media(dest.name, str(dest), str(src), device_id, officer_id, size, digest)
                self.logger.log(f"COPIED {src} -> {dest} ({size} bytes)")
                if self.config.import_delete_source:
                    try:
                        os.remove(src)
                    except Exception as exc:
                        self.logger.log(f"WARN delete source failed {src}: {exc}")
            self.set_slot_status(cand, "готово", f"файлов: {copied}")
            self.set_slot_progress(cand, total=100 if copied else 0, file=100 if copied else 0, current_file="", speed="", eta="")
            return ImportResult(disk.path, True, copied, total_bytes)
        except Exception as exc:
            self.set_slot_status(cand, "ошибка", str(exc))
            return ImportResult(disk.path, False, copied, total_bytes, str(exc))
        finally:
            if mountpoint:
                try:
                    unmount_path(mountpoint, logger=self.logger)
                except Exception as exc:
                    self.logger.log(f"WARN unmount failed {mountpoint}: {exc}")

    @staticmethod
    def _format_bytes(value: int) -> str:
        n = float(value or 0)
        for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
            if n < 1024 or unit == "ТБ":
                return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} {unit}"
            n /= 1024
        return str(value or 0)

    @staticmethod
    def _format_speed(bytes_per_sec: float) -> str:
        if bytes_per_sec <= 0:
            return ""
        mib = bytes_per_sec / (1024 * 1024)
        if mib >= 1:
            return f"{mib:.1f} МБ/с"
        kib = bytes_per_sec / 1024
        return f"{kib:.0f} КБ/с"

    @staticmethod
    def _format_eta(seconds: float) -> str:
        seconds = int(max(0, seconds))
        if seconds <= 0:
            return ""
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:d}:{m:02d}:{s:02d}"
        return f"{m:d}:{s:02d}"

    def _unique_dest(self, dest_dir: Path, file_name: str) -> Path:
        from .storage import unique_dest
        return unique_dest(dest_dir, file_name)

    def import_disks_parallel(self, pairs: List[tuple[amba.AmbarellaCandidate, UsbDisk]]) -> List[ImportResult]:
        """One-shot import with hub-aware scheduling.

        Used by run_once/test mode. Native continuous mode uses the same helper.
        """
        results: List[ImportResult] = []
        total = len(pairs)
        done = 0
        errors = 0
        waiting_import: list[tuple[amba.AmbarellaCandidate, UsbDisk]] = list(pairs)
        self.logger.log(
            f"--- Import from disks: {total}, total_parallel={self.config.max_parallel_imports}, "
            f"per_hub={self.config.max_parallel_imports_per_hub} ---"
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.config.max_parallel_imports)) as pool:
            fut_map: dict[concurrent.futures.Future, tuple[amba.AmbarellaCandidate, UsbDisk]] = {}
            self._start_waiting_imports(waiting_import, fut_map, pool)
            while fut_map or waiting_import:
                if not fut_map:
                    # All remaining entries are blocked by per-hub limits only if
                    # active imports exist. With no active imports, force one
                    # pass to avoid a dead loop in pathological cases.
                    self._start_waiting_imports(waiting_import, fut_map, pool)
                    if not fut_map:
                        break
                finished, _ = concurrent.futures.wait(fut_map, return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in finished:
                    cand, disk = fut_map.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as exc:
                        res = ImportResult(disk.path, False, error=str(exc))
                    results.append(res)
                    done += 1
                    if not res.ok:
                        errors += 1
                    self.logger.log(f"IMPORT RESULT {'OK' if res.ok else 'FAIL'} {res.disk}: files={res.files} bytes={res.bytes} error={res.error}")
                self._start_waiting_imports(waiting_import, fut_map, pool)
                importing = len(fut_map)
                self.set_header(total=total, queue=0, connected=max(0, total - done - importing), importing=importing, ready=done - errors, errors=errors)
        return results

    def _candidate_identity(self, cand: amba.AmbarellaCandidate) -> str:
        slot = self._slot_number_for_candidate(cand)
        if slot is not None:
            return f"slot:{slot}"
        return f"usb:{cand.bus}:{cand.port_path}"

    def _refresh_header_from_sets(self, queued: list[amba.AmbarellaCandidate], waiting_import: list[tuple[amba.AmbarellaCandidate, UsbDisk]], futures: dict, ready: int, errors: int) -> None:
        active_imports = len(futures)
        connected_waiting = len(waiting_import)
        total = len(queued) + connected_waiting + active_imports + ready + errors
        self.set_header(total=total, queue=len(queued), connected=connected_waiting, importing=active_imports, ready=ready, errors=errors)

    def _read_one_vendor_info(self, cand: amba.AmbarellaCandidate) -> bool:
        dev = amba.find_device_again(cand)
        if dev is None:
            self.set_slot_status(cand, "ошибка", "регистратор пропал")
            self.logger.log(f"INFO FAIL {format_candidate(cand, self.slot_map)}: device disappeared")
            return False
        try:
            self.set_slot_status(cand, "считывание")
            with amba.AmbarellaSession(dev) as s:
                cand.info = s.read_info(self.password, full=False)
            self.set_slot_status(cand, "ожидает скачивания")
            info = " ".join(f"{k}={v}" for k, v in cand.info.items())
            self.logger.log(f"INFO OK {format_candidate(cand, self.slot_map)} {info}")
            return True
        except Exception as exc:
            self.set_slot_status(cand, "ошибка", str(exc))
            self.logger.log(f"INFO FAIL {format_candidate(cand, self.slot_map)}: {exc}")
            return False

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while not self._stop.is_set() and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))

    def run_continuous(self, poll_interval_sec: float = 1.0) -> int:
        """Continuous dock-station mode used by the native operator app."""
        self.logger.log("=== DocStation Linux continuous import service start ===")
        self.logger.log(
            "Mode: keep recorders in Ambarella/vendor mode until an import slot is free; "
            f"total_parallel={self.config.max_parallel_imports}, per_hub={self.config.max_parallel_imports_per_hub}"
        )
        self.slot_map = SlotMap.load(self.config.dock_slots_path)

        # Гарантируем что папка архива существует
        self._ensure_storage_root()
        # Чистим мусор при старте
        self._cleanup_storage()
        _last_cleanup = time.monotonic()
        self.clear_slot_statuses()
        known: set[str] = set()
        queued: list[amba.AmbarellaCandidate] = []
        waiting_import: list[tuple[amba.AmbarellaCandidate, UsbDisk]] = []
        ready = 0
        errors = 0
        try:
            before = lsblk_usb_disks()
        except Exception:
            before = {}
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.config.max_parallel_imports))
        futures: dict[concurrent.futures.Future, tuple[amba.AmbarellaCandidate, UsbDisk]] = {}
        try:
            while not self._stop.is_set():
                self.slot_map = SlotMap.load(self.config.dock_slots_path)

                # 1. Detect new vendor-mode recorders and immediately read startup info.
                #    They remain in vendor mode after this step. This avoids a large
                #    pile of idle Mass Storage devices on the USB/storage stack.
                try:
                    cands = sorted(amba.list_ambarella(self.config.vendor_vid_int, self.config.vendor_pid_int), key=self.slot_map.sort_key_for_candidate)
                except Exception as exc:
                    self.logger.log(f"DETECT ERROR: {exc}")
                    cands = []
                for cand in cands:
                    ident = self._candidate_identity(cand)
                    if ident in known:
                        continue
                    known.add(ident)
                    self.set_slot_status(cand, "обнаружен")
                    self.logger.log(f"DETECTED {format_candidate(cand, self.slot_map)} hub={self._hub_group_label(self._hub_group_for_candidate(cand))}")
                    if self._read_one_vendor_info(cand):
                        queued.append(cand)
                    else:
                        errors += 1

                # 2. Start imports for disks that were just switched and settled.
                #    Hub-aware limits: default 6 total, 3 per USB branch. One hub
                #    never borrows another hub's limit.
                self._start_waiting_imports(waiting_import, futures, pool)

                # 3. Collect completed imports.
                done_futs = [f for f in list(futures) if f.done()]
                if done_futs:
                    # FIX: refresh 'before' after removals so recycled /dev/sdX
                    # paths (same name, different device) are not missed by
                    # wait_for_new_usb_disk on the next switch.
                    try:
                        before = lsblk_usb_disks()
                    except Exception:
                        pass
                for fut in done_futs:
                    cand, disk = futures.pop(fut)
                    try:
                        res = fut.result()
                    except Exception as exc:
                        res = ImportResult(disk.path, False, error=str(exc))
                    known.discard(self._candidate_identity(cand))
                    if res.ok:
                        ready += 1
                    else:
                        errors += 1
                    self.logger.log(f"IMPORT RESULT {'OK' if res.ok else 'FAIL'} {res.disk}: files={res.files} bytes={res.bytes} error={res.error}")

                self._refresh_header_from_sets(queued, waiting_import, futures, ready, errors)

                # 4. Convert exactly one Ambarella recorder to disk only if the
                #    global and per-hub import limits have a free reserved slot.
                cand, group = self._pop_next_switch_candidate(queued, waiting_import, futures)
                if cand is not None:
                    self._refresh_header_from_sets(queued, waiting_import, futures, ready, errors)
                    delay_ms = max(0, self.config.switch_to_disk_delay_ms)
                    self.set_slot_status(cand, "пауза перед скачиванием", "")
                    self.logger.log(
                        f"SWITCH DELAY {delay_ms} ms {format_candidate(cand, self.slot_map)} "
                        f"hub={self._hub_group_label(group)}"
                    )
                    self._sleep_interruptible(delay_ms / 1000.0)
                    if self._stop.is_set():
                        break
                    self.set_slot_status(cand, "подключение к архиву")
                    dev = amba.find_device_again(cand)
                    if dev is None:
                        self.set_slot_status(cand, "ошибка", "пропал перед скачиванием")
                        self.logger.log(f"SWITCH FAIL {format_candidate(cand, self.slot_map)}: device disappeared")
                        known.discard(self._candidate_identity(cand))
                        errors += 1
                        continue
                    try:
                        with amba.AmbarellaSession(dev) as s:
                            s.switch_to_disk(self.password)
                        self.logger.log(f"SWITCH COMMAND SENT {format_candidate(cand, self.slot_map)}")
                        # FIX: snapshot AFTER sending switch command, not from
                        # an old snapshot that may include removed disks whose
                        # /dev/sdX paths could be re-assigned to this device.
                        before = lsblk_usb_disks()
                        disk = wait_for_new_usb_disk(before, self.config.switch_timeout_sec, logger=self.logger)
                        if disk is None:
                            self.set_slot_status(cand, "ошибка", "диск не появился")
                            self.logger.log(f"SWITCH FAIL {format_candidate(cand, self.slot_map)}: no new USB disk")
                            known.discard(self._candidate_identity(cand))
                            errors += 1
                            continue
                        before = lsblk_usb_disks()
                        self.set_slot_status(cand, "подключён")
                        waiting_import.append((cand, disk))
                        self._refresh_header_from_sets(queued, waiting_import, futures, ready, errors)
                        if self.config.disk_settle_ms > 0:
                            self.logger.log(f"DISK SETTLE {self.config.disk_settle_ms} ms {disk.path}")
                            self._sleep_interruptible(self.config.disk_settle_ms / 1000.0)
                        self._start_waiting_imports(waiting_import, futures, pool)
                    except Exception as exc:
                        self.set_slot_status(cand, "ошибка", str(exc))
                        self.logger.log(f"SWITCH FAIL {format_candidate(cand, self.slot_map)}: {exc}")
                        known.discard(self._candidate_identity(cand))
                        errors += 1
                    continue

                self._sleep_interruptible(max(0.25, poll_interval_sec))

                # Чистка мусора раз в час
                if time.monotonic() - _last_cleanup > 3600:
                    self._cleanup_storage()
                    _last_cleanup = time.monotonic()

        finally:
            self.logger.log("=== DocStation Linux continuous import service stop ===")
            pool.shutdown(wait=False, cancel_futures=True)
        return 0

    # ── Storage helpers ────────────────────────────────────────────────────

    def _ensure_storage_root(self) -> None:
        """Создаёт папку архива если её нет. Падение — критично, логируем."""
        try:
            root = resolve_relative(self.config.storage_root)
            root.mkdir(parents=True, exist_ok=True)
            self.logger.log(f"Storage root: {root}")
        except Exception as exc:
            self.logger.log(f"ERROR: не удалось создать папку архива {self.config.storage_root}: {exc}")

    def _cleanup_storage(self) -> None:
        """Удаляет мусор в папке архива:
        - пустые директории
        - файлы *.tmp и *.part старше 2 часов (незавершённые закачки)
        - папки .import_tmp* старше 2 часов
        """
        try:
            root = resolve_relative(self.config.storage_root)
            if not root.exists():
                return
            cutoff = time.time() - 7200  # 2 часа
            removed_files = 0
            removed_dirs = 0

            # Временные файлы незавершённых импортов
            for pattern in ("**/*.tmp", "**/*.part", "**/.import_tmp*"):
                for p in root.glob(pattern):
                    try:
                        if p.stat().st_mtime < cutoff:
                            if p.is_file():
                                p.unlink()
                                removed_files += 1
                            elif p.is_dir():
                                import shutil as _shutil
                                _shutil.rmtree(p, ignore_errors=True)
                                removed_dirs += 1
                    except Exception:
                        pass

            # Пустые директории (обход снизу вверх)
            for p in sorted(root.rglob("*"), reverse=True):
                if p.is_dir() and p != root:
                    try:
                        if not any(p.iterdir()):
                            p.rmdir()
                            removed_dirs += 1
                    except Exception:
                        pass

            if removed_files or removed_dirs:
                self.logger.log(
                    f"Cleanup: удалено {removed_files} файлов, {removed_dirs} папок"
                )
        except Exception as exc:
            self.logger.log(f"Cleanup error: {exc}")

    def run_once(self, dry_run: bool = False) -> int:
        self.logger.log("=== DocStation Linux import cycle start ===")
        self.slot_map = SlotMap.load(self.config.dock_slots_path)
        self.clear_slot_statuses()
        cands = sorted(amba.list_ambarella(self.config.vendor_vid_int, self.config.vendor_pid_int), key=self.slot_map.sort_key_for_candidate)
        self.logger.log(f"Ambarella/vendor-mode recorders detected: {len(cands)}")
        self.set_header(total=len(cands), queue=len(cands), connected=0, importing=0, ready=0)
        for i, c in enumerate(cands, 1):
            self.set_slot_status(c, "обнаружен")
            self.logger.log(f"  [{i:02d}] {format_candidate(c, self.slot_map)}")
        if not cands:
            return 0
        info_ok = self.read_vendor_info(cands)
        if dry_run:
            self.logger.log("Dry run requested. Stop before switch-to-disk.")
            return 0
        pairs = self.switch_sequential(info_ok)
        self.logger.log(f"Switched/new disks: {len(pairs)}")
        self.set_header(total=len(cands), queue=0, connected=len(pairs), importing=0, ready=0)
        results = self.import_disks_parallel(pairs)
        ok = sum(1 for r in results if r.ok)
        fail = sum(1 for r in results if not r.ok)
        self.set_header(total=len(cands), queue=0, connected=0, importing=0, ready=ok, errors=fail)
        self.logger.log(f"=== Final result: Ambarella={len(cands)} InfoOK={len(info_ok)} NewDisks={len(pairs)} ImportOK={ok} ImportFAIL={fail} ===")
        return 0 if fail == 0 else 1
