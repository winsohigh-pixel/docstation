from __future__ import annotations

import datetime as _dt
from pathlib import Path


class Logger:
    def __init__(self, path: str | Path = "logs/docstation.log", verbose: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.verbose = verbose
        self._fp = self.path.open("a", encoding="utf-8", buffering=1)

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass

    def log(self, message: str) -> None:
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"{ts} {message}"
        self._fp.write(line + "\n")
        if self.verbose:
            print(line)
