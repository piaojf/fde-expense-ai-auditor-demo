"""Generate synthetic invoices; no real company/customer/taxpayer data.

Run with Codex bundled Python (reportlab + Pillow), not application runtime.
Raster scan contains pixels only; OCR expectations are NOT embedded in metadata.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas

OUT = Path("tests/fixtures/invoices")
OUT.mkdir(parents=True, exist_ok=True)
ROWS = [
    "SYNTHETIC INVOICE - DEMO ONLY",
    "NOT A VALID TAX INVOICE",
    "Invoice Number: DEMO202609200001",
    "Invoice Code: DEMO00123456",
    "Issue Date: 2026-09-20",
    "Seller Name: Synthetic Shenzhen Hotel",
    "Buyer Name: Synthetic Demo Company",
    "Total Amount: CNY 600.00",
    "Tax Amount: CNY 33.96",
]


def main():
    pdf = canvas.Canvas(str(OUT / "text-invoice.pdf"), pagesize=(700, 550))
    pdf.setTitle("Synthetic demo invoice; not valid for tax")
    for i, row in enumerate(ROWS):
        pdf.setFont("Helvetica", 18 if i < 2 else 15)
        pdf.drawString(35, 500 - i * 48, row)
    pdf.save()
    image = Image.new("RGB", (1800, 1450), "#f3f1e9")
    draw = ImageDraw.Draw(image)
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    font = ImageFont.truetype(font_path, 44)
    draw.rectangle((55, 55, 1740, 1385), outline="#444444", width=3)
    for i, row in enumerate(ROWS):
        draw.text((95, 115 + i * 125), row, fill="#202020", font=font)
    image = image.rotate(0.35, resample=Image.Resampling.BICUBIC, fillcolor="#eeeae0")
    image.save(OUT / "scan-invoice.png")
    image.save(OUT / "scan-invoice.jpg", quality=85)
    image.save(OUT / "scan-invoice.pdf", resolution=200)
    # CJK-language gate; clearly synthetic pixels, not hidden text.
    chinese = Image.new("RGB", (1800, 1400), "white")
    cdraw = ImageDraw.Draw(chinese)
    cfont = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", 48)
    crows = ["合成票据 仅供测试 无税务效力", "发票号码: DEMO202609200002",
             "发票代码: DEMO00123456", "开票日期: 2026-09-20", "销售方名称: 合成深圳酒店",
             "购买方名称: 合成演示公司", "价税合计: 600.00", "税额: 33.96"]
    for i, row in enumerate(crows):
        cdraw.text((100, 100 + i * 140), row, font=cfont, fill="black")
    chinese.save(OUT / "scan-chinese.png")


if __name__ == "__main__":
    main()
