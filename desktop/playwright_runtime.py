"""Playwright Chromium paths for the frozen Windows EXE.

Frozen Playwright resolves browsers with PLAYWRIGHT_BROWSERS_PATH=0
semantics (next to the bundled driver package). That folder is wiped on
the next unzip. Point the driver at AppData and seed it from the zip
when AppData is empty.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from . import paths

PLAYWRIGHT_BROWSERS_ENV = "PLAYWRIGHT_BROWSERS_PATH"
HEADLESS_SHELL_EXE = "chrome-headless-shell.exe"
SETTINGS_CHROMIUM_HINT = "Open Settings and click Install Chromium, then Recheck."
SOURCE_CHROMIUM_HINT = "In a terminal, run: playwright install chromium"


def playwright_user_browsers_dir() -> Path:
    """Stable Chromium dir that survives replacing the EXE zip."""
    return paths.user_data_dir() / "ms-playwright"


def bundled_playwright_driver_dir(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else paths.install_root()
    return base / "_internal" / "playwright" / "driver"


def bundled_playwright_browsers_dir(root: Path | None = None) -> Path:
    return bundled_playwright_driver_dir(root) / "package" / ".local-browsers"


def bundled_playwright_node(root: Path | None = None) -> Path:
    driver = bundled_playwright_driver_dir(root)
    win = driver / "node.exe"
    if win.is_file() or sys.platform == "win32":
        return win
    return driver / "node"


def bundled_playwright_cli(root: Path | None = None) -> Path:
    return bundled_playwright_driver_dir(root) / "package" / "cli.js"


def dist_local_browsers(dist_root: Path) -> Path:
    return Path(dist_root) / "_internal" / "playwright" / "driver" / "package" / ".local-browsers"


def _is_browser_tree(name: str) -> bool:
    return name.startswith(("chromium", "ffmpeg-"))


def find_headless_shell(browsers_dir: Path) -> Path | None:
    """Return chrome-headless-shell.exe (or the Unix binary) under browsers_dir."""
    if not browsers_dir.is_dir():
        return None
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell.exe",
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    )
    for pattern in patterns:
        matches = sorted(path for path in browsers_dir.glob(pattern) if path.is_file())
        if matches:
            return matches[0]
    for name in (HEADLESS_SHELL_EXE, "chrome-headless-shell"):
        found = sorted(path for path in browsers_dir.rglob(name) if path.is_file())
        if found:
            return found[0]
    return None


def chromium_present(browsers_dir: Path) -> bool:
    if find_headless_shell(browsers_dir) is not None:
        return True
    if not browsers_dir.is_dir():
        return False
    chrome_patterns = (
        "chromium-*/chrome-*/chrome.exe",
        "chromium-*/chrome-*/chrome",
        "chromium-*/chrome-linux/chrome",
    )
    return any(any(browsers_dir.glob(pattern)) for pattern in chrome_patterns)


def seed_user_browsers_from_bundled(src: Path, dest: Path) -> list[Path]:
    """Copy missing Chromium trees into AppData. Never overwrite existing trees."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    if not src.is_dir():
        return copied
    for child in sorted(src.iterdir()):
        if not _is_browser_tree(child.name):
            continue
        target = dest / child.name
        if target.exists():
            continue
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
        copied.append(target)
    return copied


def configure_playwright_browsers(
    *,
    frozen: bool | None = None,
    root: Path | None = None,
    dest: Path | None = None,
) -> Path:
    """Point PLAYWRIGHT_BROWSERS_PATH at AppData for a frozen EXE.

    If AppData is empty and the zip has `.local-browsers`, seed AppData.
    If the copy fails, fall back to the bundled folder so this launch works.
    Assignment, not setdefault: the frozen driver may already have `=0`.
    """
    use_frozen = paths.is_frozen() if frozen is None else frozen
    user_dir = Path(dest) if dest is not None else playwright_user_browsers_dir()
    if not use_frozen:
        return user_dir
    bundled = bundled_playwright_browsers_dir(root)
    try:
        if bundled.is_dir():
            seed_user_browsers_from_bundled(bundled, user_dir)
    except OSError:
        if chromium_present(bundled):
            os.environ[PLAYWRIGHT_BROWSERS_ENV] = str(bundled)
            return bundled
    user_dir.mkdir(parents=True, exist_ok=True)
    os.environ[PLAYWRIGHT_BROWSERS_ENV] = str(user_dir)
    return user_dir


def playwright_install_argv(root: Path | None = None) -> list[str] | None:
    """Argv that installs Chromium. Frozen builds use bundled node+cli."""
    if not paths.is_frozen():
        return [sys.executable, "-m", "playwright", "install", "chromium"]
    node = bundled_playwright_node(root)
    cli = bundled_playwright_cli(root)
    if node.is_file() and cli.is_file():
        return [str(node), str(cli), "install", "chromium"]
    return None


def playwright_install_environ(
    base: MutableMapping[str, str] | None = None,
    *,
    dest: Path | None = None,
) -> dict[str, str]:
    """Env for `cli.js install chromium`. Frozen builds target AppData."""
    env = dict(os.environ if base is None else base)
    if not paths.is_frozen():
        return env
    user_dir = Path(dest) if dest is not None else playwright_user_browsers_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    env[PLAYWRIGHT_BROWSERS_ENV] = str(user_dir)
    os.environ[PLAYWRIGHT_BROWSERS_ENV] = str(user_dir)
    return env


def chromium_user_hint() -> str:
    if paths.is_frozen():
        return SETTINGS_CHROMIUM_HINT
    return SOURCE_CHROMIUM_HINT


def site_packages_local_browsers() -> Path | None:
    try:
        import playwright
    except Exception:
        return None
    path = Path(playwright.__file__).resolve().parent / "driver" / "package" / ".local-browsers"
    return path if path.is_dir() else None


def _ms_playwright_candidates() -> Iterator[Path]:
    env = os.environ.get(PLAYWRIGHT_BROWSERS_ENV, "").strip()
    if env and env != "0":
        yield Path(env)
    packaged = site_packages_local_browsers()
    if packaged is not None:
        yield packaged
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "ms-playwright"
    yield Path.home() / "AppData" / "Local" / "ms-playwright"
    yield Path.home() / ".cache" / "ms-playwright"


def discover_installed_browsers() -> Path | None:
    for path in _ms_playwright_candidates():
        if chromium_present(path):
            return path
    return None


def copy_chromium_into_dist(dist_root: Path, source: Path | None = None) -> Path:
    """Copy Chromium + headless-shell into the PyInstaller dist tree."""
    dest = dist_local_browsers(dist_root)
    src = source if source is not None else discover_installed_browsers()
    if src is None or not src.is_dir():
        return dest
    try:
        if src.resolve() == dest.resolve():
            return dest
    except OSError:
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True)
    return dest


def require_dist_headless_shell(dist_root: Path) -> Path:
    """Fail CI if the artifact cannot launch headless Chromium."""
    dest = dist_local_browsers(dist_root)
    found = find_headless_shell(dest)
    if found is None:
        raise FileNotFoundError(
            f"{HEADLESS_SHELL_EXE} is missing from {dest}. "
            "Install Chromium with PLAYWRIGHT_BROWSERS_PATH=0, then rebuild."
        )
    return found


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dist = Path(args[0] if args else "dist/youtube-pipeline")
    copy_chromium_into_dist(dist)
    print(require_dist_headless_shell(dist))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
