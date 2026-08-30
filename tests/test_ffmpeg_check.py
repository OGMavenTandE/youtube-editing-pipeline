from __future__ import annotations

import os
from pathlib import Path

from pipeline.config import which_or_path

from desktop.ffmpeg_check import (
    REG_EXPAND_SZ,
    check_ffmpeg,
    common_ffmpeg_probe_paths,
    expand_registry_path,
    first_existing_file,
    locate_ffmpeg,
    merge_windows_path,
    prepend_dir_to_path,
    probe_windows_ffmpeg,
    refresh_os_path_from_registry,
    sibling_ffprobe,
    winget_gyan_ffmpeg_exes,
)


def test_merge_windows_path_joins_machine_then_user() -> None:
    merged = merge_windows_path(
        r"C:\Windows\system32;C:\Windows",
        r"C:\Users\scott\bin;C:\Windows\system32",
    )
    assert merged == r"C:\Windows\system32;C:\Windows;C:\Users\scott\bin"


def test_merge_windows_path_skips_empty_and_quoted() -> None:
    merged = merge_windows_path(r'"C:\Program Files\foo";;', r";C:\bar;")
    assert merged == r"C:\Program Files\foo;C:\bar"


def test_expand_registry_path_expands_expand_sz() -> None:
    def expander(value: str) -> str:
        return value.replace("%LOCALAPPDATA%", r"C:\Users\scott\AppData\Local")

    raw = r"%LOCALAPPDATA%\Microsoft\WinGet\Links"
    assert expand_registry_path(raw, REG_EXPAND_SZ, expander) == (
        r"C:\Users\scott\AppData\Local\Microsoft\WinGet\Links"
    )
    assert expand_registry_path(raw, 1, expander) == raw


def test_refresh_os_path_from_registry_replaces_process_path() -> None:
    environ = {"PATH": r"C:\stale\bin"}
    merged = refresh_os_path_from_registry(
        r"C:\Windows\system32",
        r"C:\Users\scott\AppData\Local\Microsoft\WinGet\Links",
        environ,
    )
    assert environ["PATH"] == merged
    assert r"C:\Windows\system32" in merged
    assert r"WinGet\Links" in merged
    assert r"C:\stale\bin" not in merged


def test_refresh_os_path_from_registry_uses_expanded_values() -> None:
    machine = expand_registry_path(
        r"%SystemRoot%\system32",
        REG_EXPAND_SZ,
        lambda value: value.replace("%SystemRoot%", r"C:\Windows"),
    )
    user = expand_registry_path(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links",
        REG_EXPAND_SZ,
        lambda value: value.replace("%LOCALAPPDATA%", r"C:\Users\scott\AppData\Local"),
    )
    environ: dict[str, str] = {"PATH": "old"}
    refresh_os_path_from_registry(machine, user, environ)
    assert environ["PATH"] == (
        r"C:\Windows\system32;C:\Users\scott\AppData\Local\Microsoft\WinGet\Links"
    )


def _write_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake")
    path.chmod(0o755)
    return path


def test_winget_gyan_glob_finds_nested_bin(tmp_path: Path) -> None:
    packages = tmp_path / "Microsoft" / "WinGet" / "Packages"
    ffmpeg = _write_exe(
        packages
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-8.0-full_build"
        / "bin"
        / "ffmpeg.exe"
    )
    _write_exe(ffmpeg.with_name("ffprobe.exe"))
    found = winget_gyan_ffmpeg_exes(packages)
    assert found == [ffmpeg]


def test_probe_windows_ffmpeg_winget_then_common_dirs(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    program_files = tmp_path / "Program Files"
    ffmpeg = _write_exe(
        local
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-7.1-full_build"
        / "bin"
        / "ffmpeg.exe"
    )
    assert (
        probe_windows_ffmpeg(
            local_app_data=local,
            program_files=program_files,
            program_files_x86=tmp_path / "Program Files (x86)",
        )
        == ffmpeg
    )


def test_probe_windows_ffmpeg_program_files(tmp_path: Path) -> None:
    program_files = tmp_path / "Program Files"
    ffmpeg = _write_exe(program_files / "ffmpeg" / "bin" / "ffmpeg.exe")
    assert (
        probe_windows_ffmpeg(
            local_app_data=tmp_path / "empty-local",
            program_files=program_files,
        )
        == ffmpeg
    )


def test_common_probe_paths_include_c_ffmpeg_and_program_files(tmp_path: Path) -> None:
    paths = common_ffmpeg_probe_paths(
        local_app_data=tmp_path / "Local",
        program_files=tmp_path / "Program Files",
        program_files_x86=tmp_path / "Program Files (x86)",
    )
    assert Path(r"C:\ffmpeg\bin\ffmpeg.exe") in paths
    assert tmp_path / "Program Files" / "ffmpeg" / "bin" / "ffmpeg.exe" in paths
    assert tmp_path / "Program Files (x86)" / "ffmpeg" / "bin" / "ffmpeg.exe" in paths


def test_first_existing_file_skips_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "ffmpeg.exe"
    present = _write_exe(tmp_path / "yes" / "ffmpeg.exe")
    assert first_existing_file([missing, present]) == present
    assert first_existing_file([missing]) is None


def test_sibling_ffprobe_next_to_ffmpeg(tmp_path: Path) -> None:
    ffmpeg = _write_exe(tmp_path / "bin" / "ffmpeg.exe")
    assert sibling_ffprobe(ffmpeg) is None
    probe = _write_exe(tmp_path / "bin" / "ffprobe.exe")
    assert sibling_ffprobe(ffmpeg) == probe


def test_prepend_dir_to_path_puts_bin_first() -> None:
    environ = {"PATH": r"C:\Windows\system32;C:\old\ffmpeg\bin"}
    prepend_dir_to_path(r"C:\Users\scott\ffmpeg\bin", environ, pathsep=";")
    assert environ["PATH"].startswith(r"C:\Users\scott\ffmpeg\bin;")
    assert environ["PATH"].count(r"C:\Users\scott\ffmpeg\bin") == 1


def test_locate_ffmpeg_refreshes_path_then_which(monkeypatch, tmp_path: Path) -> None:
    ffmpeg = _write_exe(tmp_path / "winget-links" / "ffmpeg.exe")
    _write_exe(tmp_path / "winget-links" / "ffprobe.exe")
    monkeypatch.setenv("PATH", "/stale")

    def fake_refresh(environ=None):
        target = os.environ if environ is None else environ
        target["PATH"] = str(ffmpeg.parent)
        return target["PATH"]

    monkeypatch.setattr("desktop.ffmpeg_check.sys.platform", "win32")
    monkeypatch.setattr("desktop.ffmpeg_check.refresh_windows_path", fake_refresh)
    monkeypatch.setattr("desktop.ffmpeg_check.which_or_path", lambda binary: None)
    monkeypatch.setattr(
        "desktop.ffmpeg_check.shutil.which",
        lambda name: str(ffmpeg) if name in {"ffmpeg", "ffmpeg.exe"} else None,
    )
    monkeypatch.setattr("desktop.ffmpeg_check._default_probe_ffmpeg", lambda: None)

    found = locate_ffmpeg("ffmpeg")
    assert found == str(ffmpeg)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(ffmpeg.parent)


def test_locate_ffmpeg_probes_when_which_misses(monkeypatch, tmp_path: Path) -> None:
    ffmpeg = _write_exe(
        tmp_path
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-8.0-full_build"
        / "bin"
        / "ffmpeg.exe"
    )
    _write_exe(ffmpeg.with_name("ffprobe.exe"))
    monkeypatch.setenv("PATH", "/stale")

    monkeypatch.setattr("desktop.ffmpeg_check.sys.platform", "win32")
    monkeypatch.setattr("desktop.ffmpeg_check.refresh_windows_path", lambda environ=None: "stale")
    monkeypatch.setattr("desktop.ffmpeg_check.which_or_path", lambda binary: None)
    monkeypatch.setattr("desktop.ffmpeg_check.shutil.which", lambda name: None)
    monkeypatch.setattr("desktop.ffmpeg_check._default_probe_ffmpeg", lambda: ffmpeg)

    found = locate_ffmpeg()
    assert found == str(ffmpeg)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(ffmpeg.parent)
    assert sibling_ffprobe(found) == ffmpeg.with_name("ffprobe.exe")


def test_check_ffmpeg_reports_found_path(monkeypatch, tmp_path: Path) -> None:
    ffmpeg = _write_exe(tmp_path / "bin" / "ffmpeg.exe")
    monkeypatch.setattr(
        "desktop.ffmpeg_check.load_settings",
        lambda: type("S", (), {"ffmpeg_bin": "ffmpeg"})(),
    )
    monkeypatch.setattr("desktop.ffmpeg_check.locate_ffmpeg", lambda preferred: str(ffmpeg))
    check = check_ffmpeg()
    assert check.found
    assert check.path == str(ffmpeg)
    assert check.name == "FFmpeg"


def test_adopted_bin_dir_makes_ffprobe_visible_to_which(monkeypatch, tmp_path: Path) -> None:
    ffmpeg = _write_exe(tmp_path / "bin" / "ffmpeg")
    probe = _write_exe(tmp_path / "bin" / "ffprobe")
    monkeypatch.setenv("PATH", "/stale")
    from desktop.ffmpeg_check import _adopt_ffmpeg_bin

    _adopt_ffmpeg_bin(str(ffmpeg))
    assert which_or_path("ffprobe") == str(probe)
    assert sibling_ffprobe(ffmpeg) == probe


def test_check_ffmpeg_not_found_keeps_hint(monkeypatch) -> None:
    monkeypatch.setattr(
        "desktop.ffmpeg_check.load_settings",
        lambda: type("S", (), {"ffmpeg_bin": "ffmpeg"})(),
    )
    monkeypatch.setattr("desktop.ffmpeg_check.locate_ffmpeg", lambda preferred: None)
    check = check_ffmpeg()
    assert not check.found
    assert check.path is None
    assert "winget install Gyan.FFmpeg" in check.hint
