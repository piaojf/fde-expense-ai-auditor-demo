"""Independent Synthetic Policy Fixture. No private policy content is read or copied."""

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

OUT = Path(__file__).resolve().parents[1] / "tests/fixtures/policies/public-synthetic-matrix.docx"
GRADES = ("A", "B", "C", "D")
LIMITS = ((1250, 1050, 900, 750), (1100, 900, 760, 640), (950, 800, 670, 550), (800, 660, 530, 430))


def build():
    def paragraph(value):
        return f"<w:p><w:r><w:t>{escape(value)}</w:t></w:r></w:p>"

    def table(rows):
        return (
            "<w:tbl>"
            + "".join(
                "<w:tr>"
                + "".join("<w:tc>" + paragraph(str(cell)) + "</w:tc>" for cell in row)
                + "</w:tr>"
                for row in rows
            )
            + "</w:tbl>"
        )

    body = paragraph("Synthetic Policy Fixture — Fictional Example Labs — PUBLIC-SYNTHETIC-V1")
    body += paragraph("虚构示例实验室；仅用于公开测试，无财务授权效力。")
    body += paragraph("有效期 [2026-11-01, 2027-01-01)；适用企业 SYNTHETIC-LABS。")
    body += paragraph("第一章 城市分级")
    body += table(
        [
            ["城市档位", "城市范围", "级别", "说明"],
            ["Ⅰ类城市", "深圳", "PREFECTURE", "合成分类"],
            ["Ⅱ类城市", "杭州", "PREFECTURE", "合成分类"],
            ["Ⅲ类城市", "示例地市", "PREFECTURE", "合成分类"],
            ["Ⅳ类城市", "示例县", "COUNTY", "合成分类"],
        ]
    )
    body += paragraph("第二章 员工等级")
    body += paragraph("员工等级 A、B、C、D 必须由可信员工档案提供，不接受申报人指定。")
    body += paragraph("第三章 住宿标准")
    body += paragraph("人民币金额；货币单位为元。发票、住宿记录及业务目的须独立核查。")
    body += table(
        [
            ["职级每间每晚", "Ⅰ类城市", "Ⅱ类城市", "Ⅲ类城市", "Ⅳ类城市"],
            *[[g + " 合成员工", *limits] for g, limits in zip(GRADES, LIMITS, strict=True)],
        ]
    )
    body += paragraph("第四章 人工审核")
    body += paragraph("财务独立审批；AI 仅提供建议。超限不得由模型绕过；例外须另行授权并留痕。")
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + body + "</w:body></w:document>"
    )
    content = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, data in (("[Content_Types].xml", content), ("word/document.xml", document)):
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data.encode())
    return output.getvalue()


if __name__ == "__main__":
    OUT.write_bytes(build())
    print("Generated independent Synthetic Policy Fixture:", OUT.name)
