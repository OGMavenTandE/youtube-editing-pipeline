"""Write selected keys into the app .env without logging secret values."""

from __future__ import annotations

from pathlib import Path


def read_env_value(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    prefix = f"{key}="
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.startswith(prefix):
            continue
        return line[len(prefix) :].strip().strip('"').strip("'")
    return ""


def upsert_env_value(path: Path, key: str, value: str) -> None:
    """Replace or append KEY=value. Never includes the value in exceptions."""
    if not key or not key.replace("_", "").isalnum():
        raise ValueError("Invalid environment key.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    prefix = f"{key}="
    out: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        is_assignment = stripped.startswith(prefix)
        is_commented = stripped.startswith(f"#{key}=") or stripped.startswith(f"# {key}=")
        if is_assignment or is_commented:
            if not found:
                out.append(f"{key}={value}")
                found = True
            continue
        out.append(line)
    if not found:
        if out and out[-1] != "":
            out.append("")
        out.append(f"{key}={value}")
    try:
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError as exc:
        raise OSError(f"Could not write {key} to the app .env file.") from exc
