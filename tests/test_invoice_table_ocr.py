"""Synthetic invoice layouts only; never embed the user's invoice or identifiers."""

from integrations.ocr.fields import extract_fields


def row(text, n, *, bbox=None, confidence=0.92, page=1):
    return dict(
        text=text,
        page=page,
        line=n,
        method="tesseract",
        confidence=confidence,
        **({"bbox": bbox} if bbox else {}),
    )


def test_chinese_table_labels_and_numeric_total():
    lines = [
        row("普通发票 发票代码: 011002399999 发票号码: 12345678", 1),
        row("开 票 日 期: 2026 年 10 月 09 日 校验码: 12345", 2),
        row("价税合计（大写）捌拾壹元整 （小写）￥81.00", 3),
    ]
    f = extract_fields(lines)
    assert f["invoice_code"].value == "011002399999"
    assert f["invoice_number"].value == "12345678"
    assert f["issue_date"].value == "2026-10-09"
    assert f["total_amount"].value == "81.00"
    assert f["tax_amount"].value is None


def test_spatial_names_and_total_are_bound_to_same_page_and_row():
    lines = [
        row("发票号码: 12345678", 1, bbox=[700, 30, 950, 50]),
        row("名 称: 合成演示公司", 2, bbox=[70, 140, 400, 160]),
        row("价税合计（大写）捌拾壹元整", 3, bbox=[70, 440, 400, 460]),
        row("￥81.00", 4, bbox=[700, 440, 850, 460]),
        row("名 称: 合成演示酒店有限公司", 5, bbox=[70, 490, 500, 510]),
        row("￥999.00", 6, bbox=[700, 440, 850, 460], page=2),
    ]
    f = extract_fields(lines)
    assert f["buyer_name"].value == "合成演示公司"
    assert f["seller_name"].value == "合成演示酒店有限公司"
    assert f["total_amount"].value == "81.00"
    assert f["seller_name"].line == 5


def test_conflicts_masks_and_low_confidence_stay_unknown():
    lines = [
        row("发票号码: 12345678", 1),
        row("发票号码: 87654321", 2),
        row("销售方名称: 合成***酒店", 3),
        row("价税合计（小写）￥81.00", 4, confidence=0.3),
    ]
    f = extract_fields(lines)
    assert all(f[k].value is None for k in ("invoice_number", "seller_name", "total_amount"))


def test_line_noise_does_not_hide_high_confidence_amount_words():
    line = row("价税合计（大写）杂字 （小写）￥81.00", 1, confidence=0.4)
    line["words"] = [dict(text="￥81.00", confidence=0.96)]
    f = extract_fields([line])
    assert f["total_amount"].value == "81.00"
    assert f["total_amount"].confidence == 0.96


def test_no_total_from_item_amount_or_unlabelled_maximum():
    lines = [row("金额 81.00 税额", 1), row("￥999.00", 2), row("开票日期: 2026-02-30", 3)]
    f = extract_fields(lines)
    assert f["total_amount"].value is None
    assert f["tax_amount"].value is None
    assert f["issue_date"].value is None


def test_explicit_roles_override_position_and_signature_is_not_a_name():
    lines = [
        row("销售方名称: 合成酒店", 1, bbox=[70, 100, 400, 120]),
        row("购买方名称: 合成公司", 2, bbox=[70, 150, 400, 170]),
        row("价税合计: 81.00", 3, bbox=[70, 440, 400, 460]),
        row("销售方：（章）", 4, bbox=[700, 550, 900, 570]),
    ]
    f = extract_fields(lines)
    assert f["seller_name"].value == "合成酒店"
    assert f["buyer_name"].value == "合成公司"


def test_fragmented_or_excess_decimal_amount_is_not_truncated():
    for value in ("￥8 1.00", "￥81.001"):
        assert extract_fields([row("价税合计（小写）" + value, 1)])["total_amount"].value is None


def test_side_by_side_pdf_uses_seller_role_and_visual_positions():
    from integrations.ocr.worker import pdf_text_lines

    class TextPage:
        def __init__(self):
            self.glyphs = []

        def add(self, text, x, y, vertical=False):
            for index, char in enumerate(text):
                left = x if vertical else x + index * 9
                bottom = y - index * 12 if vertical else y
                self.glyphs.append((char, (left, bottom, left + 8, bottom + 8)))

        def count_chars(self):
            return len(self.glyphs)

        def get_text_range(self, index, count):
            return self.glyphs[index][0]

        def get_charbox(self, index):
            return self.glyphs[index][1]

    page = TextPage()
    # Labels precede their values in object order; both roles sit above totals.
    page.add("发票号码：", 420, 370)
    page.add("开票日期：", 420, 350)
    for x, role in ((20, "购买方信息"), (310, "销售方信息")):
        page.add(role, x, 300, vertical=True)
        page.add("名称：", x + 15, 300)
    page.add("26110000000012345678", 480, 370)
    page.add("2026年10月12日", 480, 350)
    page.add("合成采购科技有限公司", 65, 300)
    page.add("合成演示酒店有限公司", 355, 300)
    page.add("价税合计（大写）", 20, 80)
    page.add("（小写）￥456.78", 420, 80)
    fields = extract_fields(pdf_text_lines(page, 1, 400))
    assert fields["invoice_number"].value == "26110000000012345678"
    assert fields["issue_date"].value == "2026-10-12"
    assert fields["seller_name"].value == "合成演示酒店有限公司"
    assert fields["buyer_name"].value == "合成采购科技有限公司"
    assert fields["total_amount"].value == "456.78"


def test_vertical_role_information_is_not_a_company_name():
    lines = [
        row("销售方信息", 1, bbox=[300, 80, 310, 150]),
        row("名称：合成酒店有限公司", 2, bbox=[320, 80, 480, 95]),
        row("购买方信息", 3, bbox=[20, 80, 30, 150]),
        row("名称：合成公司", 4, bbox=[40, 80, 200, 95]),
    ]
    f = extract_fields(lines)
    assert f["seller_name"].value == "合成酒店有限公司"
    assert f["buyer_name"].value == "合成公司"
