"""Local-file trust boundary for bank CSV analysis."""

from __future__ import annotations

from pathlib import Path


MAX_CSV_BYTES = 10 * 1024 * 1024


def validate_local_csv(path: str | Path, allowed_root: str | Path | None = None) -> Path:
    root = Path(allowed_root or Path.cwd()).resolve()
    candidate = Path(path).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"CSV must be located inside the project workspace: {root}") from exc
    if candidate.is_symlink():
        raise ValueError("Symbolic-link CSV inputs are not allowed.")
    if candidate.suffix.lower() != ".csv":
        raise ValueError("Only .csv bank transaction files are allowed.")
    size = candidate.stat().st_size
    if size == 0:
        raise ValueError("The CSV file is empty.")
    if size > MAX_CSV_BYTES:
        raise ValueError(f"CSV exceeds the {MAX_CSV_BYTES // (1024 * 1024)} MB limit.")
    return candidate


def validate_output_path(path: str | Path, allowed_root: str | Path | None = None) -> Path:
    root = Path(allowed_root or Path.cwd()).resolve()
    candidate = Path(path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Report output must remain inside the project workspace.") from exc
    if candidate.suffix.lower() != ".html":
        raise ValueError("Report output must be an .html file.")
    return candidate
