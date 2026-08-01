from __future__ import annotations

import hashlib
import io
import posixpath
import re
import unicodedata
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree


EXPECTED_HEADERS = (
    "一级菜单_名称",
    "二级菜单_名称",
    "二级菜单_内容",
    "三级菜单_名称",
    "三级菜单_内容",
)
DEFAULT_MAX_XLSX_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_XLSX_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2_000
MAX_HEADER_SCAN_ROWS = 50

_RELATIONSHIP_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_CELL_REFERENCE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")
_SIMPLE_DECIMAL = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class TaxonomyWorkbookError(ValueError):
    """A safe, user-presentable error raised while reading an XLSX package."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TaxonomyValidationError(ValueError):
    """Raised by :func:`parse_taxonomy_xlsx` when preflight has errors."""

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("Excel 产品平台层级预检未通过")
        self.errors = errors


def normalize_taxonomy_text(value: Any) -> str:
    """Return stable display text without changing its semantic spelling.

    Unicode compatibility normalization makes full-width characters comparable,
    and all whitespace runs (including non-breaking spaces and line breaks) are
    collapsed to one ordinary space.  Applying this function repeatedly is
    idempotent.
    """

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).replace("\u00a0", " ")
    return " ".join(text.split())


def normalize_taxonomy_key(value: Any) -> str:
    """Return the case-insensitive key used for merge and duplicate checks."""

    return normalize_taxonomy_text(value).casefold()


def normalize_menu_value(value: Any) -> str:
    """Normalize a level value, including harmless decimal zero trimming."""

    text = normalize_taxonomy_text(value)
    if not text or not _SIMPLE_DECIMAL.fullmatch(text):
        return text
    try:
        decimal = Decimal(text)
    except InvalidOperation:
        return text
    if not decimal.is_finite():
        return text
    normalized = format(decimal.normalize(), "f")
    return "0" if normalized in {"-0", "+0"} else normalized


def _issue(
    code: str,
    message: str,
    *,
    sheet: str | None = None,
    row: int | None = None,
    column: str | None = None,
    value: str | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "message": message}
    if sheet is not None:
        issue["sheet"] = sheet
    if row is not None:
        issue["row"] = row
    if column is not None:
        issue["column"] = column
    if value is not None:
        issue["value"] = value
    return issue


def _column_number(reference: str) -> int:
    match = _CELL_REFERENCE.fullmatch(reference.upper())
    if match is None:
        raise TaxonomyWorkbookError(
            "INVALID_CELL_REFERENCE",
            f"工作表包含无法识别的单元格引用：{reference}",
        )
    number = 0
    for character in match.group(1):
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _safe_member_name(base: str, target: str) -> str:
    target = target.replace("\\", "/")
    if target.startswith("/"):
        normalized = posixpath.normpath(target.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(base, target))
    if normalized == ".." or normalized.startswith("../"):
        raise TaxonomyWorkbookError(
            "UNSAFE_XLSX_PATH",
            "Excel 文件包含不安全的内部路径。",
        )
    return normalized


def _xml_root(archive: zipfile.ZipFile, member: str) -> ElementTree.Element:
    try:
        payload = archive.read(member)
    except KeyError:
        raise TaxonomyWorkbookError(
            "MISSING_XLSX_PART",
            f"Excel 文件缺少必要组件：{member}",
        ) from None
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        raise TaxonomyWorkbookError(
            "INVALID_XLSX_XML",
            f"Excel 文件组件损坏：{member}",
        ) from None


def _text_from_cell(
    cell: ElementTree.Element,
    shared_strings: list[str],
) -> str:
    cell_type = cell.attrib.get("t")
    value_node = next(
        (child for child in cell if child.tag.rsplit("}", 1)[-1] == "v"),
        None,
    )
    if cell_type == "inlineStr":
        return "".join(
            node.text or ""
            for node in cell.iter()
            if node.tag.rsplit("}", 1)[-1] == "t"
        )
    raw = "" if value_node is None else value_node.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            raise TaxonomyWorkbookError(
                "INVALID_SHARED_STRING",
                "Excel 文件包含失效的共享字符串引用。",
            ) from None
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if cell_type == "e":
        return raw or "#ERROR!"
    if cell_type in {None, "n"} and raw:
        return normalize_menu_value(raw)
    return raw


def _worksheet_rows(
    archive: zipfile.ZipFile,
    member: str,
    shared_strings: list[str],
) -> dict[int, dict[int, str]]:
    root = _xml_root(archive, member)
    rows: dict[int, dict[int, str]] = {}
    inferred_row = 0
    for row_node in root.iter():
        if row_node.tag.rsplit("}", 1)[-1] != "row":
            continue
        inferred_row += 1
        try:
            row_number = int(row_node.attrib.get("r", inferred_row))
        except ValueError:
            row_number = inferred_row
        row_values: dict[int, str] = {}
        inferred_column = 0
        for cell in row_node:
            if cell.tag.rsplit("}", 1)[-1] != "c":
                continue
            reference = cell.attrib.get("r")
            if reference:
                column = _column_number(reference)
            else:
                inferred_column += 1
                column = inferred_column
            inferred_column = column
            row_values[column] = _text_from_cell(cell, shared_strings)
        rows[row_number] = row_values
    return rows


def _load_sheet_rows(content: bytes) -> list[tuple[str, dict[int, dict[int, str]]]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile):
        raise TaxonomyWorkbookError(
            "INVALID_XLSX_ARCHIVE",
            "文件不是有效的 XLSX 压缩包，或文件已经损坏。",
        ) from None

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise TaxonomyWorkbookError(
                "XLSX_TOO_COMPLEX",
                "Excel 文件内部组件过多，请精简后重新上传。",
            )
        if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_XLSX_BYTES:
            raise TaxonomyWorkbookError(
                "XLSX_EXPANDED_SIZE_EXCEEDED",
                "Excel 解压后的内容超过 50 MB 安全上限。",
            )
        member_names = [info.filename.replace("\\", "/") for info in infos]
        if len(member_names) != len(set(member_names)):
            raise TaxonomyWorkbookError(
                "DUPLICATE_XLSX_PART",
                "Excel 文件包含重复的内部组件。",
            )
        for member_name in member_names:
            normalized = posixpath.normpath(member_name)
            if normalized == ".." or normalized.startswith("../"):
                raise TaxonomyWorkbookError(
                    "UNSAFE_XLSX_PATH",
                    "Excel 文件包含不安全的内部路径。",
                )
        required = {"[Content_Types].xml", "xl/workbook.xml"}
        if not required.issubset(set(member_names)):
            raise TaxonomyWorkbookError(
                "MISSING_XLSX_STRUCTURE",
                "文件缺少 XLSX 工作簿结构，可能只是更改了扩展名。",
            )

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in member_names:
            shared_root = _xml_root(archive, "xl/sharedStrings.xml")
            for item in shared_root:
                if item.tag.rsplit("}", 1)[-1] != "si":
                    continue
                shared_strings.append(
                    "".join(
                        node.text or ""
                        for node in item.iter()
                        if node.tag.rsplit("}", 1)[-1] == "t"
                    )
                )

        workbook_root = _xml_root(archive, "xl/workbook.xml")
        relations_root = _xml_root(archive, "xl/_rels/workbook.xml.rels")
        relation_targets: dict[str, str] = {}
        for relation in relations_root:
            if relation.tag.rsplit("}", 1)[-1] != "Relationship":
                continue
            relation_id = relation.attrib.get("Id")
            target = relation.attrib.get("Target")
            if relation_id and target:
                relation_targets[relation_id] = _safe_member_name("xl", target)

        sheets: list[tuple[str, dict[int, dict[int, str]]]] = []
        for sheet in workbook_root.iter():
            if sheet.tag.rsplit("}", 1)[-1] != "sheet":
                continue
            sheet_name = normalize_taxonomy_text(sheet.attrib.get("name"))
            relation_id = sheet.attrib.get(f"{{{_RELATIONSHIP_NS}}}id")
            member = relation_targets.get(relation_id or "")
            if not sheet_name or not member:
                continue
            if member not in member_names:
                raise TaxonomyWorkbookError(
                    "MISSING_WORKSHEET",
                    f"Excel 工作表“{sheet_name}”缺少数据组件。",
                )
            sheets.append(
                (sheet_name, _worksheet_rows(archive, member, shared_strings))
            )
        if not sheets:
            raise TaxonomyWorkbookError(
                "NO_WORKSHEET",
                "Excel 文件中没有可读取的工作表。",
            )
        return sheets


def _matching_headers(
    sheets: list[tuple[str, dict[int, dict[int, str]]]],
) -> list[tuple[str, int, dict[int, dict[int, str]]]]:
    matches: list[tuple[str, int, dict[int, dict[int, str]]]] = []
    expected = tuple(normalize_taxonomy_key(item) for item in EXPECTED_HEADERS)
    for sheet_name, rows in sheets:
        for row_number in sorted(rows):
            if row_number > MAX_HEADER_SCAN_ROWS:
                break
            values = tuple(
                normalize_taxonomy_key(rows[row_number].get(column, ""))
                for column in range(2, 7)
            )
            if values == expected:
                matches.append((sheet_name, row_number, rows))
    return matches


def _add_unique_value(
    technology: dict[str, Any],
    *,
    level: int,
    value: str,
    row_number: int,
    sheet_name: str,
    errors: list[dict[str, Any]],
) -> None:
    key = normalize_taxonomy_key(value)
    value_map: dict[str, dict[str, Any]] = technology[f"_level{level}Values"]
    if key in value_map:
        first_row = value_map[key]["sourceRows"][0]
        errors.append(
            _issue(
                f"DUPLICATE_LEVEL{level}_VALUE",
                f"同一技术的{level}级菜单内容重复；首次出现在第{first_row}行。",
                sheet=sheet_name,
                row=row_number,
                column="D" if level == 2 else "F",
                value=value,
            )
        )
        value_map[key]["sourceRows"].append(row_number)
        return
    value_map[key] = {
        "name": value,
        "key": key,
        "sourceRows": [row_number],
    }


def _build_preview(
    *,
    sheet_name: str,
    header_row: int,
    rows: dict[int, dict[int, str]],
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    technologies_by_key: dict[str, dict[str, Any]] = {}
    current_technology: dict[str, Any] | None = None
    current_level2_name = ""
    current_level3_name = ""
    nonempty_data_rows = 0

    for row_number in sorted(rows):
        if row_number <= header_row:
            continue
        raw_values = [rows[row_number].get(column, "") for column in range(2, 7)]
        values = [normalize_taxonomy_text(value) for value in raw_values]
        if not any(values):
            continue
        nonempty_data_rows += 1
        technology_name, level2_name, level2_raw, level3_name, level3_raw = values

        if technology_name:
            technology_key = normalize_taxonomy_key(technology_name)
            current_technology = technologies_by_key.get(technology_key)
            if current_technology is None:
                current_technology = {
                    "name": technology_name,
                    "key": technology_key,
                    "sourceRows": [],
                    "_level2Name": "",
                    "_level2Key": "",
                    "_level2NameRow": None,
                    "_level2Values": {},
                    "_level3Name": "",
                    "_level3Key": "",
                    "_level3NameRow": None,
                    "_level3Values": {},
                }
                technologies_by_key[technology_key] = current_technology
            current_level2_name = ""
            current_level3_name = ""
        elif current_technology is None:
            errors.append(
                _issue(
                    "MISSING_TECHNOLOGY_NAME",
                    "该行存在菜单数据，但没有可向下填充的一级菜单名称。",
                    sheet=sheet_name,
                    row=row_number,
                    column="B",
                )
            )
            continue

        assert current_technology is not None
        current_technology["sourceRows"].append(row_number)

        if level2_name:
            current_level2_name = level2_name
            level2_key = normalize_taxonomy_key(level2_name)
            existing_key = current_technology["_level2Key"]
            if existing_key and existing_key != level2_key:
                errors.append(
                    _issue(
                        "INCONSISTENT_LEVEL2_MENU_NAME",
                        "同一技术只能配置一个二级菜单名称；"
                        f"当前为“{level2_name}”，此前为“{current_technology['_level2Name']}”。",
                        sheet=sheet_name,
                        row=row_number,
                        column="C",
                        value=level2_name,
                    )
                )
            elif not existing_key:
                current_technology["_level2Name"] = level2_name
                current_technology["_level2Key"] = level2_key
                current_technology["_level2NameRow"] = row_number

        if level3_name:
            current_level3_name = level3_name
            level3_key = normalize_taxonomy_key(level3_name)
            existing_key = current_technology["_level3Key"]
            if existing_key and existing_key != level3_key:
                errors.append(
                    _issue(
                        "INCONSISTENT_LEVEL3_MENU_NAME",
                        "同一技术只能配置一个三级菜单名称；"
                        f"当前为“{level3_name}”，此前为“{current_technology['_level3Name']}”。",
                        sheet=sheet_name,
                        row=row_number,
                        column="E",
                        value=level3_name,
                    )
                )
            elif not existing_key:
                current_technology["_level3Name"] = level3_name
                current_technology["_level3Key"] = level3_key
                current_technology["_level3NameRow"] = row_number

        level2_value = normalize_menu_value(level2_raw)
        if level2_value:
            if not current_level2_name:
                errors.append(
                    _issue(
                        "LEVEL2_VALUE_WITHOUT_MENU_NAME",
                        "二级菜单内容缺少可向下填充的二级菜单名称。",
                        sheet=sheet_name,
                        row=row_number,
                        column="D",
                        value=level2_value,
                    )
                )
            else:
                _add_unique_value(
                    current_technology,
                    level=2,
                    value=level2_value,
                    row_number=row_number,
                    sheet_name=sheet_name,
                    errors=errors,
                )

        level3_value = normalize_menu_value(level3_raw)
        if level3_value:
            if not current_level3_name:
                errors.append(
                    _issue(
                        "LEVEL3_VALUE_WITHOUT_MENU_NAME",
                        "三级菜单内容缺少可向下填充的三级菜单名称。",
                        sheet=sheet_name,
                        row=row_number,
                        column="F",
                        value=level3_value,
                    )
                )
            else:
                _add_unique_value(
                    current_technology,
                    level=3,
                    value=level3_value,
                    row_number=row_number,
                    sheet_name=sheet_name,
                    errors=errors,
                )

    if nonempty_data_rows == 0:
        errors.append(
            _issue(
                "EMPTY_TAXONOMY_DATA",
                "表头下方没有可导入的技术或产品平台数据。",
                sheet=sheet_name,
                row=header_row + 1,
            )
        )

    technology_previews: list[dict[str, Any]] = []
    flat_platforms: list[dict[str, Any]] = []
    for technology in technologies_by_key.values():
        level2_values = list(technology["_level2Values"].values())
        level3_values = list(technology["_level3Values"].values())
        level2_name = technology["_level2Name"]
        level3_name = technology["_level3Name"]

        if level2_name and not level2_values:
            errors.append(
                _issue(
                    "EMPTY_LEVEL2_MENU",
                    f"技术“{technology['name']}”的二级菜单“{level2_name}”没有内容。",
                    sheet=sheet_name,
                    row=technology["_level2NameRow"],
                    column="D",
                )
            )
        if level3_name and not level3_values:
            errors.append(
                _issue(
                    "EMPTY_LEVEL3_MENU",
                    f"技术“{technology['name']}”的三级菜单“{level3_name}”没有内容。",
                    sheet=sheet_name,
                    row=technology["_level3NameRow"],
                    column="F",
                )
            )
        if (level3_name or level3_values) and not (level2_name and level2_values):
            errors.append(
                _issue(
                    "LEVEL3_WITHOUT_LEVEL2",
                    f"技术“{technology['name']}”存在三级菜单，但没有完整的二级菜单。",
                    sheet=sheet_name,
                    row=technology["_level3NameRow"] or technology["sourceRows"][0],
                    column="E",
                )
            )

        platforms: list[dict[str, Any]] = []
        if level2_values and level3_values:
            combinations = (
                (level2_value, level3_value)
                for level2_value in level2_values
                for level3_value in level3_values
            )
        elif level2_values:
            combinations = ((level2_value, None) for level2_value in level2_values)
        else:
            combinations = iter(())

        seen_platform_keys: set[str] = set()
        for level2_value, level3_value in combinations:
            parts = [level2_value["name"]]
            if level3_value is not None:
                parts.append(level3_value["name"])
            platform_name = " ".join(parts)
            platform_key = "|".join(
                [
                    technology["key"],
                    level2_value["key"],
                    level3_value["key"] if level3_value is not None else "",
                ]
            )
            if platform_key in seen_platform_keys:
                continue
            seen_platform_keys.add(platform_key)
            platform = {
                "name": platform_name,
                "key": platform_key,
                "technologyName": technology["name"],
                "technologyKey": technology["key"],
                "level2Value": level2_value["name"],
                "level2Key": level2_value["key"],
                "level3Value": (
                    level3_value["name"] if level3_value is not None else None
                ),
                "level3Key": (
                    level3_value["key"] if level3_value is not None else None
                ),
                "path": [
                    technology["name"],
                    level2_value["name"],
                    *(
                        [level3_value["name"]]
                        if level3_value is not None
                        else []
                    ),
                ],
            }
            platforms.append(platform)
            flat_platforms.append(platform.copy())

        if not level2_name and not level3_name:
            warnings.append(
                _issue(
                    "TECHNOLOGY_WITHOUT_PLATFORM_MENU",
                    f"技术“{technology['name']}”尚未配置产品平台层级，将只导入技术名称。",
                    sheet=sheet_name,
                    row=technology["sourceRows"][0],
                    column="B",
                )
            )

        technology_previews.append(
            {
                "name": technology["name"],
                "key": technology["key"],
                "sourceRows": sorted(set(technology["sourceRows"])),
                "level2": (
                    {
                        "name": level2_name,
                        "key": technology["_level2Key"],
                        "values": level2_values,
                    }
                    if level2_name
                    else None
                ),
                "level3": (
                    {
                        "name": level3_name,
                        "key": technology["_level3Key"],
                        "values": level3_values,
                    }
                    if level3_name
                    else None
                ),
                "platforms": platforms,
            }
        )

    return {
        "sheetName": sheet_name,
        "headerRow": header_row,
        "dataRowCount": nonempty_data_rows,
        "counts": {
            "technologies": len(technology_previews),
            "level2Values": sum(
                len(item["level2"]["values"]) if item["level2"] else 0
                for item in technology_previews
            ),
            "level3Values": sum(
                len(item["level3"]["values"]) if item["level3"] else 0
                for item in technology_previews
            ),
            "platforms": len(flat_platforms),
        },
        "technologies": technology_previews,
        "platforms": flat_platforms,
    }


def _read_source(
    source: bytes | bytearray | memoryview | str | Path | BinaryIO,
    filename: str | None,
) -> tuple[bytes, str]:
    if isinstance(source, (str, Path)):
        source_path = Path(source)
        return source_path.read_bytes(), filename or source_path.name
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source), filename or "upload.xlsx"
    raw = source.read()
    if not isinstance(raw, bytes):
        raise TypeError("Excel 文件对象必须以二进制模式读取")
    inferred_name = getattr(source, "name", None)
    return raw, filename or (Path(inferred_name).name if inferred_name else "upload.xlsx")


def preflight_taxonomy_xlsx(
    source: bytes | bytearray | memoryview | str | Path | BinaryIO,
    *,
    filename: str | None = None,
    max_size_bytes: int = DEFAULT_MAX_XLSX_BYTES,
) -> dict[str, Any]:
    """Read and validate a product-platform taxonomy workbook without writes.

    The returned dictionary contains only JSON-compatible values.  A semantic
    preview is included even when row-level validation errors exist, enabling a
    UI to show the proposed Cartesian-product platforms before an administrator
    explicitly commits a separate import transaction.
    """

    content, resolved_filename = _read_source(source, filename)
    file_info = {
        "name": resolved_filename,
        "sizeBytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    preview: dict[str, Any] | None = None

    if Path(resolved_filename).suffix.casefold() != ".xlsx":
        errors.append(
            _issue(
                "INVALID_EXTENSION",
                "仅支持 .xlsx 格式的数据字典文件。",
                value=resolved_filename,
            )
        )
    if not content:
        errors.append(_issue("EMPTY_FILE", "上传的 Excel 文件为空。"))
    elif len(content) > max_size_bytes:
        errors.append(
            _issue(
                "FILE_SIZE_EXCEEDED",
                f"Excel 文件超过 {max_size_bytes // (1024 * 1024)} MB 上限。",
            )
        )

    if errors:
        return {
            "valid": False,
            "file": file_info,
            "preview": None,
            "errors": errors,
            "warnings": warnings,
        }

    try:
        sheets = _load_sheet_rows(content)
        header_matches = _matching_headers(sheets)
        if not header_matches:
            errors.append(
                _issue(
                    "HEADER_NOT_FOUND",
                    "未在任一工作表的 B:F 列找到规定表头："
                    + "、".join(EXPECTED_HEADERS),
                )
            )
        elif len(header_matches) > 1:
            locations = "、".join(
                f"{sheet}!B{row}:F{row}" for sheet, row, _rows in header_matches
            )
            errors.append(
                _issue(
                    "MULTIPLE_TAXONOMY_TABLES",
                    f"发现多个产品平台层级表头（{locations}），请只保留一个。",
                )
            )
        else:
            sheet_name, header_row, rows = header_matches[0]
            preview = _build_preview(
                sheet_name=sheet_name,
                header_row=header_row,
                rows=rows,
                errors=errors,
                warnings=warnings,
            )
    except TaxonomyWorkbookError as exc:
        errors.append(_issue(exc.code, exc.message))
    except (OSError, RuntimeError, zipfile.LargeZipFile):
        errors.append(
            _issue(
                "XLSX_READ_FAILED",
                "Excel 文件读取失败，请确认文件未损坏且未加密。",
            )
        )

    return {
        "valid": not errors,
        "file": file_info,
        "preview": preview,
        "errors": errors,
        "warnings": warnings,
    }


def parse_taxonomy_xlsx(
    source: bytes | bytearray | memoryview | str | Path | BinaryIO,
    *,
    filename: str | None = None,
    max_size_bytes: int = DEFAULT_MAX_XLSX_BYTES,
) -> dict[str, Any]:
    """Return a valid preview or raise :class:`TaxonomyValidationError`."""

    result = preflight_taxonomy_xlsx(
        source,
        filename=filename,
        max_size_bytes=max_size_bytes,
    )
    if not result["valid"]:
        raise TaxonomyValidationError(result["errors"])
    return result["preview"]


__all__ = [
    "DEFAULT_MAX_XLSX_BYTES",
    "EXPECTED_HEADERS",
    "TaxonomyValidationError",
    "TaxonomyWorkbookError",
    "normalize_menu_value",
    "normalize_taxonomy_key",
    "normalize_taxonomy_text",
    "parse_taxonomy_xlsx",
    "preflight_taxonomy_xlsx",
]
