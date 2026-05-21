from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import usb.core
    import usb.util
except Exception:  # pragma: no cover
    usb = None

REQ_OUT = 0x40
REQ_IN = 0xC0
B_REQUEST = 0x55
W_VALUE = 0x0080
W_INDEX = 0x0000
FRAME_SIZE = 128
INFO_SEQUENCE_MIN = [0x4F, 0x17, 0x21, 0x1F, 0x1D]
INFO_SEQUENCE_FULL = [0x4F, 0x17, 0x21, 0x24, 0x1F, 0x26, 0x1D, 0x2A, 0x2E, 0x2C]


@dataclass(order=True)
class AmbarellaCandidate:
    sort_key: Tuple[int, Tuple[int, ...], int] = field(init=False, repr=False)
    bus: int
    address: int
    ports: Tuple[int, ...]
    vid: int
    pid: int
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    info: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.sort_key = (self.bus, self.ports, self.address)

    @property
    def key(self) -> str:
        p = ".".join(str(x) for x in self.ports) if self.ports else "noport"
        return f"bus{self.bus:03d}-addr{self.address:03d}-ports-{p}"

    @property
    def port_path(self) -> str:
        return ".".join(str(x) for x in self.ports) if self.ports else "?"

    @property
    def human(self) -> str:
        chunks = [f"bus={self.bus:03d}", f"addr={self.address:03d}", f"ports={self.port_path}"]
        if self.serial:
            chunks.append(f"serial={self.serial}")
        if self.product:
            chunks.append(f"product={self.product}")
        return " ".join(chunks)


def ascii_z(payload: bytes, max_len: int = 80) -> str:
    data = payload[:max_len]
    if b"\x00" in data:
        data = data.split(b"\x00", 1)[0]
    return data.decode("ascii", errors="ignore").strip()


def ascii_z_fields(payload: bytes) -> List[str]:
    return [x.decode("ascii", errors="ignore").strip() for x in payload.split(b"\x00") if x.strip()]


def load_default_password(device_ini: str | Path) -> str:
    p = Path(device_ini)
    if not p.exists():
        return "000000"
    rx = re.compile(r"\{\s*(?P<type>\d+)\s*,\s*(?P<pwd>[^,}]+)\s*,")
    selected = None
    fallback = None
    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = rx.search(raw.strip())
        if not m:
            continue
        dtype = int(m.group("type"))
        pwd = m.group("pwd").strip()
        if fallback is None:
            fallback = pwd
        if dtype == 3:
            selected = pwd
            break
    return selected or fallback or "000000"


def safe_usb_string(dev: object, index: Optional[int]) -> str:
    if not usb or not index:
        return ""
    try:
        return usb.util.get_string(dev, index) or ""
    except Exception:
        return ""


def get_ports(dev: object) -> Tuple[int, ...]:
    try:
        ports = getattr(dev, "port_numbers")
        if ports:
            return tuple(int(x) for x in ports)
    except Exception:
        pass
    try:
        port = getattr(dev, "port_number")
        if port:
            return (int(port),)
    except Exception:
        pass
    return tuple()


def list_ambarella(vid: int, pid: int) -> List[AmbarellaCandidate]:
    if usb is None:
        raise RuntimeError("python3-usb is not installed. Run install_ubuntu.sh")
    result: List[AmbarellaCandidate] = []
    for dev in usb.core.find(find_all=True, idVendor=vid, idProduct=pid):
        result.append(AmbarellaCandidate(
            bus=int(getattr(dev, "bus", 0) or 0),
            address=int(getattr(dev, "address", 0) or 0),
            ports=get_ports(dev),
            vid=vid,
            pid=pid,
            manufacturer=safe_usb_string(dev, getattr(dev, "iManufacturer", None)),
            product=safe_usb_string(dev, getattr(dev, "iProduct", None)),
            serial=safe_usb_string(dev, getattr(dev, "iSerialNumber", None)),
        ))
    return sorted(result)


def find_device_again(cand: AmbarellaCandidate):
    if usb is None:
        return None
    for dev in usb.core.find(find_all=True, idVendor=cand.vid, idProduct=cand.pid):
        bus = int(getattr(dev, "bus", 0) or 0)
        addr = int(getattr(dev, "address", 0) or 0)
        ports = get_ports(dev)
        if bus == cand.bus and addr == cand.address:
            return dev
        if cand.ports and ports == cand.ports and bus == cand.bus:
            return dev
    return None


class AmbarellaSession:
    def __init__(self, dev, timeout_ms: int = 2500, verbose_usb: bool = False):
        self.dev = dev
        self.timeout_ms = timeout_ms
        self.verbose_usb = verbose_usb
        self.claimed = False

    def __enter__(self):
        try:
            self.dev.set_configuration()
        except Exception:
            pass
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except Exception:
            pass
        try:
            usb.util.claim_interface(self.dev, 0)
            self.claimed = True
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.claimed:
            try:
                usb.util.release_interface(self.dev, 0)
            except Exception:
                pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    @staticmethod
    def build_frame(command: int, payload: Optional[bytes] = None) -> bytes:
        frame = bytearray(FRAME_SIZE)
        frame[0] = 0xA1
        frame[1] = command & 0xFF
        frame[2] = 0x00
        frame[3] = command & 0xFF
        if payload:
            frame[4:4 + min(len(payload), FRAME_SIZE - 4)] = payload[:FRAME_SIZE - 4]
        return bytes(frame)

    def send(self, command: int, payload: Optional[bytes] = None, allow_in_fail: bool = False) -> bytes:
        frame = self.build_frame(command, payload)
        wrote = self.dev.ctrl_transfer(REQ_OUT, B_REQUEST, W_VALUE, W_INDEX, frame, timeout=self.timeout_ms)
        if wrote != FRAME_SIZE:
            raise RuntimeError(f"CMD 0x{command:02X}: OUT transferred {wrote}/{FRAME_SIZE}")
        try:
            raw = bytes(self.dev.ctrl_transfer(REQ_IN, B_REQUEST, W_VALUE, W_INDEX, FRAME_SIZE, timeout=self.timeout_ms))
        except Exception:
            if allow_in_fail:
                return b""
            raise
        if len(raw) < 4 or raw[0] != 0xA1 or raw[1] != command or raw[3] != command:
            raise RuntimeError(f"CMD 0x{command:02X}: bad response first16={raw[:16].hex(' ')}")
        return raw

    def authenticate(self, password: str) -> None:
        self.send(0x15)
        self.send(0x12, password.encode("ascii", errors="ignore"))

    def read_info(self, password: str, full: bool = False) -> Dict[str, str]:
        self.authenticate(password)
        responses: Dict[int, bytes] = {}
        for cmd in INFO_SEQUENCE_FULL if full else INFO_SEQUENCE_MIN:
            responses[cmd] = self.send(cmd)
        return parse_info_responses(responses)

    def switch_to_disk(self, password: str) -> None:
        self.authenticate(password)
        self.send(0x23, allow_in_fail=True)


def parse_info_responses(responses: Dict[int, bytes]) -> Dict[str, str]:
    info: Dict[str, str] = {}
    if 0x4F in responses and len(responses[0x4F]) > 4:
        info["type"] = str(responses[0x4F][4])
    if 0x17 in responses:
        fields = ascii_z_fields(responses[0x17][4:])
        if fields:
            info["device_id"] = fields[0]
        if len(fields) > 1:
            info["officer_id"] = fields[1]
    if 0x21 in responses:
        info["fw"] = ascii_z(responses[0x21][4:])
    if 0x1F in responses:
        info["storage"] = ascii_z(responses[0x1F][4:])
    if 0x1D in responses:
        info["resolution"] = ascii_z(responses[0x1D][4:])
    return info
