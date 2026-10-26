"""Disposable policy parser; PDF/OCR reuses UI-P2, DOCX never executes Office."""

import io
import json
import resource
import sys
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from integrations.ocr.worker import OCRUnavailableError, inspect

MAX_TEXT = 100_000
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def xml(raw):
    normalized = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in normalized or b"<!ENTITY" in normalized:
        raise ValueError("UNSAFE_XML")
    return ET.fromstring(raw)


def docx(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        entries = package.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 200 or len(names) != len(set(names)):
            raise ValueError("FILE_COMPLEXITY_LIMIT")
        if sum(e.file_size for e in entries) > 20 * 1024 * 1024:
            raise ValueError("FILE_COMPLEXITY_LIMIT")
        for entry in entries:
            name = entry.filename
            if (
                "\\" in name
                or name.startswith("/")
                or ":" in name
                or ".." in PurePosixPath(name).parts
                or entry.flag_bits & 1
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("INVALID_FILE")
            if entry.file_size > 5 * 1024 * 1024 or (
                entry.file_size > max(1, entry.compress_size) * 100
            ):
                raise ValueError("FILE_COMPLEXITY_LIMIT")
            if any(v in name.lower() for v in ("vbaproject", "embeddings/", "activex", "altchunk")):
                raise ValueError("INVALID_FILE")
            if name.endswith((".xml", ".rels")):
                root = xml(package.read(entry))
                if name.endswith(".rels"):
                    for relation in root:
                        if (
                            relation.get("TargetMode", "").lower() == "external"
                            or ".." in PurePosixPath(relation.get("Target", "")).parts
                        ):
                            raise ValueError("INVALID_FILE")
        types = xml(package.read("[Content_Types].xml"))
        expected = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        )
        if not any(
            e.get("PartName") == "/word/document.xml" and e.get("ContentType") == expected
            for e in types
        ):
            raise ValueError("INVALID_FILE")
        pages = None  # DOCX layout cannot be paginated reliably without Office.
        if "docProps/app.xml" in names:
            app = xml(package.read("docProps/app.xml"))
            declared = next((e.text for e in app.iter() if e.tag.endswith("}Pages")), None)
            if declared:
                count = int(declared)
                if not 0 <= count <= 3:
                    raise ValueError("FILE_COMPLEXITY_LIMIT")
        document = xml(package.read("word/document.xml"))
        if any(
            e.tag in {WORD_NS + "altChunk", WORD_NS + "object", WORD_NS + "instrText"}
            for e in document.iter()
        ):
            raise ValueError("INVALID_FILE")
        paragraphs = list(document.iter(WORD_NS + "p"))
        if len(paragraphs) > 2000:
            raise ValueError("FILE_COMPLEXITY_LIMIT")
        lines = []
        for i, paragraph in enumerate(paragraphs):
            text = "".join(e.text or "" for e in paragraph.iter(WORD_NS + "t")).strip()
            if text:
                lines.append(
                    dict(text=text, page=None, line=i + 1, method="docx-text", confidence=None)
                )
        return dict(
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            pages=pages,
            methods=["docx-text"],
            lines=lines,
        )


def parse(config):
    raw = Path(config["path"]).read_bytes()
    if not 0 < len(raw) <= 10 * 1024 * 1024:
        raise ValueError("FILE_COMPLEXITY_LIMIT")
    if config["extension"] == ".pdf" and raw.startswith(b"%PDF-"):
        result = inspect({**config, "mode": "ocr"})
    elif config["extension"] == ".docx" and raw.startswith(b"PK\x03\x04"):
        result = docx(raw)
    else:
        raise ValueError("INVALID_FILE")
    text = "\n".join(line["text"] for line in result["lines"])
    if not text.strip():
        raise ValueError("NO_TEXT")
    if len(text) > MAX_TEXT:
        raise ValueError("FILE_COMPLEXITY_LIMIT")
    offset = 0
    for line in result["lines"]:
        line["start"] = offset
        offset += len(line["text"])
        line["end"] = offset
        offset += 1
    result["text"] = text
    return result


def main():
    # CPU limit inherited by Tesseract. Address-space limit Linux only; macOS PDFium
    # reserves large virtual mappings. Parent deadline kills the entire process group.
    resource.setrlimit(resource.RLIMIT_CPU, (80, 80))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    try:
        result = parse(json.loads(sys.stdin.read()))
    except OCRUnavailableError:
        result = {"error": "OCR_UNAVAILABLE"}
    except ValueError as exc:
        known = {"NO_TEXT", "FILE_COMPLEXITY_LIMIT"}
        result = {"error": str(exc) if str(exc) in known else "INVALID_FILE"}
    except Exception:
        result = {"error": "INVALID_FILE"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
