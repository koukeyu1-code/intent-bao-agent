from __future__ import annotations

import io
import zipfile
from html import escape
from pathlib import Path

import pytest

from app.taxonomy_excel import (
    EXPECTED_HEADERS,
    TaxonomyValidationError,
    normalize_menu_value,
    normalize_taxonomy_key,
    normalize_taxonomy_text,
    parse_taxonomy_xlsx,
    preflight_taxonomy_xlsx,
)


SAMPLE_WORKBOOK = (
    Path(__file__).resolve().parents[3]
    / "参考资料"
    / "意向宝展示看板表单设计_V1.0.xlsx"
)


def _cell(reference: str, value: object) -> str:
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}'
        "</t></is></c>"
    )


def _minimal_xlsx(rows: dict[int, list[object | None]]) -> bytes:
    sheet_rows: list[str] = []
    columns = ("B", "C", "D", "E", "F")
    for row_number, values in sorted(rows.items()):
        cells = "".join(
            _cell(f"{column}{row_number}", value)
            for column, value in zip(columns, values, strict=True)
            if value is not None
        )
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types">'
            '<Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
            '2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" '
            'r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
            '2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


def _valid_rows() -> dict[int, list[object | None]]:
    return {
        2: list(EXPECTED_HEADERS),
        3: ["三模", "尺寸", 27, "分辨率", "QHD"],
        4: [None, None, 31.5, None, "UHD"],
    }


def test_generated_workbook_builds_cartesian_product_not_row_pairs() -> None:
    result = preflight_taxonomy_xlsx(
        _minimal_xlsx(_valid_rows()),
        filename="taxonomy.xlsx",
    )

    assert result["valid"] is True
    assert result["errors"] == []
    assert result["preview"]["counts"] == {
        "technologies": 1,
        "level2Values": 2,
        "level3Values": 2,
        "platforms": 4,
    }
    assert [item["name"] for item in result["preview"]["platforms"]] == [
        "27 QHD",
        "27 UHD",
        "31.5 QHD",
        "31.5 UHD",
    ]
    technology = result["preview"]["technologies"][0]
    assert technology["level2"]["name"] == "尺寸"
    assert technology["level3"]["name"] == "分辨率"


@pytest.mark.skipif(
    not SAMPLE_WORKBOOK.exists(),
    reason="项目外部参考工作簿未随测试环境提供",
)
def test_supplied_workbook_creates_four_three_mode_platforms() -> None:
    result = preflight_taxonomy_xlsx(SAMPLE_WORKBOOK)

    assert result["valid"] is True
    assert result["preview"]["sheetName"] == "Sheet1"
    assert result["preview"]["headerRow"] == 2
    three_mode = next(
        item
        for item in result["preview"]["technologies"]
        if item["name"] == "三模"
    )
    assert [item["name"] for item in three_mode["platforms"]] == [
        "27 QHD",
        "27 UHD",
        "31.5 QHD",
        "31.5 UHD",
    ]
    assert result["preview"]["counts"]["technologies"] == 6
    assert result["preview"]["counts"]["platforms"] == 4


def test_wrong_header_is_rejected_without_preview_table() -> None:
    rows = _valid_rows()
    rows[2] = ["错误一级表头", *EXPECTED_HEADERS[1:]]

    result = preflight_taxonomy_xlsx(
        _minimal_xlsx(rows),
        filename="bad-header.xlsx",
    )

    assert result["valid"] is False
    assert result["preview"] is None
    assert [item["code"] for item in result["errors"]] == ["HEADER_NOT_FOUND"]


def test_duplicate_values_are_reported_after_stable_normalization() -> None:
    rows = _valid_rows()
    rows[4] = [None, None, "27.0", None, " qhd "]

    result = preflight_taxonomy_xlsx(
        _minimal_xlsx(rows),
        filename="duplicates.xlsx",
    )

    assert result["valid"] is False
    assert {item["code"] for item in result["errors"]} == {
        "DUPLICATE_LEVEL2_VALUE",
        "DUPLICATE_LEVEL3_VALUE",
    }
    assert result["preview"]["counts"]["platforms"] == 1
    assert result["preview"]["platforms"][0]["name"] == "27 QHD"


def test_empty_and_inconsistent_menu_definitions_are_reported() -> None:
    rows = {
        2: list(EXPECTED_HEADERS),
        3: ["三模", "尺寸", None, "分辨率", None],
        4: [None, "屏幕尺寸", None, "清晰度", None],
    }

    result = preflight_taxonomy_xlsx(
        _minimal_xlsx(rows),
        filename="inconsistent.xlsx",
    )

    assert result["valid"] is False
    assert {item["code"] for item in result["errors"]} == {
        "INCONSISTENT_LEVEL2_MENU_NAME",
        "INCONSISTENT_LEVEL3_MENU_NAME",
        "EMPTY_LEVEL2_MENU",
        "EMPTY_LEVEL3_MENU",
        "LEVEL3_WITHOUT_LEVEL2",
    }


def test_values_cannot_cross_fill_from_a_previous_technology() -> None:
    rows = _valid_rows()
    rows[5] = ["Mini LED", None, "34", None, "UHD"]

    result = preflight_taxonomy_xlsx(
        _minimal_xlsx(rows),
        filename="orphan-values.xlsx",
    )

    assert result["valid"] is False
    assert {item["code"] for item in result["errors"]} == {
        "LEVEL2_VALUE_WITHOUT_MENU_NAME",
        "LEVEL3_VALUE_WITHOUT_MENU_NAME",
    }


@pytest.mark.parametrize(
    ("filename", "content", "expected_code"),
    [
        ("taxonomy.xls", b"not-xlsx", "INVALID_EXTENSION"),
        ("taxonomy.xlsx", b"not-xlsx", "INVALID_XLSX_ARCHIVE"),
        ("taxonomy.xlsx", b"", "EMPTY_FILE"),
    ],
)
def test_file_contract_errors_are_structured(
    filename: str,
    content: bytes,
    expected_code: str,
) -> None:
    result = preflight_taxonomy_xlsx(content, filename=filename)

    assert result["valid"] is False
    assert result["preview"] is None
    assert result["errors"][0]["code"] == expected_code


def test_size_limit_stops_processing_before_zip_read() -> None:
    content = _minimal_xlsx(_valid_rows())
    result = preflight_taxonomy_xlsx(
        content,
        filename="too-large.xlsx",
        max_size_bytes=100,
    )

    assert result["valid"] is False
    assert [item["code"] for item in result["errors"]] == [
        "FILE_SIZE_EXCEEDED"
    ]


def test_normalization_is_idempotent_and_keys_are_case_insensitive() -> None:
    original = "  ＱＨＤ\u00a0  显示  "
    once = normalize_taxonomy_text(original)
    twice = normalize_taxonomy_text(once)

    assert once == "QHD 显示"
    assert twice == once
    assert normalize_taxonomy_key(" ＱＨＤ ") == normalize_taxonomy_key("qhd")
    assert normalize_menu_value("31.500") == "31.5"
    assert normalize_menu_value(normalize_menu_value("31.500")) == "31.5"


def test_same_bytes_produce_identical_previews_and_parse_raises_on_errors() -> None:
    content = _minimal_xlsx(_valid_rows())
    first = preflight_taxonomy_xlsx(content, filename="taxonomy.xlsx")
    second = preflight_taxonomy_xlsx(content, filename="taxonomy.xlsx")

    assert first["preview"] == second["preview"]
    assert first["file"]["sha256"] == second["file"]["sha256"]
    assert parse_taxonomy_xlsx(content, filename="taxonomy.xlsx") == first["preview"]
    with pytest.raises(TaxonomyValidationError) as exc_info:
        parse_taxonomy_xlsx(b"broken", filename="taxonomy.xlsx")
    assert exc_info.value.errors[0]["code"] == "INVALID_XLSX_ARCHIVE"
