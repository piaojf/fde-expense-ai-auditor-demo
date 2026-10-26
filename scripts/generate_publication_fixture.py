"""Generate only a tiny synthetic future-dated policy; never an internal document."""

import zipfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "tests/fixtures/policies/draft-800.docx"
    target = root / "tests/fixtures/policies/publication-800.docx"
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as dst:
        for entry in src.infolist():
            data = src.read(entry)
            if entry.filename == "word/document.xml":
                data = data.replace(b"2026-draft-800", b"2026-11-800").replace(
                    b"2026-01-01", b"2026-11-01"
                )
            dst.writestr(entry.filename, data)
    print("Created synthetic future policy", target.name)


if __name__ == "__main__":
    main()
