"""Inspect serialized exports rather than only sanitizer return values."""

import csv
import io
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook

from app import exporting


DANGEROUS_TEXT = [
    "=1+1", "+SUM(1,2)", "-1+2", "@SUM(1,2)",
    "  =1+1", "\t=1+1", "\r=1+1", "\n=1+1", "\ufeff=1+1",
    "\t采购意见", '=HYPERLINK("https://example.invalid","证据")',
]


@pytest.fixture
def export_rows(monkeypatch):
    rows = [
        {"原文": value, "支持理由": value, "技术": value, "置信度": 0.95}
        for value in DANGEROUS_TEXT
    ]
    rows.extend([
        {"原文": '采购明确支持，成本需验证\n第二行含"引号"', "支持理由": "正常", "技术": "MiniLED", "置信度": 0.8},
        {"原文": "含控制字符\x00和\x1b，证据可阅读", "支持理由": "正常", "技术": "MiniLED", "置信度": 0.7},
    ])
    formal = [{"原文": "=2+2", "支持理由": "+1+1", "技术": "@SUM(1,2)", "置信度": ""}]
    monkeypatch.setattr(exporting, "_package_rows", lambda *_args: (rows, formal))
    return rows, formal


def test_csv_formula_prefixes_are_text_after_csv_parsing(export_rows):
    rows, formal = export_rows
    filename, content = exporting.export_package(None, "test", "csv")
    assert filename.endswith(".csv")
    assert content.startswith(b"\xef\xbb\xbf")
    serialized = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    assert len(serialized) == len(rows) + len(formal)
    for original, actual in zip(rows[:len(DANGEROUS_TEXT)], serialized):
        for field in ("原文", "支持理由", "技术"):
            assert actual[field] == "'" + original[field]
        assert actual["置信度"] == "0.95"
    assert serialized[-1]["原文"] == "'=2+2"
    assert serialized[-1]["支持理由"] == "'+1+1"
    assert serialized[-1]["技术"] == "'@SUM(1,2)"
    assert serialized[-3]["原文"] == rows[-2]["原文"]
    assert serialized[-2]["原文"] == "含控制字符\\x00和\\x1b，证据可阅读"


def test_xlsx_has_text_cells_and_no_formula_nodes(export_rows):
    rows, _formal = export_rows
    filename, content = exporting.export_package(None, "test", "xlsx")
    assert filename.endswith(".xlsx")
    workbook = load_workbook(io.BytesIO(content), data_only=False)
    assert workbook.sheetnames == ["意向观察", "正式状态"]
    sheet = workbook["意向观察"]
    for row_index, original in enumerate(rows[:len(DANGEROUS_TEXT)], 2):
        for column, field in enumerate(("原文", "支持理由", "技术"), 1):
            cell = sheet.cell(row_index, column)
            # XML readers normalize literal CR line endings to LF.
            assert cell.value == original[field].replace("\r\n", "\n").replace("\r", "\n")
            assert cell.data_type == "s"
        assert sheet.cell(row_index, 4).value == 0.95
        assert sheet.cell(row_index, 4).data_type == "n"
    assert sheet.cell(len(rows), 1).value == rows[-2]["原文"]
    assert sheet.cell(len(rows) + 1, 1).value == "含控制字符\\x00和\\x1b，证据可阅读"
    assert workbook["正式状态"]["A2"].value == "=2+2"
    assert workbook["正式状态"]["A2"].data_type == "s"
    workbook.close()
    with ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                assert b"<f" not in archive.read(name)
