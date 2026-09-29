from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "dist"
OUTPUT = OUTPUT_DIR / "ai-digest-debian-handoff.zip"

EXCLUDED_PARTS = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "research",
    "tmp",
    "var",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".session", ".sqlite3"}
EXCLUDED_NAMES = {".env", ".env.production"}


def included(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return (
        path.is_file()
        and not any(part in EXCLUDED_PARTS for part in relative.parts)
        and path.name not in EXCLUDED_NAMES
        and path.suffix.lower() not in EXCLUDED_SUFFIXES
    )


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    files = sorted(path for path in ROOT.rglob("*") if included(path))
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())

    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    checksum = OUTPUT.with_suffix(f"{OUTPUT.suffix}.sha256")
    checksum.write_text(f"{digest}  {OUTPUT.name}\n", encoding="ascii")
    print(f"archive={OUTPUT}")
    print(f"files={len(files)}")
    print(f"sha256={digest}")


if __name__ == "__main__":
    main()
