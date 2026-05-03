from __future__ import annotations

import re
import urllib.request
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
from typing import Any


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    @property
    def title(self) -> str:
        title = " ".join(part.strip() for part in self._title_parts if part.strip())
        return _clean_spaces(title)

    def text(self) -> str:
        text = "\n".join(part.strip() for part in self._parts if part.strip())
        lines = [_clean_spaces(line) for line in text.splitlines()]
        return "\n".join(line for line in lines if line)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "br"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if self._skip_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "td", "th"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        clean = _clean_spaces(data)
        if not clean:
            return
        if self._in_title:
            self._title_parts.append(clean)
        self._parts.append(clean)


def fetch_source_document(
    url: str,
    *,
    publisher: str = "Skattestyrelsen",
    source_type: str = "guidance",
    effective_year: int | None = None,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tax-helper/0.1 (+local research tool)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
    encoding = _encoding_from_content_type(content_type) or "utf-8"
    html = raw.decode(encoding, errors="replace")
    parser = HTMLTextExtractor()
    parser.feed(html)
    title = parser.title or url
    body = parser.text()
    if not body:
        raise ValueError(f"no readable text found at {url}")
    return {
        "url": url,
        "title": title,
        "publisher": publisher,
        "retrieved_at": datetime.now(UTC).date().isoformat(),
        "source_type": source_type,
        "effective_year": effective_year,
        "body": body,
    }


class RubricGuidePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self._parts: list[str] = []
        self._link_parts: list[str] | None = None
        self._link_href = ""
        self._skip_depth = 0
        self._content_depth = 0

    @property
    def title(self) -> str:
        return self.meta.get("name") or self.meta.get("DSstat.pageName") or ""

    @property
    def rubric_code(self) -> str | None:
        match = re.search(r"\bRubrik\s+(\d{2,3})\b", self.title)
        return match.group(1) if match else None

    @property
    def oid(self) -> str:
        return self.meta.get("DSstat.oid", "")

    @property
    def version(self) -> str:
        return self.meta.get("DSstat.oidVersion", "")

    @property
    def updated_at(self) -> str:
        pub_date = self.meta.get("pubDate", "")
        if len(pub_date) >= 8 and pub_date[:8].isdigit():
            return f"{pub_date[:4]}-{pub_date[4:6]}-{pub_date[6:8]}"
        return datetime.now(UTC).date().isoformat()

    def body(self) -> str:
        lines = [_clean_spaces(part) for part in self._parts]
        return "\n".join(line for line in lines if line)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            name = attr.get("name")
            content = attr.get("content")
            if name and content:
                self.meta[name] = content
            return
        if tag in {"script", "style", "svg", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "div" and "MPtext" in attr.get("class", ""):
            self._content_depth += 1
        elif self._content_depth and tag == "div":
            self._content_depth += 1
        if self._content_depth and tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "br"}:
            self._parts.append("\n")
        if self._content_depth and tag == "a":
            self._link_href = attr.get("href", "")
            self._link_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if self._content_depth and tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}:
            self._parts.append("\n")
        if self._content_depth and tag == "a" and self._link_parts is not None:
            text = _clean_spaces(" ".join(self._link_parts))
            if self._link_href and text:
                self.links.append((self._link_href, text))
            self._link_parts = None
            self._link_href = ""
        if self._content_depth and tag == "div":
            self._content_depth = max(0, self._content_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not self._content_depth:
            return
        clean = _clean_spaces(data)
        if not clean:
            return
        self._parts.append(clean)
        if self._link_parts is not None:
            self._link_parts.append(clean)


def fetch_html(url: str, *, timeout_seconds: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "tax-helper/0.1 (+local research tool)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
    encoding = _encoding_from_content_type(content_type) or "utf-8"
    return raw.decode(encoding, errors="replace")


def parse_rubric_guide_page(html: str, base_url: str) -> dict[str, Any] | None:
    parser = RubricGuidePageParser()
    parser.feed(html)
    code = parser.rubric_code
    body = clean_rubric_guide_body(parser.body())
    if not code or not body:
        return None
    links = [
        {"url": urljoin(base_url, href), "label": label}
        for href, label in parser.links
        if not is_page_chrome_link(href, label)
    ]
    if links:
        body = f"{body}\n\nLinks:\n" + "\n".join(f"- {link['label']}: {link['url']}" for link in links)
    return {
        "code": code,
        "title": parser.title or f"Rubrik {code}",
        "body": body,
        "oid": parser.oid,
        "version": parser.version,
        "updated_at": parser.updated_at,
    }


def clean_rubric_guide_body(body: str) -> str:
    markers = (
        "\nDokumentet gælder for ",
        "\nFik du svar på dine spørgsmål?",
        "\nHar du forslag til, hvordan vi kan forbedre indholdet?",
    )
    cleaned = body
    for marker in markers:
        index = cleaned.find(marker)
        if index != -1:
            cleaned = cleaned[:index]
    return cleaned.strip()


def is_page_chrome_link(href: str, label: str) -> bool:
    normalized = f"{href} {label}".lower()
    return "kontaktformular" in normalized or "skat.dk/kontakt" in normalized


def _encoding_from_content_type(content_type: str) -> str | None:
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    return match.group(1).strip('"') if match else None


def _clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
