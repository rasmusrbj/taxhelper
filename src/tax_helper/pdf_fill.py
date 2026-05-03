from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RUBRIC_COLUMN_MIN_X = 390.0
RUBRIC_COLUMN_MAX_X = 455.0
VALUE_RIGHT_X = 555.0
VALUE_MIN_X = 458.0


@dataclass(frozen=True)
class FillPdfResult:
    output_path: Path
    filled_codes: list[str]
    missing_position_codes: list[str]
    page_count: int


@dataclass(frozen=True)
class RubricPosition:
    code: str
    page_number: int
    page_width: float
    page_height: float
    top: float
    height: float


@dataclass(frozen=True)
class PageImage:
    page_number: int
    path: Path
    page_width: float
    page_height: float
    image_width: int
    image_height: int
    components: int


@dataclass(frozen=True)
class OverlayText:
    text: str
    right_x: float
    top: float
    height: float
    font_size: float


def load_fill_values(path: Path, *, field_to_code: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"invalid JSON values file: {path}") from err
    if not isinstance(data, dict):
        raise ValueError("fill values file must contain a JSON object")

    rubrics: dict[str, Any] = {}
    unknown_keys: list[str] = []
    collect_rubric_values(data.get("rubrics"), rubrics)
    collect_template_values(data, rubrics)

    fields: dict[str, Any] = {}
    collect_mapping_values(data.get("fields"), fields)
    for field_no, value in fields.items():
        code = field_to_code.get(field_no)
        if code is None:
            unknown_keys.append(f"field:{field_no}")
            continue
        rubrics[code] = value

    known_top_level = {"rubrics", "fields", "sections", "ok", "kind", "year", "filters", "summary"}
    for key, value in data.items():
        if key in known_top_level:
            continue
        code = normalize_rubric_key(key)
        if code:
            rubrics[code] = value
            continue
        if key.isdigit() and key in field_to_code:
            rubrics[field_to_code[key]] = value
            continue
        unknown_keys.append(key)

    return {code: value for code, value in rubrics.items() if value is not None}, unknown_keys


def collect_mapping_values(value: Any, target: dict[str, Any]) -> None:
    if value is None:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            scalar = unwrap_fill_value(item)
            if scalar is not None:
                target[str(key)] = scalar
        return
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            key = item.get("code") or item.get("rubric") or item.get("rubrik") or item.get("field")
            scalar = unwrap_fill_value(item)
            if key is not None and scalar is not None:
                target[str(key)] = scalar


def collect_rubric_values(value: Any, target: dict[str, Any]) -> None:
    collected: dict[str, Any] = {}
    collect_mapping_values(value, collected)
    for key, item in collected.items():
        code = normalize_rubric_key(key)
        if code:
            target[code] = item


def collect_template_values(data: dict[str, Any], target: dict[str, Any]) -> None:
    for section in data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for rubric in section.get("rubrics") or []:
            if not isinstance(rubric, dict):
                continue
            code = normalize_rubric_key(str(rubric.get("code") or ""))
            value = unwrap_fill_value(rubric)
            if code and value is not None:
                target[code] = value


def unwrap_fill_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("value", "calculated_value", "existing_value"):
        if key in value and value[key] is not None:
            return value[key]
    template = value.get("template")
    if isinstance(template, dict):
        fields = template.get("fields")
        if isinstance(fields, dict):
            for key in ("calculated_value", "existing_value"):
                if fields.get(key) is not None:
                    return fields[key]
    fields = value.get("fields")
    if isinstance(fields, dict):
        for key in ("calculated_value", "existing_value"):
            if fields.get(key) is not None:
                return fields[key]
    return None


def normalize_rubric_key(value: str) -> str | None:
    match = re.fullmatch(r"(?:rubrik[_\s-]*)?(\d{2,3})", value.strip().casefold())
    if match is None:
        return None
    return match.group(1)


def format_fill_value(value: Any) -> str:
    if isinstance(value, bool):
        return "X" if value else ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value).strip()


def fill_pdf(
    *,
    pdf_source: str,
    output_path: Path,
    values_by_code: dict[str, Any],
    font_size: float = 8.5,
    dpi: int = 150,
) -> FillPdfResult:
    if not values_by_code:
        raise ValueError("no fill values were provided")
    require_tool("pdftohtml")
    require_tool("pdftocairo")
    with tempfile.TemporaryDirectory(prefix="taxhelper-fill-") as tmpdir:
        work_dir = Path(tmpdir)
        source_path = materialize_pdf_source(pdf_source, work_dir)
        xml_path = extract_pdf_xml(source_path, work_dir)
        page_sizes = parse_page_sizes(xml_path)
        positions = parse_rubric_positions(xml_path, codes=set(values_by_code))
        page_images = render_pdf_pages(source_path, work_dir, page_sizes=page_sizes, dpi=dpi)
        overlays: dict[int, list[OverlayText]] = {}
        filled_codes: list[str] = []
        missing_codes: list[str] = []
        for code in sorted(values_by_code, key=rubric_sort_key):
            text = format_fill_value(values_by_code[code])
            if not text:
                continue
            position = positions.get(code)
            if position is None:
                missing_codes.append(code)
                continue
            overlays.setdefault(position.page_number, []).append(
                OverlayText(
                    text=text,
                    right_x=VALUE_RIGHT_X,
                    top=position.top,
                    height=position.height,
                    font_size=font_size,
                )
            )
            filled_codes.append(code)
        write_raster_overlay_pdf(page_images, overlays, output_path)
    return FillPdfResult(
        output_path=output_path,
        filled_codes=filled_codes,
        missing_position_codes=missing_codes,
        page_count=len(page_images),
    )


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise ValueError(f"required PDF tool not found on PATH: {name}")


def materialize_pdf_source(pdf_source: str, work_dir: Path) -> Path:
    if pdf_source.startswith(("http://", "https://")):
        target = work_dir / "source.pdf"
        request = urllib.request.Request(pdf_source, headers={"User-Agent": "taxhelper/0.1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            target.write_bytes(response.read())
        return target
    path = Path(pdf_source).expanduser()
    if not path.exists():
        raise ValueError(f"PDF does not exist: {path}")
    return path


def extract_pdf_xml(pdf_path: Path, work_dir: Path) -> Path:
    prefix = work_dir / "layout"
    subprocess.run(
        [
            "pdftohtml",
            "-xml",
            "-hidden",
            "-nodrm",
            "-zoom",
            "1",
            "-noroundcoord",
            str(pdf_path),
            str(prefix),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    xml_path = prefix.with_suffix(".xml")
    if not xml_path.exists():
        raise ValueError("pdftohtml did not produce XML layout output")
    return xml_path


def parse_page_sizes(xml_path: Path) -> dict[int, tuple[float, float]]:
    root = ET.parse(xml_path).getroot()
    sizes: dict[int, tuple[float, float]] = {}
    for page in root.findall("page"):
        page_number = int(page.attrib["number"])
        sizes[page_number] = (float(page.attrib["width"]), float(page.attrib["height"]))
    return sizes


def parse_rubric_positions(xml_path: Path, *, codes: set[str] | None = None) -> dict[str, RubricPosition]:
    root = ET.parse(xml_path).getroot()
    positions: dict[str, RubricPosition] = {}
    for page in root.findall("page"):
        page_number = int(page.attrib["number"])
        page_width = float(page.attrib["width"])
        page_height = float(page.attrib["height"])
        for text in page.findall("text"):
            left = float(text.attrib["left"])
            if left < RUBRIC_COLUMN_MIN_X or left > RUBRIC_COLUMN_MAX_X:
                continue
            content = " ".join("".join(text.itertext()).split())
            match = re.match(r"^(\d{2,3})\b", content)
            if match is None:
                continue
            code = match.group(1)
            if codes is not None and code not in codes:
                continue
            positions.setdefault(
                code,
                RubricPosition(
                    code=code,
                    page_number=page_number,
                    page_width=page_width,
                    page_height=page_height,
                    top=float(text.attrib["top"]),
                    height=float(text.attrib["height"]),
                ),
            )
    return positions


def render_pdf_pages(
    pdf_path: Path,
    work_dir: Path,
    *,
    page_sizes: dict[int, tuple[float, float]],
    dpi: int,
) -> list[PageImage]:
    prefix = work_dir / "page"
    subprocess.run(
        [
            "pdftocairo",
            "-jpeg",
            "-r",
            str(dpi),
            "-jpegopt",
            "quality=95",
            str(pdf_path),
            str(prefix),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    images: list[PageImage] = []
    for path in sorted(work_dir.glob("page-*.jpg"), key=lambda item: page_sort_key(item.name)):
        page_number = page_sort_key(path.name)
        page_width, page_height = page_sizes[page_number]
        image_width, image_height, components = jpeg_size(path)
        images.append(
            PageImage(
                page_number=page_number,
                path=path,
                page_width=page_width,
                page_height=page_height,
                image_width=image_width,
                image_height=image_height,
                components=components,
            )
        )
    if not images:
        raise ValueError("pdftocairo did not render any JPEG pages")
    return images


def jpeg_size(path: Path) -> tuple[int, int, int]:
    data = path.read_bytes()
    index = 2
    while index < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        length = int.from_bytes(data[index : index + 2], "big")
        if 0xC0 <= marker <= 0xC3:
            components = data[index + 7]
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height, components
        index += length
    raise ValueError(f"could not read JPEG dimensions: {path}")


def write_raster_overlay_pdf(
    page_images: list[PageImage],
    overlays: dict[int, list[OverlayText]],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    objects: dict[int, bytes] = {}
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    page_ids: list[int] = []
    next_id = 4
    for page in page_images:
        image_id = next_id
        content_id = next_id + 1
        page_id = next_id + 2
        next_id += 3
        page_ids.append(page_id)
        color_space = {
            1: "/DeviceGray",
            3: "/DeviceRGB",
            4: "/DeviceCMYK",
        }.get(page.components, "/DeviceRGB")
        image_data = page.path.read_bytes()
        objects[image_id] = (
            f"<< /Type /XObject /Subtype /Image /Width {page.image_width} "
            f"/Height {page.image_height} /ColorSpace {color_space} "
            f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(image_data)} >>\n"
            "stream\n"
        ).encode("ascii") + image_data + b"\nendstream"
        content = page_content_stream(page, overlays.get(page.page_number, []))
        objects[content_id] = (
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page.page_width:.3f} "
            f"{page.page_height:.3f}] /Resources << /XObject << /Im0 {image_id} 0 R >> "
            f"/Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")
    write_pdf_objects(objects, output_path)


def page_content_stream(page: PageImage, overlays: list[OverlayText]) -> bytes:
    lines = [
        "q",
        f"{page.page_width:.3f} 0 0 {page.page_height:.3f} 0 0 cm",
        "/Im0 Do",
        "Q",
    ]
    if overlays:
        lines.extend(["BT", "0 0 0 rg"])
        for overlay in overlays:
            text_width = estimate_text_width(overlay.text, overlay.font_size)
            x = max(VALUE_MIN_X, overlay.right_x - text_width)
            y = page.page_height - overlay.top - overlay.height + 1.2
            lines.append(f"/F1 {overlay.font_size:.2f} Tf")
            lines.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm")
            lines.append(f"({pdf_escape(overlay.text)}) Tj")
        lines.append("ET")
    return ("\n".join(lines) + "\n").encode("latin-1", errors="replace")


def estimate_text_width(value: str, font_size: float) -> float:
    width = 0.0
    for char in value:
        width += 0.28 if char in ".,:;!|'" else 0.56
    return width * font_size


def pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_pdf_objects(objects: dict[int, bytes], output_path: Path) -> None:
    max_id = max(objects)
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max_id + 1)
    for obj_id in range(1, max_id + 1):
        body = objects[obj_id]
        offsets[obj_id] = len(output)
        output.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {max_id + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for obj_id in range(1, max_id + 1):
        output.extend(f"{offsets[obj_id]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    output_path.write_bytes(output)


def page_sort_key(name: str) -> int:
    match = re.search(r"-(\d+)\.jpe?g$", name)
    if match is None:
        return 0
    return int(match.group(1))


def rubric_sort_key(code: str) -> tuple[int, str]:
    return (int(code) if code.isdigit() else 9999, code)
