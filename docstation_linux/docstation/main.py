from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import amba
from .config import StationConfig
from .db import StationDatabase
from .loggingx import Logger
from .runner import DocStationRunner
from .slots import SlotMap, calibrate_slots, print_slot_map, format_candidate
from .storage_slots import StorageSlotMap, list_storage_disks, calibrate_storage_slots_cli
from .webapp import serve


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="DocStation Linux")
    p.add_argument("--config", default="StationConfig.linux.json")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="Create default config/db")
    sub.add_parser("web", help="Run optional touch web UI")
    sub.add_parser("app", help="Run native GTK touch application")
    sub.add_parser("once", help="One import cycle")
    sub.add_parser("dry-run", help="Detect/read Ambarella info only")
    sub.add_parser("list-amba", help="List Ambarella devices")
    sub.add_parser("show-slots", help="Show calibrated cup/slot map")
    cal = sub.add_parser("calibrate-slots", help="Interactive cup/slot calibration")
    cal.add_argument("--count", type=int, default=20, help="Number of cups/slots to calibrate")
    cal.add_argument("--start", type=int, default=1, help="Start from this slot number")
    cal.add_argument("--keep-existing", action="store_true", help="Keep existing mappings for slots outside the calibrated range")
    cal.add_argument("--manual", action="store_true", help="Use old Enter-confirmed calibration mode")
    cs = sub.add_parser("calibrate-storage", help="Calibrate physical SATA storage disk slots")
    cs.add_argument("--count", type=int, default=4, help="Number of SATA slots to calibrate")
    sub.add_parser("show-storage", help="Show calibrated SATA storage slots and disk state")
    rp = sub.add_parser("reset-passwords", help="Reset archive passwords")
    rp.add_argument("--admin",    default="888")
    rp.add_argument("--operator", default="111")
    rp.add_argument("--service",  default="7777")
    loop = sub.add_parser("loop", help="Continuous import loop")
    loop.add_argument("--interval-sec", type=int, default=30)
    return p


def get_objects(cfg_path: str):
    cfg = StationConfig.load(cfg_path)
    if not Path(cfg_path).exists():
        cfg.save(cfg_path)
    db = StationDatabase(cfg.database_path)
    logger = Logger("logs/docstation_linux.log")
    return cfg, db, logger


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg, db, logger = get_objects(args.config)
    try:
        if args.cmd == "init":
            cfg.save(args.config)
            db.ensure_default_passwords()
            for p in [cfg.storage_root, Path(cfg.database_path).parent, "logs", cfg.mount_root]:
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                except PermissionError:
                    pass
            print("DocStation Linux initialized")
            print(f"Web UI: http://{cfg.web_host}:{cfg.web_port}")
            print("Default passwords: operator=111 admin=888")
            return 0
        if args.cmd == "list-amba":
            cands = amba.list_ambarella(cfg.vendor_vid_int, cfg.vendor_pid_int)
            slot_map = SlotMap.load(cfg.dock_slots_path)
            cands = sorted(cands, key=slot_map.sort_key_for_candidate)
            print(f"Ambarella/vendor-mode recorders detected: {len(cands)}")
            for i, c in enumerate(cands, 1):
                print(f"[{i:02d}] {format_candidate(c, slot_map)}")
            return 0
        if args.cmd == "show-slots":
            return print_slot_map(cfg.dock_slots_path)
        if args.cmd == "calibrate-slots":
            return calibrate_slots(cfg, count=max(1, args.count), start_slot=max(1, args.start), keep_existing=args.keep_existing, manual=args.manual)
        if args.cmd == "calibrate-storage":
            calibrate_storage_slots_cli(cfg.storage_slots_path, count=max(1, args.count))
            return 0
        if args.cmd == "show-storage":
            sm = StorageSlotMap.load(cfg.storage_slots_path)
            disks = list_storage_disks()
            by_path_index = {d.by_path: d for d in disks}
            if not sm.storage_slots:
                print("Дисковые слоты не откалиброваны. Запустите: calibrate-storage")
                print("\nТекущие диски в системе:")
                for d in disks:
                    print(f"  {d.dev:12s}  {d.by_path:50s}  {d.model:30s}  {d.size_human:8s}"
                          f"{'  смонтирован: ' + d.mountpoint if d.mountpoint else ''}")
            else:
                print(f"{'Слот':6s}  {'by-path':45s}  {'Диск':30s}  {'Размер':8s}  {'Свободно':8s}  {'Статус':20s}  Использовать")
                print("-" * 150)
                for slot in sorted(sm.storage_slots, key=lambda x: x.slot):
                    disk = next((d for bp, d in by_path_index.items() if slot.path_match and slot.path_match in bp), None)
                    status = "смонтирован" if (disk and disk.mountpoint) else ("подключён" if disk else "не подключён")
                    print(f"{slot.slot:6d}  {slot.path_match:45s}  "
                          f"{(disk.model if disk else slot.notes or '—'):30s}  "
                          f"{(disk.size_human if disk else '—'):8s}  "
                          f"{(disk.free_human if (disk and disk.mountpoint) else '—'):8s}  "
                          f"{status:20s}  {'✓' if slot.enabled else '—'}")
            return 0
        if args.cmd == "dry-run":
            return DocStationRunner(cfg, db, logger).run_once(dry_run=True)
        if args.cmd == "once":
            return DocStationRunner(cfg, db, logger).run_once(dry_run=False)
        if args.cmd == "web":
            serve(cfg, db, logger)
            return 0
        if args.cmd == "app":
            from .native_app import run_app
            logger.close()
            db.close()
            return run_app(args.config)
        if args.cmd == "reset-passwords":
            db.set_password("admin",    args.admin)
            db.set_password("operator", args.operator)
            db.set_password("service",  args.service)
            print(f"Passwords reset: admin={args.admin} operator={args.operator} service={args.service}")
            return 0
        if args.cmd == "loop":
            import time
            while True:
                try:
                    DocStationRunner(cfg, db, logger).run_once(dry_run=False)
                except KeyboardInterrupt:
                    return 0
                except Exception as exc:
                    logger.log(f"LOOP ERROR: {exc}")
                time.sleep(max(1, args.interval_sec))
    finally:
        logger.close()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
