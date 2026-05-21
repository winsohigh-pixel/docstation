#!/usr/bin/env python3
"""
test_amba_back.py — полный цикл:
  1. Найти регистратор в Ambarella/vendor mode (VID:4255)
  2. Переключить его в Mass Storage (как делает runner)
  3. Дождаться появления /dev/sdX
  4. Попробовать вернуть обратно в Ambarella разными методами
  5. Отчёт

Запуск:
    sudo python3 scripts/test_amba_back.py [--password 000000]
    sudo python3 scripts/test_amba_back.py --scan          # только показать устройства
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Ambarella constants ────────────────────────────────────────────────────────
AMBA_VID = 0x4255
AMBA_PID = 0x0001

# ── SG_IO ─────────────────────────────────────────────────────────────────────
SG_IO         = 0x2285
SG_DXFER_NONE = -1

# ── USB ioctl ─────────────────────────────────────────────────────────────────
USBDEVFS_RESET = 0x5514


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: обнаружение и переключение Ambarella → Mass Storage
# ══════════════════════════════════════════════════════════════════════════════

def find_and_switch(password: str) -> Optional[str]:
    """
    Найти регистратор в Ambarella mode, переключить в Mass Storage,
    вернуть /dev/sdX или None.
    """
    from docstation.amba import list_ambarella, find_device_again, AmbarellaSession
    from docstation.storage import lsblk_usb_disks, wait_for_new_usb_disk
    from docstation.loggingx import Logger

    logger = Logger("/dev/null")

    print("\n── Шаг 1: поиск регистратора в Ambarella/vendor mode ───────────")
    cands = list_ambarella(AMBA_VID, AMBA_PID)
    if not cands:
        print("  Регистраторов с VID:4255 не найдено.")
        print("  Вставьте регистратор и убедитесь что он в обычном режиме (не Mass Storage).")
        return None

    cand = cands[0]
    print(f"  Найден: bus={cand.bus} addr={cand.address} ports={cand.port_path}")
    if cand.serial:
        print(f"  Serial: {cand.serial}")

    print("\n── Шаг 2: считывание информации в Ambarella mode ───────────────")
    dev = find_device_again(cand)
    if dev is None:
        print("  Устройство исчезло.")
        return None

    try:
        with AmbarellaSession(dev) as s:
            info = s.read_info(password, full=False)
        print(f"  Device ID : {info.get('device_id', '—')}")
        print(f"  Officer   : {info.get('officer_id', '—')}")
        print(f"  Firmware  : {info.get('fw', '—')}")
        print(f"  Storage   : {info.get('storage', '—')}")
    except Exception as e:
        print(f"  Считывание не удалось: {e}")
        print("  Продолжаем с переключением без информации.")

    print("\n── Шаг 3: переключение в Mass Storage ──────────────────────────")
    before = lsblk_usb_disks()
    print(f"  Дисков до переключения: {len(before)}")

    dev = find_device_again(cand)
    if dev is None:
        print("  Устройство исчезло перед переключением.")
        return None

    try:
        with AmbarellaSession(dev) as s:
            s.switch_to_disk(password)
        print("  Команда switch_to_disk отправлена.")
    except Exception as e:
        print(f"  Ошибка отправки команды: {e}")
        return None

    print("  Ожидание Mass Storage диска (до 75 сек)…")
    disk = wait_for_new_usb_disk(before, timeout_sec=75, logger=logger)
    if disk is None:
        print("  Mass Storage диск не появился за 75 сек.")
        return None

    print(f"  ✓ Диск появился: {disk.path}")
    print(f"    Модель  : {disk.model or '—'}")
    print(f"    Серийный: {disk.serial or '—'}")
    print(f"    Размер  : {disk.size / (1024**3):.1f} ГБ" if disk.size else "")

    # дать системе время зарегистрировать диск
    time.sleep(2)
    return disk.path


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: попытки вернуть обратно в Ambarella
# ══════════════════════════════════════════════════════════════════════════════

class SgIoHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id",    ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len",         ctypes.c_ubyte),
        ("mx_sb_len",       ctypes.c_ubyte),
        ("iovec_cnt",       ctypes.c_ushort),
        ("dxfer_len",       ctypes.c_uint),
        ("dxferp",          ctypes.c_void_p),
        ("cmdp",            ctypes.c_void_p),
        ("sbp",             ctypes.c_void_p),
        ("timeout",         ctypes.c_uint),
        ("flags",           ctypes.c_uint),
        ("pack_id",         ctypes.c_int),
        ("usr_ptr",         ctypes.c_void_p),
        ("status",          ctypes.c_ubyte),
        ("masked_status",   ctypes.c_ubyte),
        ("msg_status",      ctypes.c_ubyte),
        ("sb_len_wr",       ctypes.c_ubyte),
        ("host_status",     ctypes.c_ushort),
        ("driver_status",   ctypes.c_ushort),
        ("resid",           ctypes.c_int),
        ("duration",        ctypes.c_uint),
        ("info",            ctypes.c_uint),
    ]


def sg_io(fd: int, cdb: bytes, timeout_ms: int = 8000) -> tuple[int, bytes]:
    cdb_arr   = (ctypes.c_uint8 * len(cdb))(*cdb)
    sense_arr = (ctypes.c_uint8 * 32)()
    hdr = SgIoHdr()
    hdr.interface_id    = ord('S')
    hdr.dxfer_direction = SG_DXFER_NONE
    hdr.cmd_len         = len(cdb)
    hdr.mx_sb_len       = 32
    hdr.cmdp            = ctypes.cast(cdb_arr,   ctypes.c_void_p)
    hdr.sbp             = ctypes.cast(sense_arr, ctypes.c_void_p)
    hdr.timeout         = timeout_ms
    fcntl.ioctl(fd, SG_IO, hdr)
    sense = bytes(sense_arr[:hdr.sb_len_wr]) if hdr.sb_len_wr else b""
    return hdr.status, sense


def unmount_all(dev: str) -> None:
    r = subprocess.run(["lsblk", "-no", "MOUNTPOINTS", dev],
                       capture_output=True, text=True)
    for mp in r.stdout.strip().splitlines():
        mp = mp.strip()
        if mp:
            print(f"  Размонтируем {mp}…")
            subprocess.run(["umount", "-l", mp], capture_output=True)


def wait_for_amba(timeout_sec: float = 25.0) -> bool:
    """Ждём появления VID:4255."""
    try:
        import usb.core
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if list(usb.core.find(find_all=True, idVendor=AMBA_VID, idProduct=AMBA_PID)):
                return True
            time.sleep(0.4)
        return False
    except ImportError:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            r = subprocess.run(["lsusb", "-d", f"{AMBA_VID:04x}:{AMBA_PID:04x}"],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return True
            time.sleep(0.4)
        return False


def find_usb_devpath(block_dev: str) -> Optional[str]:
    name = Path(block_dev).name
    import re
    sysfs = Path(f"/sys/block/{name}")
    if not sysfs.exists():
        m = re.match(r"(sd[a-z]+)", name)
        if m:
            sysfs = Path(f"/sys/block/{m.group(1)}")
    if not sysfs.exists():
        return None
    try:
        real = str(sysfs.resolve())
        m = re.search(r"/usb\d+/([\d]+-[\d.]+)/", real)
        if m:
            usb_id = m.group(1)
            bus_f  = Path(f"/sys/bus/usb/devices/{usb_id}/busnum")
            dev_f  = Path(f"/sys/bus/usb/devices/{usb_id}/devnum")
            if bus_f.exists() and dev_f.exists():
                return f"/dev/bus/usb/{int(bus_f.read_text()):03d}/{int(dev_f.read_text()):03d}"
    except Exception as e:
        print(f"    sysfs: {e}")
    return None


# ── Метод 1: SCSI START STOP UNIT (eject) ─────────────────────────────────────
def method_scsi_eject(dev: str) -> bool:
    print("\n── Метод 1: SCSI START STOP UNIT (eject) ───────────────────────")
    try:
        unmount_all(dev)
        with open(dev, "rb") as f:
            # SYNC CACHE
            st, _ = sg_io(f.fileno(), bytes([0x35,0,0,0,0,0,0,0,0,0]))
            print(f"  SYNC CACHE: status={st}")
            # START STOP UNIT: LOEJ=1 START=0
            st, sense = sg_io(f.fileno(), bytes([0x1B,0,0,0,0x02,0]))
            print(f"  START STOP (eject): status={st}  sense={sense.hex(' ') if sense else '—'}")
    except Exception as e:
        print(f"  Ошибка: {e}")
    print("  Ждём Ambarella 20 сек…")
    found = wait_for_amba(20)
    print(f"  Результат: {'✓ ПОЯВИЛСЯ' if found else '✗ не появился'}")
    return found


# ── Метод 2: USB device reset ─────────────────────────────────────────────────
def method_usb_reset(dev: str) -> bool:
    print("\n── Метод 2: USB device reset (USBDEVFS_RESET) ──────────────────")
    usb_path = find_usb_devpath(dev)
    print(f"  USB devpath: {usb_path or 'не найден'}")
    ok = False
    if usb_path and Path(usb_path).exists():
        try:
            with open(usb_path, "wb") as f:
                fcntl.ioctl(f, USBDEVFS_RESET, 0)
            print("  ioctl USBDEVFS_RESET: OK")
            ok = True
        except Exception as e:
            print(f"  ioctl: {e}")
    else:
        # fallback: sg reset
        try:
            with open(dev, "rb") as f:
                fcntl.ioctl(f, 0x2284, 0)   # SG_SCSI_RESET_DEVICE
            print("  SG_SCSI_RESET_DEVICE: OK")
            ok = True
        except Exception as e:
            print(f"  SG_SCSI_RESET: {e}")
    print("  Ждём Ambarella 20 сек…")
    found = wait_for_amba(20)
    print(f"  Результат: {'✓ ПОЯВИЛСЯ' if found else '✗ не появился'}")
    return found


# ── Метод 3: udisksctl power-off ──────────────────────────────────────────────
def method_udisks_poweroff(dev: str) -> bool:
    print("\n── Метод 3: udisksctl power-off ────────────────────────────────")
    r = subprocess.run(["udisksctl", "power-off", "-b", dev, "--no-user-interaction"],
                       capture_output=True, text=True, timeout=15)
    print(f"  udisksctl: {(r.stdout + r.stderr).strip() or 'OK'}")
    print("  Ждём Ambarella 20 сек…")
    found = wait_for_amba(20)
    print(f"  Результат: {'✓ ПОЯВИЛСЯ' if found else '✗ не появился'}")
    return found


# ── Метод 4: uhubctl port power cycle ────────────────────────────────────────
def method_uhubctl(dev: str) -> bool:
    print("\n── Метод 4: uhubctl (hub port power cycle) ─────────────────────")
    if subprocess.run(["which", "uhubctl"], capture_output=True).returncode != 0:
        print("  uhubctl не установлен: sudo apt install uhubctl")
        return False

    # find port info from sysfs
    import re
    name = Path(dev).name
    port_num = None
    hub_bus  = None
    try:
        sysfs = str(Path(f"/sys/block/{name}").resolve())
        m = re.search(r"/usb(\d+)/([\d]+-[\d.]+)/", sysfs)
        if m:
            hub_bus  = int(m.group(1))
            devpath  = m.group(2)        # e.g. "1-2.3"
            port_num = int(devpath.split(".")[-1])
            print(f"  Hub bus={hub_bus} port={port_num}")
    except Exception as e:
        print(f"  sysfs parse: {e}")

    if port_num is None or hub_bus is None:
        print("  Не удалось определить hub/port")
        return False

    r1 = subprocess.run(f"uhubctl -b {hub_bus} -p {port_num} -a off",
                        shell=True, capture_output=True, text=True)
    print(f"  power off: {(r1.stdout + r1.stderr).strip()}")
    time.sleep(3)
    r2 = subprocess.run(f"uhubctl -b {hub_bus} -p {port_num} -a on",
                        shell=True, capture_output=True, text=True)
    print(f"  power on:  {(r2.stdout + r2.stderr).strip()}")
    print("  Ждём Ambarella 25 сек…")
    found = wait_for_amba(25)
    print(f"  Результат: {'✓ ПОЯВИЛСЯ' if found else '✗ не появился'}")
    return found


# ── Метод 5: SCSI WRITE BUFFER (vendor-specific reboot) ──────────────────────
def method_scsi_vendor(dev: str) -> bool:
    """Некоторые устройства реагируют на vendor-specific команды перезагрузки."""
    print("\n── Метод 5: SCSI vendor commands (reboot/mode switch) ──────────")
    results = []
    try:
        with open(dev, "rb") as f:
            # Попытка 5a: WRITE BUFFER mode=0x1c (firmware download trigger)
            try:
                st, sense = sg_io(f.fileno(),
                    bytes([0x3B, 0x1C, 0, 0, 0, 0, 0, 0, 0, 0]), timeout_ms=3000)
                print(f"  WRITE BUFFER 0x1C: status={st} sense={sense.hex(' ') if sense else '—'}")
                results.append(st)
            except Exception as e:
                print(f"  WRITE BUFFER 0x1C: {e}")

            # Попытка 5b: MODE SELECT с нестандартным параметром
            try:
                st, sense = sg_io(f.fileno(),
                    bytes([0x15, 0x10, 0, 0, 0, 0]), timeout_ms=3000)
                print(f"  MODE SELECT: status={st} sense={sense.hex(' ') if sense else '—'}")
                results.append(st)
            except Exception as e:
                print(f"  MODE SELECT: {e}")
    except Exception as e:
        print(f"  Открытие устройства: {e}")

    print("  Ждём Ambarella 15 сек…")
    found = wait_for_amba(15)
    print(f"  Результат: {'✓ ПОЯВИЛСЯ' if found else '✗ не появился'}")
    return found


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(results: list[tuple[str, bool]]) -> None:
    print(f"\n{'='*60}")
    print("  ИТОГ")
    print(f"{'='*60}")
    working = [name for name, ok in results if ok]
    for name, ok in results:
        print(f"  {'✓' if ok else '✗'}  {name}")
    print()
    if working:
        print(f"  ВЫВОД: обратный перевод в Ambarella ВОЗМОЖЕН.")
        print(f"  Рабочий метод: {working[0]}")
        print()
        print("  Можно добавить в runner.py после завершения импорта,")
        print("  чтобы не требовалось физически переставлять регистратор.")
    else:
        print("  ВЫВОД: программный возврат в Ambarella НЕ ПОДДЕРЖИВАЕТСЯ")
        print("  данной прошивкой / конфигурацией оборудования.")
        print()
        print("  Это нормально — регистратор вернётся в Ambarella mode")
        print("  автоматически при следующем физическом переподключении.")
        print()
        print("  Если нужен автоматический возврат без перетыкания — варианты:")
        print("    1. Управляемый USB-хаб с питанием портов (uhubctl)")
        print("    2. GPIO relay на USB VBUS (аппаратный сброс питания)")
    print(f"{'='*60}\n")


def scan() -> None:
    print("\n── Ambarella/vendor mode (VID:4255) ────────────────────────────")
    try:
        from docstation.amba import list_ambarella
        cands = list_ambarella(AMBA_VID, AMBA_PID)
        if cands:
            for c in cands:
                print(f"  bus={c.bus:03d} addr={c.address:03d} ports={c.port_path}"
                      + (f"  serial={c.serial}" if c.serial else ""))
        else:
            print("  Не найдено")
    except Exception as e:
        r = subprocess.run(["lsusb", "-d", f"{AMBA_VID:04x}:{AMBA_PID:04x}"],
                           capture_output=True, text=True)
        print(r.stdout.strip() or f"  Не найдено ({e})")

    print("\n── Mass Storage (USB блочные устройства) ───────────────────────")
    r = subprocess.run(
        ["lsblk", "-o", "NAME,PATH,SIZE,MODEL,TRAN,MOUNTPOINTS", "-d"],
        capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "NAME" in line or "usb" in line.lower():
            print(f"  {line}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Тест переключения Ambarella ↔ Mass Storage")
    ap.add_argument("--password", default="000000",
                    help="Пароль регистратора (default: 000000)")
    ap.add_argument("--scan", action="store_true",
                    help="Только показать устройства, не переключать")
    args = ap.parse_args()

    print(f"\n{'='*60}")
    print("  test_amba_back.py")
    print("  Ambarella → Mass Storage → попытка вернуть обратно")
    print(f"{'='*60}")

    if args.scan:
        scan()
        return

    if os.geteuid() != 0:
        print("\nВНИМАНИЕ: нужен sudo для USB ioctl и SG_IO.\n")

    # Шаг 1-3: найти Ambarella, переключить в Mass Storage
    block_dev = find_and_switch(args.password)
    if block_dev is None:
        print("\nНе удалось получить Mass Storage устройство. Тест прерван.")
        return

    print(f"\n{'='*60}")
    print(f"  Устройство в Mass Storage: {block_dev}")
    print(f"  Начинаем попытки вернуть в Ambarella mode...")
    print(f"{'='*60}")

    results: list[tuple[str, bool]] = []

    found = method_scsi_eject(block_dev)
    results.append(("SCSI START STOP UNIT (eject)", found))
    if found:
        print_summary(results)
        return

    # проверяем что устройство ещё существует
    if not Path(block_dev).exists():
        print(f"\n  {block_dev} исчез после метода 1 — возможно устройство само переключилось.")
        found = wait_for_amba(5)
        results.append(("Авто-переключение", found))
        print_summary(results)
        return

    found = method_usb_reset(block_dev)
    results.append(("USB device reset", found))
    if found:
        print_summary(results)
        return

    if Path(block_dev).exists():
        found = method_udisks_poweroff(block_dev)
        results.append(("udisksctl power-off", found))
        if found:
            print_summary(results)
            return

    found = method_uhubctl(block_dev)
    results.append(("uhubctl port power cycle", found))
    if found:
        print_summary(results)
        return

    if Path(block_dev).exists():
        found = method_scsi_vendor(block_dev)
        results.append(("SCSI vendor commands", found))

    print_summary(results)


if __name__ == "__main__":
    main()
