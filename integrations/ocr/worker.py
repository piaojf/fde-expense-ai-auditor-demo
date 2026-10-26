"""Bounded disposable decode/OCR process; no file contents written to logs."""

import base64
import csv
import io
import json
import math
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageOps
from PIL.Image import open as open_image

from integrations.ocr.fields import aligned, chinese_spacing, extract_fields

MAX_PIXELS = 20_000_000
MAX_PAGES = 3
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class OCRUnavailableError(Exception):
    pass


def run_ocr(image, page, config, *, psm=6):
    with TemporaryDirectory(prefix="fde-ocr-") as folder:
        path = Path(folder) / "page.png"
        image.convert("RGB").save(path)
        command = [
            config["binary"],
            str(path),
            "stdout",
            "-l",
            config["languages"],
            "--psm",
            str(psm),
        ]
        if config.get("tessdata"):
            command.extend(["--tessdata-dir", config["tessdata"]])
        command.extend(["-c", "tessedit_create_tsv=1"])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=25,
                check=False,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OCRUnavailableError("OCR_UNAVAILABLE") from exc
        if result.returncode:
            raise OCRUnavailableError("OCR_UNAVAILABLE")
        groups = {}
        for word in csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t"):
            if word["level"] != "5" or not word.get("text", "").strip():
                continue
            key = (word["block_num"], word["par_num"], word["line_num"])
            groups.setdefault(key, []).append(word)
        return [
            dict(
                text=" ".join(w["text"] for w in words),
                page=page,
                line=i + 1,
                method="tesseract",
                confidence=max(0, sum(float(w["conf"]) for w in words) / len(words) / 100),
                bbox=[
                    min(int(w["left"]) for w in words),
                    min(int(w["top"]) for w in words),
                    max(int(w["left"]) + int(w["width"]) for w in words),
                    max(int(w["top"]) + int(w["height"]) for w in words),
                ],
                words=[
                    dict(text=w["text"], confidence=max(0, float(w["conf"]) / 100)) for w in words
                ],
            )
            for i, words in enumerate(groups.values())
        ]


def recognize_image(image, page, config):
    lines = run_ocr(image, page, config)
    fields = extract_fields(lines)
    required = ("invoice_number", "issue_date", "seller_name", "total_amount")
    if "chi_sim" not in config["languages"] or all(fields[k].value for k in required):
        return lines
    # Multi-column invoice tables and the header interfere with PSM 6's single
    # block assumption. Additional column passes preserve original coordinates
    # and provenance, allowing conservative same-row matching across blocks.
    split = int(image.width * 0.6)
    for left, right in ((0, split), (split, image.width)):
        crop = image.crop((left, 0, right, image.height))
        scale = min(2, math.sqrt(MAX_PIXELS / (crop.width * crop.height)))
        crop = crop.resize(
            (int(crop.width * scale), int(crop.height * scale)), Image.Resampling.LANCZOS
        )
        for row in run_ocr(crop, page, config):
            x1, y1, x2, y2 = row["bbox"]
            row["bbox"] = [x1 / scale + left, y1 / scale, x2 / scale + left, y2 / scale]
            row["line"] = len(lines) + 1
            lines.append(row)
    # Sparse total cells need single-line segmentation. Only refine currency
    # cells aligned with an explicit grand-total label; never select the largest
    # number on the page or infer a total from item rows.
    anchors = [r for r in lines if "价税合计" in chinese_spacing(r["text"])]
    targets = [
        r
        for r in lines
        if any(aligned(a, r) and r["bbox"][0] > a["bbox"][2] for a in anchors)
        and any(c in r["text"] for c in "¥￥")
    ]
    for target in targets[:2]:
        x1, y1, x2, y2 = target["bbox"]
        box = (
            max(0, int(x1) - 5),
            max(0, int(y1) - 5),
            min(image.width, math.ceil(x2) + 5),
            min(image.height, math.ceil(y2) + 5),
        )
        crop = image.crop(box)
        # Printed numeric totals are dark; remove coloured template labels and
        # rules from this already located cell, retaining its currency symbol.
        red, green, blue = crop.convert("RGB").split()
        crop = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
            lambda pixel: 0 if pixel < 150 else 255
        )
        scale = min(3, math.sqrt(MAX_PIXELS / (crop.width * crop.height)))
        crop = crop.resize(
            (int(crop.width * scale), int(crop.height * scale)), Image.Resampling.BICUBIC
        )
        for row in run_ocr(crop, page, config, psm=7):
            bx1, by1, bx2, by2 = row["bbox"]
            row["bbox"] = [
                bx1 / scale + box[0],
                by1 / scale + box[1],
                bx2 / scale + box[0],
                by2 / scale + box[1],
            ]
            row["line"] = len(lines) + 1
            lines.append(row)
    return lines


def pdf_text_lines(textpage, page, height):
    """Rebuild visual rows instead of trusting PDF object stream order."""
    glyphs = []
    for index in range(textpage.count_chars()):
        char = textpage.get_text_range(index, 1)
        if not char.strip() and char != " ":
            continue
        left, bottom, right, top = textpage.get_charbox(index)
        glyphs.append((char, [left, height - top, right, height - bottom]))
    rows = []
    for char, box in sorted(glyphs, key=lambda item: (sum(item[1][1::2]) / 2, item[1][0])):
        center = (box[1] + box[3]) / 2
        match = next((r for r in rows if abs(r[0] - center) < 4), None)
        if match is None:
            rows.append([center, [(char, box)]])
        else:
            match[0] = (match[0] * len(match[1]) + center) / (len(match[1]) + 1)
            match[1].append((char, box))
    result = []

    def append(text, boxes):
        result.append(
            dict(
                text=text,
                page=page,
                line=len(result) + 1,
                method="pdf-text",
                confidence=None,
                bbox=[
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ],
            )
        )

    for _, row in rows:
        segment = []
        for char, box in sorted(row, key=lambda item: item[1][0]):
            if segment and box[0] - segment[-1][1][2] > 24:
                append("".join(c for c, _ in segment), [b for _, b in segment])
                segment = []
            segment.append((char, box))
        if segment:
            append("".join(c for c, _ in segment), [b for _, b in segment])
    # Vertical role labels are common in side-by-side electronic VAT invoices.
    for role in ("销售方信息", "购买方信息"):
        for char, box in glyphs:
            if char != role[0]:
                continue
            column = sorted(
                (
                    (c, b)
                    for c, b in glyphs
                    if abs(b[0] - box[0]) < 3 and box[1] <= b[1] <= box[1] + 65
                ),
                key=lambda item: item[1][1],
            )
            if "".join(c for c, _ in column).startswith(role):
                append(role, [b for _, b in column[: len(role)]])
    return result


def inspect(config):
    path = Path(config["path"])
    raw = path.read_bytes()
    mode = config["mode"]
    lines, methods = [], []
    image = None
    if raw.startswith(b"%PDF-"):
        if b"%%EOF" not in raw[-2048:]:
            raise ValueError("INVALID_FILE")
        with pdfium.PdfDocument(raw) as doc:
            if not 1 <= len(doc) <= MAX_PAGES:
                raise ValueError("FILE_COMPLEXITY_LIMIT")
            pages = len(doc)
            for i in range(pages):
                with closing(doc[i]) as page:
                    width, height = page.get_size()
                    if (
                        not all(math.isfinite(v) and 0 < v <= 2000 for v in (width, height))
                        or width * height * 4 > MAX_PIXELS
                    ):
                        raise ValueError("FILE_COMPLEXITY_LIMIT")
                    if mode == "ocr":
                        with closing(page.get_textpage()) as textpage:
                            text = textpage.get_text_range()
                            if len(text.strip()) >= 20:
                                if len(text) > 100_000:
                                    raise ValueError("FILE_COMPLEXITY_LIMIT")
                                text_lines = [
                                    dict(
                                        text=t,
                                        page=i + 1,
                                        line=n + 1,
                                        method="pdf-text",
                                        confidence=None,
                                    )
                                    for n, t in enumerate(text.splitlines())
                                    if t.strip()
                                ]
                                parsed = extract_fields(text_lines)
                                required = (
                                    "invoice_number",
                                    "issue_date",
                                    "seller_name",
                                    "total_amount",
                                )
                                lines.extend(
                                    text_lines
                                    if all(parsed[k].value for k in required)
                                    else pdf_text_lines(textpage, i + 1, height)
                                )
                                methods.append("pdf-text")
                                continue
                    with closing(page.render(scale=2 if mode == "ocr" else 1)) as bitmap:
                        image = bitmap.to_pil().copy()
                    if mode == "ocr":
                        lines.extend(recognize_image(image, i + 1, config))
                        methods.append("tesseract")
                    if mode == "preview":
                        break
            mime = "application/pdf"
    else:
        with open_image(io.BytesIO(raw)) as candidate:
            if candidate.format not in ("PNG", "JPEG") or getattr(candidate, "n_frames", 1) != 1:
                raise ValueError("INVALID_FILE")
            if candidate.width * candidate.height > MAX_PIXELS:
                raise ValueError("FILE_COMPLEXITY_LIMIT")
            mime = "image/png" if candidate.format == "PNG" else "image/jpeg"
            candidate.verify()
        with open_image(io.BytesIO(raw)) as candidate:
            candidate.load()
            image = ImageOps.exif_transpose(candidate).convert("RGB")
        pages = 1
        if mode == "ocr":
            lines = recognize_image(image, 1, config)
            methods = ["tesseract"]
    result = dict(content_type=mime, pages=pages, lines=lines, methods=sorted(set(methods)))
    if mode == "preview":
        image.thumbnail((1200, 1600))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        result["preview"] = base64.b64encode(buffer.getvalue()).decode()
    return result


def main():
    try:
        result = inspect(json.loads(sys.stdin.read()))
    except OCRUnavailableError:
        result = {"error": "OCR_UNAVAILABLE"}
    except Exception:
        result = {"error": "INVALID_FILE"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
