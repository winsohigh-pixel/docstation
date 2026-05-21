"""
docstation.entry — точки входа для команд установленного пакета.

После `pip install -e .` эти функции вызываются системными командами:
  docstation-app     → GTK-приложение
  docstation-web     → web UI
  docstation-once    → один цикл импорта
  docstation-dryrun  → сухая проверка без скачивания

Каждая функция:
  1. Определяет рабочую директорию (где лежит StationConfig.linux.json).
  2. Меняет в неё cwd — чтобы относительные пути в конфиге работали.
  3. Передаёт управление основному коду.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# ── Поиск рабочей директории ───────────────────────────────────────────────
# Приоритет:
#   1. Переменная окружения DOCSTATION_DIR
#   2. /opt/docstation (установка через .deb / systemd)
#   3. ~/docstation_linux (распакованный архив в домашней папке)
#   4. Директория самого пакета (разработческий режим)

def _find_workdir() -> Path:
    env = os.environ.get("DOCSTATION_DIR")
    if env:
        p = Path(env)
        if p.exists():
            return p

    candidates = [
        Path("/opt/docstation"),
        Path.home() / "docstation_linux",
        Path.home() / "Downloads" / "docstation_linux",
    ]
    for c in candidates:
        if (c / "StationConfig.linux.json").exists():
            return c

    # Режим разработки: запуск из корня репозитория
    here = Path(__file__).resolve().parent.parent
    if (here / "StationConfig.linux.json").exists():
        return here

    # Нет конфига нигде — вернём /opt/docstation, он создастся при init
    return Path("/opt/docstation")


def _setup() -> str:
    """Меняет cwd в рабочую директорию, возвращает путь к конфигу."""
    workdir = _find_workdir()
    os.chdir(workdir)
    return str(workdir / "StationConfig.linux.json")


# ── Entry points ───────────────────────────────────────────────────────────

def run_app() -> None:
    """docstation-app — запустить нативное GTK-приложение."""
    config_path = _setup()
    # Компиляция байткода при каждом запуске (как в run_app.sh)
    import compileall
    compileall.compile_dir("docstation", quiet=True, force=False)
    from docstation.native_app import run_app as _run
    raise SystemExit(_run(config_path))


def run_web() -> None:
    """docstation-web — запустить web UI."""
    config_path = _setup()
    from docstation.main import main
    raise SystemExit(main(["--config", config_path, "web"]))


def run_once() -> None:
    """docstation-once — один полный цикл импорта."""
    config_path = _setup()
    from docstation.main import main
    raise SystemExit(main(["--config", config_path, "once"]))


def run_dryrun() -> None:
    """docstation-dryrun — сухая проверка без скачивания."""
    config_path = _setup()
    from docstation.main import main
    raise SystemExit(main(["--config", config_path, "dry-run"]))
