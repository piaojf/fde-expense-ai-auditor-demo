"""Synthetic policy samples only; generated with bundled reportlab/Pillow."""

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

OUT = Path("tests/fixtures/policies")
ROWS = [
    "SYNTHETIC POLICY - LOCAL DEMO ONLY - NOT PUBLISHED",
    "Title: Synthetic reimbursement policy",
    "Company: DEMO-CN",
    "Region: 深圳",
    "Expense Type: HOTEL",
    "Version: 2026-draft-800",
    "Effective From: 2026-01-01",
    "Effective To: 2027-01-01",
    "Article 1: 2026 Shenzhen accommodation limit is CNY 800 per night.",
    "Article 2: Original receipts and manager confirmation are required.",
    "This draft must not replace the currently ACTIVE CNY 700 policy.",
]


def make_docx(rows):
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            """
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml"
ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>
""",
        )
        z.writestr(
            "_rels/.rels",
            """
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
Target="word/document.xml"/></Relationships>
""",
        )
        paragraphs = "".join(f"<w:p><w:r><w:t>{escape(row)}</w:t></w:r></w:p>" for row in rows)
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>' + paragraphs + "</w:body></w:document>",
        )
    return buffer.getvalue()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(OUT / "draft-800.pdf"), pagesize=(880, 740), invariant=1)
    pdf.setTitle("Synthetic policy draft; NOT PUBLISHED")
    pdf.setFont("STSong-Light", 18)
    for i, row in enumerate(ROWS):
        pdf.drawString(35, 680 - i * 53, row)
    pdf.save()
    (OUT / "draft-800.docx").write_bytes(make_docx(ROWS))
    image = Image.new("RGB", (2000, 1700), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 38)
    for i, row in enumerate(ROWS):
        draw.text((60, 90 + i * 135), row.replace("深圳", "Shenzhen"), font=font, fill="black")
    image.save(OUT / "scan-draft-800.pdf", resolution=200)


if __name__ == "__main__":
    main()
