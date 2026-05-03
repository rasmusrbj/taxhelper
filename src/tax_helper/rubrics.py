from __future__ import annotations

import re
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from tax_helper.importer import fetch_html, parse_rubric_guide_page


DEFAULT_RUBRIC_PDF_URL = "https://skat.dk/media/ftiduwhm/04003_januar2026-t.pdf"
DEFAULT_RUBRIC_GUIDE_URL = "https://info.skat.dk/data.aspx?oid=2236973&chk=221074"


@dataclass(frozen=True)
class Rubric:
    tax_year: int
    code: str
    field_no: str
    form_code: str
    form_title: str
    section: str
    label: str
    locked_note: str
    source_url: str
    detail_url: str = ""
    detail_title: str = ""
    detail_body: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RubricGuidePage:
    code: str
    title: str
    url: str
    body: str
    oid: str
    version: str
    updated_at: str


@dataclass(frozen=True)
class ScrapeResult:
    pdf_source_text: str
    rubrics: list[Rubric]
    guide_pages: list[RubricGuidePage]
    scanned_pages: int


def scrape_rubrics(
    *,
    pdf_url: str = DEFAULT_RUBRIC_PDF_URL,
    pdf_source_url: str | None = None,
    guide_seed_url: str = DEFAULT_RUBRIC_GUIDE_URL,
    tax_year: int = 2025,
    form_code: str = "04.003",
    form_title: str = "Oplysningsskemaet",
    guide_start_oid: int | None = None,
    guide_end_oid: int | None = None,
    with_guidance: bool = True,
    delay_seconds: float = 0.05,
    guide_timeout_seconds: int = 4,
) -> ScrapeResult:
    pdf_bytes = fetch_bytes(pdf_url)
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = Path(tmpdir) / "rubrics.pdf"
        pdf_path.write_bytes(pdf_bytes)
        pdf_text = pdftotext(pdf_path)
    rubrics = parse_pdf_rubrics(
        pdf_text,
        tax_year=tax_year,
        form_code=form_code,
        form_title=form_title,
        source_url=pdf_source_url or pdf_url,
    )
    guide_pages: list[RubricGuidePage] = []
    scanned_pages = 0
    if with_guidance:
        start_oid, end_oid, version = resolve_guide_scan_range(
            guide_seed_url,
            guide_start_oid=guide_start_oid,
            guide_end_oid=guide_end_oid,
            timeout_seconds=guide_timeout_seconds,
        )
        for oid in range(start_oid, end_oid + 1):
            url = build_info_url(oid, version)
            scanned_pages += 1
            try:
                html = fetch_html(url, timeout_seconds=guide_timeout_seconds)
            except OSError:
                continue
            parsed = parse_rubric_guide_page(html, url)
            if parsed is None:
                continue
            guide_pages.append(
                RubricGuidePage(
                    code=parsed["code"],
                    title=parsed["title"],
                    url=url,
                    body=parsed["body"],
                    oid=parsed["oid"],
                    version=parsed["version"],
                    updated_at=parsed["updated_at"],
                )
            )
            if delay_seconds > 0:
                time.sleep(delay_seconds)
    return ScrapeResult(
        pdf_source_text=pdf_text,
        rubrics=rubrics,
        guide_pages=dedupe_guide_pages(guide_pages),
        scanned_pages=scanned_pages,
    )


def fetch_bytes(url: str, *, timeout_seconds: int = 30) -> bytes:
    if not url.startswith(("http://", "https://")):
        return Path(url).expanduser().read_bytes()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tax-helper/0.1 (+local research tool)",
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.5",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def pdftotext(pdf_path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-raw", str(pdf_path), "-"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "pdftotext failed. Install poppler-utils/poppler, or pass rubrics as JSON."
        )
    return result.stdout


def parse_pdf_rubrics(
    text: str,
    *,
    tax_year: int,
    form_code: str,
    form_title: str,
    source_url: str,
) -> list[Rubric]:
    rubrics: list[Rubric] = []
    section = ""
    label_parts: list[str] = []
    pending: tuple[str, str, str] | None = None
    last_index: int | None = None

    for raw_line in text.splitlines():
        line = clean_pdf_line(raw_line)
        if not line:
            continue
        section_match = re.match(r"^(?P<section>.+?)\s+Rubrik(?:\s+Beløb.*)?\s+Felt nr\.$", line)
        if section_match:
            pending = None
            label_parts = []
            section = section_match.group("section").strip()
            continue
        if is_footer_or_intro(line):
            continue

        if pending is not None:
            if is_numeric_code(line):
                row = build_rubric(
                    pending[0],
                    pending[1],
                    line,
                    pending[2],
                    section,
                    tax_year,
                    form_code,
                    form_title,
                    source_url,
                )
                rubrics.append(row)
                last_index = len(rubrics) - 1
                pending = None
                label_parts = []
                continue
            pending = (pending[0], pending[1], f"{pending[2]} {line}".strip())
            continue

        parsed_buffered = parse_buffered_row(line)
        if parsed_buffered is not None and label_parts:
            code, field_no, note = parsed_buffered
            rubrics.append(
                build_rubric(
                    code,
                    merge_label_parts(label_parts, ""),
                    field_no,
                    note,
                    section,
                    tax_year,
                    form_code,
                    form_title,
                    source_url,
                )
            )
            last_index = len(rubrics) - 1
            label_parts = []
            continue

        parsed = parse_complete_row(line)
        if parsed is not None:
            label, code, field_no, note = parsed
            label = merge_label_parts(label_parts, label)
            rubrics.append(
                build_rubric(
                    code,
                    label,
                    field_no,
                    note,
                    section,
                    tax_year,
                    form_code,
                    form_title,
                    source_url,
                )
            )
            last_index = len(rubrics) - 1
            label_parts = []
            continue

        parsed_partial = parse_partial_row(line)
        if parsed_partial is not None:
            label, code, note = parsed_partial
            pending = (code, merge_label_parts(label_parts, label), note)
            label_parts = []
            continue

        if is_numeric_code(line) and label_parts:
            pending = (line, merge_label_parts(label_parts, ""), "")
            label_parts = []
            continue

        if is_numeric_code(line) and not label_parts and last_index is not None:
            previous = rubrics[last_index]
            rubrics[last_index] = Rubric(
                tax_year=previous.tax_year,
                code=previous.code,
                field_no=f"{previous.field_no},{line}",
                form_code=previous.form_code,
                form_title=previous.form_title,
                section=previous.section,
                label=previous.label,
                locked_note=previous.locked_note,
                source_url=previous.source_url,
                detail_url=previous.detail_url,
                detail_title=previous.detail_title,
                detail_body=previous.detail_body,
                updated_at=previous.updated_at,
            )
            continue

        if section:
            label_parts.append(line)

    return dedupe_rubrics(rubrics)


def resolve_guide_scan_range(
    guide_seed_url: str,
    *,
    guide_start_oid: int | None,
    guide_end_oid: int | None,
    timeout_seconds: int = 4,
) -> tuple[int, int, str]:
    parsed_url = urlparse(guide_seed_url)
    params = parse_qs(parsed_url.query)
    seed_oid = int(params.get("oid", ["2236973"])[0])
    version = params.get("chk", params.get("vid", [""]))[0]
    start_oid = guide_start_oid if guide_start_oid is not None else max(1, seed_oid - 10)
    if guide_end_oid is not None:
        return start_oid, guide_end_oid, version

    root_oid = start_oid
    try:
        root_html = fetch_html(build_info_url(root_oid, version), timeout_seconds=timeout_seconds)
    except OSError:
        return start_oid, seed_oid + 130, version
    children_match = re.search(r'name="children"\s+id="children"\s+value="(\d+)"', root_html)
    children = int(children_match.group(1)) if children_match else 130
    return start_oid, root_oid + children + 15, version


def build_info_url(oid: int, version: str) -> str:
    params = {"oid": str(oid)}
    if version:
        params["chk"] = version
    return f"https://info.skat.dk/data.aspx?{urlencode(params)}"


def dedupe_guide_pages(pages: list[RubricGuidePage]) -> list[RubricGuidePage]:
    by_code: dict[str, RubricGuidePage] = {}
    for page in pages:
        existing = by_code.get(page.code)
        if existing is None or len(page.body) > len(existing.body):
            by_code[page.code] = page
    return sorted(by_code.values(), key=lambda page: int(page.code))


def dedupe_rubrics(rubrics: list[Rubric]) -> list[Rubric]:
    by_key: dict[tuple[int, str], Rubric] = {}
    for rubric in rubrics:
        by_key[(rubric.tax_year, rubric.code)] = rubric
    return sorted(by_key.values(), key=lambda rubric: (rubric.tax_year, int(rubric.code)))


def build_rubric(
    code: str,
    label: str,
    field_no: str,
    note: str,
    section: str,
    tax_year: int,
    form_code: str,
    form_title: str,
    source_url: str,
) -> Rubric:
    return Rubric(
        tax_year=tax_year,
        code=code.strip(),
        field_no=field_no.strip(),
        form_code=form_code,
        form_title=form_title,
        section=section,
        label=clean_pdf_line(label),
        locked_note=normalize_note(note),
        source_url=source_url,
    )


def parse_complete_row(line: str) -> tuple[str, str, str, str] | None:
    field_match = re.match(r"^(?P<head>.+?)\s+(?P<field>\d{2,3})$", line)
    if not field_match:
        return None
    head = field_match.group("head")
    match = re.match(
        r"^(?P<label>.+?)\s+(?P<code>\d{2,3})(?:\s+(?P<note>Felt .+|Anvend .+|Hvis .+|Ja .+|Nej .+))?$",
        head,
    )
    if not match:
        return None
    label = match.group("label").strip()
    code = match.group("code")
    field_no = field_match.group("field")
    note = (match.group("note") or "").strip()
    if not looks_like_label(label):
        return None
    return label, code, field_no, note


def parse_buffered_row(line: str) -> tuple[str, str, str] | None:
    match = re.match(
        r"^(?P<code>\d{2,3})(?:\s+(?P<note>Felt .+|Anvend .+|Hvis .+|Ja .+|Nej .+))?\s+(?P<field>\d{2,3})$",
        line,
    )
    if not match:
        return None
    return match.group("code"), match.group("field"), (match.group("note") or "")


def parse_partial_row(line: str) -> tuple[str, str, str] | None:
    match = re.match(
        r"^(?P<label>.+?)\s+(?P<code>\d{2,3})\s+(?P<note>Felt .+|Anvend .+|Hvis .+|Ja .+|Nej .+)$",
        line,
    )
    if not match:
        return None
    label = match.group("label").strip()
    if not looks_like_label(label):
        return None
    return label, match.group("code"), match.group("note").strip()


def merge_label_parts(parts: list[str], inline_label: str) -> str:
    return clean_pdf_line(" ".join([*parts, inline_label]))


def normalize_note(note: str) -> str:
    note = clean_pdf_line(note)
    note = note.replace("Anvendblanket", "Anvend blanket ")
    note = note.replace("blanket04.", "blanket 04.")
    note = re.sub(r"blanket\s+04\.\s*", "blanket 04.", note)
    note = re.sub(r"\s*/\s*", "/", note)
    return note


def clean_pdf_line(line: str) -> str:
    line = line.replace("\u00ad", "")
    line = line.replace("\x0c", "")
    return re.sub(r"\s+", " ", line).strip()


def is_footer_or_intro(line: str) -> bool:
    ignored_prefixes = (
        "Side ",
        "04.003 ",
        "Dato ",
        "E-mail",
        "Det er dit ansvar",
        "Oplysningsskemaet",
        "Navn og adresse",
        "Personfradrag",
        "Indregnet restskat",
        "Skattekommune",
        "Skatteprocenter",
        "Telefonnummer",
        "Mail via",
        "TastSelv Internet",
        "Vejledning",
        "Dette oplysningsskema",
        "Udfyld oplysningsskemaet",
        "Husk fristen",
        "Brug TastSelv",
        "Mere information",
        "Du kan få mere information",
        "Du er også velkommen",
    )
    return line == "2025" or line.startswith(ignored_prefixes)


def is_numeric_code(line: str) -> bool:
    return bool(re.fullmatch(r"\d{2,3}", line))


def looks_like_label(label: str) -> bool:
    if len(label) < 3:
        return False
    return any(character.isalpha() for character in label)
