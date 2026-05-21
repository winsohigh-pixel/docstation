from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import amba
from .config import StationConfig


def normalize_usb_path(value: str | None) -> str:
    value = str(value or "").strip()
    if value.startswith("ports="):
        value = value.split("=", 1)[1]
    return value.strip()


@dataclass
class SlotMapping:
    slot: int
    label: str
    usb_path: str

    @staticmethod
    def from_dict(data: dict) -> "SlotMapping":
        slot = int(data.get("slot") or data.get("number") or 0)
        label = str(data.get("label") or f"Ячейка {slot:02d}")
        usb_path = normalize_usb_path(data.get("usb_path") or data.get("port_path") or data.get("ports"))
        return SlotMapping(slot=slot, label=label, usb_path=usb_path)


class SlotMap:
    def __init__(self, slots: Optional[List[SlotMapping]] = None):
        self.slots: List[SlotMapping] = sorted(slots or [], key=lambda x: x.slot)
        self._by_path: Dict[str, SlotMapping] = {normalize_usb_path(s.usb_path): s for s in self.slots if s.usb_path}

    @staticmethod
    def load(path: str | Path) -> "SlotMap":
        p = Path(path)
        if not p.exists():
            return SlotMap([])
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return SlotMap([])
        raw = data.get("slots") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            raw = []
        slots: List[SlotMapping] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                sm = SlotMapping.from_dict(item)
                if sm.slot > 0 and sm.usb_path:
                    slots.append(sm)
            except Exception:
                continue
        return SlotMap(slots)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "slots": [asdict(s) for s in sorted(self.slots, key=lambda x: x.slot)]
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def label_for_path(self, usb_path: str | None) -> str:
        sm = self._by_path.get(normalize_usb_path(usb_path))
        return sm.label if sm else ""

    def slot_for_path(self, usb_path: str | None) -> Optional[int]:
        sm = self._by_path.get(normalize_usb_path(usb_path))
        return sm.slot if sm else None

    def label_for_candidate(self, cand: amba.AmbarellaCandidate) -> str:
        return self.label_for_path(cand.port_path)

    def slot_for_candidate(self, cand: amba.AmbarellaCandidate) -> Optional[int]:
        return self.slot_for_path(cand.port_path)

    def sort_key_for_candidate(self, cand: amba.AmbarellaCandidate):
        slot = self.slot_for_candidate(cand)
        if slot is not None:
            return (0, slot)
        return (1, cand.bus, cand.ports, cand.address)

    def rows(self) -> List[SlotMapping]:
        return list(sorted(self.slots, key=lambda x: x.slot))

    def count(self) -> int:
        return len(self.slots)


def format_candidate(c: amba.AmbarellaCandidate, slot_map: Optional[SlotMap] = None) -> str:
    prefix = ""
    if slot_map:
        label = slot_map.label_for_candidate(c)
        if label:
            prefix = f"{label}: "
    return prefix + c.human


def print_slot_map(path: str | Path) -> int:
    sm = SlotMap.load(path)
    print(f"Файл карты стаканов: {path}")
    if not sm.slots:
        print("Карта стаканов пустая. Запусти: ./scripts/calibrate_slots.sh")
        return 0
    for row in sm.rows():
        print(f"{row.slot:02d}: {row.label} -> usb_path={row.usb_path}")
    return 0


def _read_int_choice(prompt: str, min_value: int, max_value: int) -> Optional[int]:
    raw = input(prompt).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except Exception:
        return None
    if min_value <= value <= max_value:
        return value
    return None


def _choose_candidate(cands: List[amba.AmbarellaCandidate]) -> Optional[amba.AmbarellaCandidate]:
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    print("Видно больше одного регистратора. Для точной калибровки лучше оставить один.")
    for i, c in enumerate(cands, 1):
        print(f"  [{i:02d}] ports={c.port_path} bus={c.bus:03d} addr={c.address:03d} product={c.product or '-'}")
    idx = _read_int_choice("Номер нужного устройства или Enter для повтора: ", 1, len(cands))
    if idx is None:
        return None
    return cands[idx - 1]


def _candidate_signature(c: amba.AmbarellaCandidate) -> tuple[int, tuple[int, ...]]:
    return (c.bus, tuple(c.ports))


def _list_candidates_safe(config: StationConfig) -> List[amba.AmbarellaCandidate]:
    return amba.list_ambarella(config.vendor_vid_int, config.vendor_pid_int)


def _wait_for_empty(config: StationConfig, stable_sec: float = 1.2, poll_sec: float = 0.35) -> None:
    """Wait until no Ambarella devices are visible for a short stable period."""
    empty_since: Optional[float] = None
    last_count = None
    while True:
        try:
            cands = _list_candidates_safe(config)
        except Exception as exc:
            print(f"Ошибка USB при ожидании пустой док-станции: {exc}")
            time.sleep(1.0)
            continue
        count = len(cands)
        if count != last_count:
            if count == 0:
                print("Регистраторов не видно. Ждём стабильного пустого состояния...")
            else:
                print(f"Видно регистраторов: {count}. Вынь все регистраторы для продолжения.")
            last_count = count
        if count == 0:
            if empty_since is None:
                empty_since = time.monotonic()
            if time.monotonic() - empty_since >= stable_sec:
                return
        else:
            empty_since = None
        time.sleep(poll_sec)


def _wait_for_single_candidate(config: StationConfig, label: str, stable_sec: float = 1.2,
                               poll_sec: float = 0.35) -> Optional[amba.AmbarellaCandidate]:
    """Wait until exactly one Ambarella device is inserted and remains stable."""
    stable_sig = None
    stable_since: Optional[float] = None
    last_message = ""
    while True:
        try:
            cands = _list_candidates_safe(config)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            msg = f"Ошибка USB: {exc}"
            if msg != last_message:
                print(msg)
                last_message = msg
            time.sleep(1.0)
            continue

        if not cands:
            msg = f"{label}: жду вставки регистратора..."
            if msg != last_message:
                print(msg)
                last_message = msg
            stable_sig = None
            stable_since = None
            time.sleep(poll_sec)
            continue

        if len(cands) > 1:
            msg = f"{label}: видно {len(cands)} регистраторов. Оставь только один в текущей ячейке."
            if msg != last_message:
                print(msg)
                for i, c in enumerate(cands, 1):
                    print(f"  [{i:02d}] ports={c.port_path} bus={c.bus:03d} addr={c.address:03d} product={c.product or '-'}")
                last_message = msg
            stable_sig = None
            stable_since = None
            time.sleep(poll_sec)
            continue

        cand = cands[0]
        sig = _candidate_signature(cand)
        if stable_sig != sig:
            stable_sig = sig
            stable_since = time.monotonic()
            msg = f"{label}: найден ports={cand.port_path}. Проверяю стабильность..."
            if msg != last_message:
                print(msg)
                last_message = msg
            time.sleep(poll_sec)
            continue

        if stable_since is not None and time.monotonic() - stable_since >= stable_sec:
            return cand
        time.sleep(poll_sec)


def calibrate_slots(config: StationConfig, count: int = 20, output_path: str | Path | None = None,
                    start_slot: int = 1, keep_existing: bool = False, manual: bool = False) -> int:
    output = Path(output_path or config.dock_slots_path)
    existing = SlotMap.load(output) if keep_existing else SlotMap([])
    slots: Dict[int, SlotMapping] = {s.slot: s for s in existing.rows()}

    print("=== Калибровка стаканов DocStation ===")
    print("Привязка выполняется по физическому USB topology path, не по serial и не по букве диска.")
    print("Автоматический режим: вставил один регистратор -> путь зафиксирован -> вынул -> вставил следующий.")
    print("Во время калибровки должен быть вставлен только один регистратор — в текущую ячейку.")
    print(f"Карта будет сохранена в: {output}")
    print("Для отмены нажми Ctrl+C.")

    if manual:
        input("Вынь все регистраторы из док-станции и нажми Enter...")
    else:
        print("Вынь все регистраторы из док-станции. Калибровка продолжится автоматически.")
        _wait_for_empty(config)

    try:
        for slot_num in range(start_slot, count + 1):
            label = f"Ячейка {slot_num:02d}"
            while True:
                print("")
                print(f"--- {label} ---")
                if manual:
                    input(f"Вставь ОДИН регистратор в {label} и нажми Enter...")
                    try:
                        cands = amba.list_ambarella(config.vendor_vid_int, config.vendor_pid_int)
                    except Exception as exc:
                        print(f"Ошибка USB: {exc}")
                        return 1
                    if not cands:
                        print("Регистратор не найден. Проверь, что он в Ambarella-режиме, и повтори.")
                        continue
                    cand = _choose_candidate(cands)
                    if cand is None:
                        continue
                else:
                    print(f"Вставь один регистратор в {label}. Подтверждение не требуется.")
                    cand = _wait_for_single_candidate(config, label)
                    if cand is None:
                        continue

                usb_path = cand.port_path
                if not usb_path or usb_path == "?":
                    print("Не удалось получить USB topology path. Вынь/вставь регистратор и повтори.")
                    if not manual:
                        _wait_for_empty(config)
                    continue

                used_by = next((s for s in slots.values() if normalize_usb_path(s.usb_path) == usb_path and s.slot != slot_num), None)
                if used_by:
                    if manual:
                        print(f"ВНИМАНИЕ: usb_path={usb_path} уже записан как {used_by.label}.")
                        raw = input("Перезаписать на текущую ячейку? [y/N]: ").strip().lower()
                        if raw not in ("y", "yes", "д", "да"):
                            continue
                    else:
                        print(f"usb_path={usb_path} уже был записан как {used_by.label}; переносим на {label}.")
                    slots.pop(used_by.slot, None)

                info_text = ""
                try:
                    dev = amba.find_device_again(cand)
                    if dev is not None:
                        password = amba.load_default_password(config.device_ini_path)
                        with amba.AmbarellaSession(dev) as session:
                            cand.info = session.read_info(password, full=False)
                        device_id = cand.info.get("device_id", "")
                        officer_id = cand.info.get("officer_id", "")
                        info_text = f" device_id={device_id} officer_id={officer_id}".strip()
                except Exception as exc:
                    info_text = f" info-read-fail={exc}"

                slots[slot_num] = SlotMapping(slot=slot_num, label=label, usb_path=usb_path)
                SlotMap(list(slots.values())).save(output)
                print(f"Зафиксировано: {label} -> usb_path={usb_path}" + (f" ({info_text})" if info_text else ""))
                print(f"Карта сохранена: {output}")

                if manual:
                    input(f"Вынь регистратор из {label} и нажми Enter для продолжения...")
                else:
                    print(f"Вынь регистратор из {label}. После вынимания программа сама перейдёт дальше.")
                    _wait_for_empty(config)
                break
    except KeyboardInterrupt:
        print("")
        print("Калибровка остановлена пользователем. Уже записанные ячейки сохранены.")
        print_slot_map(output)
        return 130

    print("")
    print("Калибровка завершена.")
    print_slot_map(output)
    return 0
