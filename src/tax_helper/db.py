from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from tax_helper.schema import SCHEMA_SQL
from tax_helper.rubrics import Rubric, RubricGuidePage
from tax_helper.tags import (
    TAG_DEFINITIONS,
    TagAssignment,
    canonical_tag,
    matching_tags,
    tag_rubric,
    tag_rule,
)


DEFAULT_DB_NAME = "tax_rules.sqlite"


@dataclass(frozen=True)
class SearchResult:
    kind: str
    identifier: str
    title: str
    category: str
    tax_year: int | None
    snippet: str
    source_url: str
    tags: str = ""


@dataclass(frozen=True)
class RubricScrapeCounts:
    pdf_rubrics: int
    guide_pages: int
    scanned_pages: int


def default_db_path() -> Path:
    env_path = os.environ.get("TAX_HELPER_DB")
    if env_path:
        return Path(env_path).expanduser()
    cwd_db = Path.cwd() / DEFAULT_DB_NAME
    if cwd_db.exists():
        return cwd_db
    project_db = project_root() / DEFAULT_DB_NAME
    if project_db.exists():
        return project_db
    return cwd_db


def default_data_path(filename: str) -> Path:
    cwd_data = Path.cwd() / "data" / filename
    if cwd_data.exists():
        return cwd_data
    project_data = project_root() / "data" / filename
    if project_data.exists():
        return project_data
    try:
        resource = files("tax_helper.data").joinpath(filename)
    except ModuleNotFoundError:
        return cwd_data
    if resource.is_file():
        return Path(str(resource))
    return cwd_data


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_seed_path() -> Path:
    return default_data_path("seed_rules.json")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise ValueError(f"database does not exist: {db_path}")
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    install_tag_definitions(conn)
    conn.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        ("schema_version", "1"),
    )
    conn.commit()


def install_tag_definitions(conn: sqlite3.Connection) -> None:
    for definition in TAG_DEFINITIONS:
        conn.execute(
            """
            INSERT INTO tags(tag, label, description)
            VALUES (?, ?, ?)
            ON CONFLICT(tag) DO UPDATE SET
                label = excluded.label,
                description = excluded.description
            """,
            (definition.tag, definition.label, definition.description),
        )


def seed_db(conn: sqlite3.Connection, seed_path: Path) -> tuple[int, int]:
    init_db(conn)
    data = _load_json(seed_path)
    source_count = 0
    rule_count = 0
    with conn:
        for source in data.get("source_documents", []):
            upsert_source(conn, source)
            source_count += 1
        for rule in data.get("rules", []):
            upsert_rule(conn, rule)
            rule_count += 1
    return source_count, rule_count


def upsert_source(conn: sqlite3.Connection, source: dict[str, Any]) -> int:
    body = _required_str(source, "body")
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
    fields = {
        "url": _required_str(source, "url"),
        "title": _required_str(source, "title"),
        "publisher": str(source.get("publisher") or ""),
        "retrieved_at": _required_str(source, "retrieved_at"),
        "source_type": str(source.get("source_type") or "guidance"),
        "effective_year": source.get("effective_year"),
        "body": body,
        "checksum": checksum,
    }
    conn.execute(
        """
        INSERT INTO sources(
            url, title, publisher, retrieved_at, source_type, effective_year, body, checksum
        )
        VALUES (
            :url, :title, :publisher, :retrieved_at, :source_type, :effective_year,
            :body, :checksum
        )
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            publisher = excluded.publisher,
            retrieved_at = excluded.retrieved_at,
            source_type = excluded.source_type,
            effective_year = excluded.effective_year,
            body = excluded.body,
            checksum = excluded.checksum
        """,
        fields,
    )
    source_id = int(conn.execute("SELECT id FROM sources WHERE url = ?", (fields["url"],)).fetchone()["id"])
    conn.execute("DELETE FROM source_fts WHERE source_id = ?", (source_id,))
    conn.execute(
        "INSERT INTO source_fts(source_id, title, url, body) VALUES (?, ?, ?, ?)",
        (source_id, fields["title"], fields["url"], fields["body"]),
    )
    return source_id


def upsert_rule(conn: sqlite3.Connection, rule: dict[str, Any]) -> None:
    rule_id = _required_str(rule, "id")
    conn.execute(
        """
        INSERT INTO rules(
            id, title, category, tax_year, summary, applies_to, source_url,
            source_title, source_publisher, updated_at, caveats
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            category = excluded.category,
            tax_year = excluded.tax_year,
            summary = excluded.summary,
            applies_to = excluded.applies_to,
            source_url = excluded.source_url,
            source_title = excluded.source_title,
            source_publisher = excluded.source_publisher,
            updated_at = excluded.updated_at,
            caveats = excluded.caveats
        """,
        (
            rule_id,
            _required_str(rule, "title"),
            _required_str(rule, "category"),
            rule.get("tax_year"),
            _required_str(rule, "summary"),
            str(rule.get("applies_to") or ""),
            _required_str(rule, "source_url"),
            str(rule.get("source_title") or ""),
            str(rule.get("source_publisher") or ""),
            _required_str(rule, "updated_at"),
            str(rule.get("caveats") or ""),
        ),
    )
    conn.execute("DELETE FROM amounts WHERE rule_id = ?", (rule_id,))
    for amount in rule.get("amounts", []):
        conn.execute(
            """
            INSERT INTO amounts(rule_id, tax_year, label, value, unit, threshold_note, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                int(amount["tax_year"]),
                _required_str(amount, "label"),
                float(amount["value"]),
                _required_str(amount, "unit"),
                str(amount.get("threshold_note") or ""),
                str(amount.get("source_url") or rule["source_url"]),
            ),
        )
    for field in rule.get("return_fields", []):
        conn.execute(
            """
            INSERT INTO return_fields(code, kind, tax_form, label, category, rule_id, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                kind = excluded.kind,
                tax_form = excluded.tax_form,
                label = excluded.label,
                category = excluded.category,
                rule_id = excluded.rule_id,
                description = excluded.description
            """,
            (
                _required_str(field, "code"),
                _required_str(field, "kind"),
                _required_str(field, "tax_form"),
                _required_str(field, "label"),
                _required_str(field, "category"),
                rule_id,
                str(field.get("description") or ""),
            ),
        )
    conn.execute("DELETE FROM rule_fts WHERE rule_id = ?", (rule_id,))
    conn.execute(
        """
        INSERT INTO rule_fts(rule_id, title, category, summary, applies_to, caveats)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            rule_id,
            rule["title"],
            rule["category"],
            rule["summary"],
            str(rule.get("applies_to") or ""),
            str(rule.get("caveats") or ""),
        ),
    )
    refresh_rule_tags(conn, rule_id)


def rebuild_fts(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM rule_fts")
        conn.execute("DELETE FROM source_fts")
        conn.execute(
            """
            INSERT INTO rule_fts(rule_id, title, category, summary, applies_to, caveats)
            SELECT id, title, category, summary, applies_to, caveats FROM rules
            """
        )
        conn.execute(
            """
            INSERT INTO source_fts(source_id, title, url, body)
            SELECT id, title, url, body FROM sources
            """
        )
        conn.execute("DELETE FROM rubric_fts")
        conn.execute(
            """
            INSERT INTO rubric_fts(
                tax_year, code, field_no, section, label, locked_note, detail_title, detail_body
            )
            SELECT
                tax_year, code, field_no, section, label, locked_note, detail_title, detail_body
            FROM rubrics
            """
        )
        rebuild_tags(conn)


def rebuild_tags(conn: sqlite3.Connection) -> None:
    install_tag_definitions(conn)
    conn.execute("DELETE FROM entity_tags")
    for row in conn.execute("SELECT id FROM rules").fetchall():
        refresh_rule_tags(conn, str(row["id"]))
    for row in conn.execute("SELECT tax_year, code FROM rubrics").fetchall():
        refresh_rubric_tags(conn, int(row["tax_year"]), str(row["code"]))


def upsert_rubrics(
    conn: sqlite3.Connection,
    rubrics: list[Rubric],
    guide_pages: list[RubricGuidePage],
) -> RubricScrapeCounts:
    with conn:
        for rubric in rubrics:
            upsert_rubric(conn, rubric)
        for page in guide_pages:
            source_id = upsert_source(
                conn,
                {
                    "url": page.url,
                    "title": page.title,
                    "publisher": "Skattestyrelsen",
                    "retrieved_at": page.updated_at,
                    "source_type": "rubric_guidance",
                    "effective_year": rubrics[0].tax_year if rubrics else None,
                    "body": page.body,
                },
            )
            del source_id
            attach_rubric_guide(conn, page, tax_year=rubrics[0].tax_year if rubrics else 2025)
    return RubricScrapeCounts(
        pdf_rubrics=len(rubrics),
        guide_pages=len(guide_pages),
        scanned_pages=0,
    )


def upsert_rubric(conn: sqlite3.Connection, rubric: Rubric) -> None:
    conn.execute(
        """
        INSERT INTO rubrics(
            tax_year, code, field_no, form_code, form_title, section, label, locked_note,
            source_url, detail_url, detail_title, detail_body, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tax_year, code) DO UPDATE SET
            field_no = excluded.field_no,
            form_code = excluded.form_code,
            form_title = excluded.form_title,
            section = excluded.section,
            label = excluded.label,
            locked_note = excluded.locked_note,
            source_url = excluded.source_url
        """,
        (
            rubric.tax_year,
            rubric.code,
            rubric.field_no,
            rubric.form_code,
            rubric.form_title,
            rubric.section,
            rubric.label,
            rubric.locked_note,
            rubric.source_url,
            rubric.detail_url,
            rubric.detail_title,
            rubric.detail_body,
            rubric.updated_at,
        ),
    )
    refresh_rubric_fts(conn, rubric.tax_year, rubric.code)
    refresh_rubric_tags(conn, rubric.tax_year, rubric.code)


def attach_rubric_guide(conn: sqlite3.Connection, page: RubricGuidePage, *, tax_year: int) -> None:
    existing = conn.execute(
        "SELECT code FROM rubrics WHERE tax_year = ? AND code = ?",
        (tax_year, page.code),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO rubrics(
                tax_year, code, field_no, form_code, form_title, section, label, locked_note,
                source_url, detail_url, detail_title, detail_body, updated_at
            )
            VALUES (?, ?, '', '', '', 'TastSelv guidance', ?, '', '', ?, ?, ?, ?)
            """,
            (tax_year, page.code, page.title, page.url, page.title, page.body, page.updated_at),
        )
    else:
        conn.execute(
            """
            UPDATE rubrics
            SET detail_url = ?, detail_title = ?, detail_body = ?, updated_at = ?
            WHERE tax_year = ? AND code = ?
            """,
            (page.url, page.title, page.body, page.updated_at, tax_year, page.code),
        )
    refresh_rubric_fts(conn, tax_year, page.code)
    refresh_rubric_tags(conn, tax_year, page.code)


def refresh_rubric_fts(conn: sqlite3.Connection, tax_year: int, code: str) -> None:
    conn.execute("DELETE FROM rubric_fts WHERE tax_year = ? AND code = ?", (tax_year, code))
    conn.execute(
        """
        INSERT INTO rubric_fts(
            tax_year, code, field_no, section, label, locked_note, detail_title, detail_body
        )
        SELECT tax_year, code, field_no, section, label, locked_note, detail_title, detail_body
        FROM rubrics
        WHERE tax_year = ? AND code = ?
        """,
        (tax_year, code),
    )


def refresh_rule_tags(conn: sqlite3.Connection, rule_id: str) -> None:
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        return
    assignments = tag_rule(
        title=str(row["title"]),
        category=str(row["category"]),
        summary=str(row["summary"]),
        applies_to=str(row["applies_to"]),
        caveats=str(row["caveats"]),
    )
    replace_entity_tags(
        conn,
        entity_kind="rule",
        entity_id=rule_id,
        tax_year=row["tax_year"],
        assignments=assignments,
    )


def refresh_rubric_tags(conn: sqlite3.Connection, tax_year: int, code: str) -> None:
    row = conn.execute(
        "SELECT * FROM rubrics WHERE tax_year = ? AND code = ?",
        (tax_year, code),
    ).fetchone()
    if row is None:
        return
    assignments = tag_rubric(
        code=str(row["code"]),
        section=str(row["section"]),
        label=str(row["label"]),
        locked_note=str(row["locked_note"]),
        detail_title=str(row["detail_title"]),
        detail_body=str(row["detail_body"]),
    )
    replace_entity_tags(
        conn,
        entity_kind="rubric",
        entity_id=code,
        tax_year=tax_year,
        assignments=assignments,
    )


def replace_entity_tags(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    entity_id: str,
    tax_year: int | None,
    assignments: list[TagAssignment],
) -> None:
    entity_key = entity_tag_key(entity_kind, entity_id, tax_year)
    conn.execute("DELETE FROM entity_tags WHERE entity_key = ?", (entity_key,))
    for assignment in assignments:
        conn.execute(
            """
            INSERT OR IGNORE INTO entity_tags(
                entity_key, entity_kind, entity_id, tax_year, tag, reason
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entity_key,
                entity_kind,
                entity_id,
                tax_year,
                assignment.tag,
                assignment.reason,
            ),
        )


def entity_tag_key(entity_kind: str, entity_id: str, tax_year: int | None) -> str:
    if tax_year is None:
        return f"{entity_kind}:none:{entity_id}"
    return f"{entity_kind}:{tax_year}:{entity_id}"


def search_rules(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    year: int | None = None,
    tag: str | None = None,
) -> list[SearchResult]:
    fts_query = to_fts_query(query)
    params: list[Any] = [fts_query]
    filters: list[str] = []
    if year is not None:
        filters.append("(r.tax_year = ? OR r.tax_year IS NULL)")
        params.append(year)
    if tag is not None:
        filters.append(
            """
            EXISTS (
                SELECT 1
                FROM entity_tags et_filter
                WHERE et_filter.entity_kind = 'rule'
                  AND et_filter.entity_id = r.id
                  AND et_filter.tag = ?
            )
            """
        )
        params.append(canonical_tag(tag))
    where_extra = f"AND {' AND '.join(filters)}" if filters else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            'rule' AS kind,
            r.id AS identifier,
            r.title,
            r.category,
            r.tax_year,
            snippet(rule_fts, 3, '[', ']', ' ... ', 18) AS snippet,
            r.source_url,
            COALESCE((
                SELECT group_concat(et.tag, ', ')
                FROM entity_tags et
                WHERE et.entity_kind = 'rule'
                  AND et.entity_id = r.id
            ), '') AS tags,
            bm25(rule_fts) AS rank
        FROM rule_fts
        JOIN rules r ON r.id = rule_fts.rule_id
        WHERE rule_fts MATCH ?
        {where_extra}
        ORDER BY rank
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_row_to_search_result(row) for row in rows]


def search_sources(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    year: int | None = None,
) -> list[SearchResult]:
    fts_query = to_fts_query(query)
    params: list[Any] = [fts_query]
    year_filter = ""
    if year is not None:
        year_filter = "AND (s.effective_year = ? OR s.effective_year IS NULL)"
        params.append(year)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            'source' AS kind,
            CAST(s.id AS TEXT) AS identifier,
            s.title,
            s.source_type AS category,
            s.effective_year AS tax_year,
            snippet(source_fts, 3, '[', ']', ' ... ', 18) AS snippet,
            s.url AS source_url,
            bm25(source_fts) AS rank
        FROM source_fts
        JOIN sources s ON s.id = source_fts.source_id
        WHERE source_fts MATCH ?
        {year_filter}
        ORDER BY rank
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_row_to_search_result(row) for row in rows]


def search_rubrics(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    year: int | None = None,
    tag: str | None = None,
) -> list[SearchResult]:
    fts_query = to_fts_query(query)
    params: list[Any] = [fts_query]
    filters: list[str] = []
    if year is not None:
        filters.append("r.tax_year = ?")
        params.append(year)
    if tag is not None:
        filters.append(
            """
            EXISTS (
                SELECT 1
                FROM entity_tags et_filter
                WHERE et_filter.entity_kind = 'rubric'
                  AND et_filter.entity_id = r.code
                  AND et_filter.tax_year = r.tax_year
                  AND et_filter.tag = ?
            )
            """
        )
        params.append(canonical_tag(tag))
    where_extra = f"AND {' AND '.join(filters)}" if filters else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT
            'rubric' AS kind,
            r.code AS identifier,
            'Rubrik ' || r.code || ': ' || r.label AS title,
            r.section AS category,
            r.tax_year,
            snippet(rubric_fts, 4, '[', ']', ' ... ', 18) AS snippet,
            CASE WHEN r.detail_url != '' THEN r.detail_url ELSE r.source_url END AS source_url,
            COALESCE((
                SELECT group_concat(et.tag, ', ')
                FROM entity_tags et
                WHERE et.entity_kind = 'rubric'
                  AND et.entity_id = r.code
                  AND et.tax_year = r.tax_year
            ), '') AS tags,
            bm25(rubric_fts) AS rank
        FROM rubric_fts
        JOIN rubrics r
          ON r.tax_year = rubric_fts.tax_year
         AND r.code = rubric_fts.code
        WHERE rubric_fts MATCH ?
        {where_extra}
        ORDER BY rank
        LIMIT ?
        """,
        params,
    ).fetchall()
    results = [_row_to_search_result(row) for row in rows]
    if tag is None:
        tag_results = search_rubrics_by_matching_tags(conn, query, limit=limit, year=year)
        if tag_results:
            results = merge_search_results(tag_results, results, limit=limit)
    return results


def search_rubrics_by_matching_tags(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    year: int | None = None,
) -> list[SearchResult]:
    tags = matching_tags(query)
    if not tags:
        return []
    params: list[Any] = [*tags]
    year_filter = ""
    if year is not None:
        year_filter = "AND r.tax_year = ?"
        params.append(year)
    params.append(limit)
    placeholders = ",".join("?" for _ in tags)
    rows = conn.execute(
        f"""
        SELECT
            'rubric' AS kind,
            r.code AS identifier,
            'Rubrik ' || r.code || ': ' || r.label AS title,
            r.section AS category,
            r.tax_year,
            'Tags: ' || group_concat(DISTINCT et.tag) AS snippet,
            CASE WHEN r.detail_url != '' THEN r.detail_url ELSE r.source_url END AS source_url,
            COALESCE((
                SELECT group_concat(et_all.tag, ', ')
                FROM entity_tags et_all
                WHERE et_all.entity_kind = 'rubric'
                  AND et_all.entity_id = r.code
                  AND et_all.tax_year = r.tax_year
            ), '') AS tags
        FROM rubrics r
        JOIN entity_tags et
          ON et.entity_kind = 'rubric'
         AND et.entity_id = r.code
         AND et.tax_year = r.tax_year
        WHERE et.tag IN ({placeholders})
        {year_filter}
        GROUP BY r.tax_year, r.code
        ORDER BY CAST(r.code AS INTEGER), r.code
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_row_to_search_result(row) for row in rows]


def merge_search_results(
    primary: list[SearchResult],
    secondary: list[SearchResult],
    *,
    limit: int,
) -> list[SearchResult]:
    seen = {(result.kind, result.tax_year, result.identifier) for result in primary}
    merged = [*primary]
    for result in secondary:
        key = (result.kind, result.tax_year, result.identifier)
        if key in seen:
            continue
        merged.append(result)
        seen.add(key)
        if len(merged) >= limit:
            break
    return merged[:limit]


def get_rubric(conn: sqlite3.Connection, code: str, *, year: int | None = None) -> sqlite3.Row | None:
    if year is not None:
        return conn.execute(
            "SELECT * FROM rubrics WHERE tax_year = ? AND code = ?",
            (year, code),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM rubrics WHERE code = ? ORDER BY tax_year DESC LIMIT 1",
        (code,),
    ).fetchone()


def list_rubrics(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    year: int | None = None,
    tag: str | None = None,
) -> list[sqlite3.Row]:
    filters: list[str] = []
    params: list[Any] = []
    if query:
        if query.isdigit():
            filters.append("(code = ? OR field_no = ?)")
            params.extend([query, query])
        else:
            like = f"%{query}%"
            filters.append(
                "(code LIKE ? OR field_no LIKE ? OR section LIKE ? OR label LIKE ? "
                "OR locked_note LIKE ? OR detail_title LIKE ? OR detail_body LIKE ?)"
            )
            params.extend([like, like, like, like, like, like, like])
    if year is not None:
        filters.append("tax_year = ?")
        params.append(year)
    if tag is not None:
        filters.append(
            """
            EXISTS (
                SELECT 1
                FROM entity_tags et_filter
                WHERE et_filter.entity_kind = 'rubric'
                  AND et_filter.entity_id = rubrics.code
                  AND et_filter.tax_year = rubrics.tax_year
                  AND et_filter.tag = ?
            )
            """
        )
        params.append(canonical_tag(tag))
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    return conn.execute(
        f"""
        SELECT *
        FROM rubrics
        {where}
        ORDER BY tax_year DESC, CAST(code AS INTEGER), code
        """,
        params,
    ).fetchall()


def lookup_rubrics(
    conn: sqlite3.Connection,
    query: str,
    *,
    year: int | None = None,
    limit: int = 10,
) -> list[sqlite3.Row]:
    code_terms, field_terms = parse_lookup_terms(query)
    filters: list[str] = []
    params: list[Any] = []
    predicates: list[str] = []
    if code_terms:
        placeholders = ",".join("?" for _ in code_terms)
        predicates.append(f"code IN ({placeholders})")
        params.extend(sorted(code_terms, key=int))
    for field_term in sorted(field_terms, key=int):
        predicates.append("(',' || field_no || ',') LIKE ?")
        params.append(f"%,{field_term},%")
    if not predicates:
        return []
    filters.append(f"({' OR '.join(predicates)})")
    if year is not None:
        filters.append("tax_year = ?")
        params.append(year)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT *
        FROM rubrics
        WHERE {' AND '.join(filters)}
        ORDER BY tax_year DESC, CAST(code AS INTEGER), code
        LIMIT ?
        """,
        params,
    ).fetchall()


def related_rubrics(
    conn: sqlite3.Connection,
    code: str,
    *,
    year: int | None = None,
    limit: int = 10,
) -> list[sqlite3.Row]:
    rubric = get_rubric(conn, code, year=year)
    if rubric is None:
        return []
    tax_year = int(rubric["tax_year"])
    generic_tags = {"aabent-felt", "laast-felt", "blanket", "fradrag", "indkomst"}
    tags = [
        tag
        for tag in tags_for_entity(conn, entity_kind="rubric", entity_id=code, tax_year=tax_year)
        if tag not in generic_tags
    ]
    if not tags:
        return []
    placeholders = ",".join("?" for _ in tags)
    params: list[Any] = [*tags, tax_year, code, limit]
    return conn.execute(
        f"""
        SELECT
            r.*,
            COUNT(et.tag) AS shared_tag_count,
            group_concat(et.tag, ', ') AS shared_tags
        FROM entity_tags et
        JOIN rubrics r
          ON r.tax_year = et.tax_year
         AND r.code = et.entity_id
        WHERE et.entity_kind = 'rubric'
          AND et.tag IN ({placeholders})
          AND r.tax_year = ?
          AND r.code != ?
        GROUP BY r.tax_year, r.code
        ORDER BY shared_tag_count DESC, CAST(r.code AS INTEGER), r.code
        LIMIT ?
        """,
        params,
    ).fetchall()


def database_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    counts = {
        "rubrics": conn.execute("SELECT COUNT(*) AS count FROM rubrics").fetchone()["count"],
        "rubrics_with_guidance": conn.execute(
            "SELECT COUNT(*) AS count FROM rubrics WHERE detail_url != ''"
        ).fetchone()["count"],
        "rules": conn.execute("SELECT COUNT(*) AS count FROM rules").fetchone()["count"],
        "sources": conn.execute("SELECT COUNT(*) AS count FROM sources").fetchone()["count"],
        "tags": conn.execute("SELECT COUNT(*) AS count FROM tags").fetchone()["count"],
        "tag_assignments": conn.execute(
            "SELECT COUNT(*) AS count FROM entity_tags"
        ).fetchone()["count"],
    }
    years = [
        row["tax_year"]
        for row in conn.execute(
            "SELECT DISTINCT tax_year FROM rubrics WHERE tax_year IS NOT NULL ORDER BY tax_year"
        ).fetchall()
    ]
    top_tags = [
        dict(row)
        for row in conn.execute(
            """
            SELECT t.tag, t.label, COUNT(et.tag) AS count
            FROM tags t
            LEFT JOIN entity_tags et ON et.tag = t.tag
            GROUP BY t.tag, t.label
            ORDER BY count DESC, t.tag
            LIMIT 12
            """
        ).fetchall()
    ]
    return {"counts": counts, "rubric_years": years, "top_tags": top_tags}


def latest_rubric_year(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(tax_year) AS tax_year FROM rubrics").fetchone()
    if row is None or row["tax_year"] is None:
        return None
    return int(row["tax_year"])


def list_tags(conn: sqlite3.Connection, query: str | None = None) -> list[sqlite3.Row]:
    filters: list[str] = []
    params: list[Any] = []
    if query:
        canonical = canonical_tag(query)
        matches = matching_tags(query)
        like = f"%{query}%"
        if matches:
            placeholders = ",".join("?" for _ in matches)
            filters.append(
                f"(t.tag = ? OR t.tag IN ({placeholders}) OR t.label LIKE ? OR t.description LIKE ?)"
            )
            params.extend([canonical, *matches, like, like])
        else:
            filters.append("(t.tag = ? OR t.label LIKE ? OR t.description LIKE ?)")
            params.extend([canonical, like, like])
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    return conn.execute(
        f"""
        SELECT
            t.tag,
            t.label,
            t.description,
            COUNT(et.tag) AS total_count,
            SUM(CASE WHEN et.entity_kind = 'rubric' THEN 1 ELSE 0 END) AS rubric_count,
            SUM(CASE WHEN et.entity_kind = 'rule' THEN 1 ELSE 0 END) AS rule_count
        FROM tags t
        LEFT JOIN entity_tags et ON et.tag = t.tag
        {where}
        GROUP BY t.tag, t.label, t.description
        ORDER BY total_count DESC, t.tag
        """,
        params,
    ).fetchall()


def list_tagged_entities(
    conn: sqlite3.Connection,
    tag: str,
    *,
    year: int | None = None,
    kind: str | None = None,
) -> list[sqlite3.Row]:
    canonical = canonical_tag(tag)
    filters = ["et.tag = ?"]
    params: list[Any] = [canonical]
    if year is not None:
        filters.append("(et.tax_year = ? OR et.tax_year IS NULL)")
        params.append(year)
    if kind is not None:
        filters.append("et.entity_kind = ?")
        params.append(kind)
    where = " AND ".join(filters)
    return conn.execute(
        f"""
        SELECT
            et.entity_kind,
            et.entity_id,
            et.tax_year,
            et.reason,
            CASE
                WHEN et.entity_kind = 'rubric' THEN 'Rubrik ' || r.code || ': ' || r.label
                WHEN et.entity_kind = 'rule' THEN ru.title
                ELSE et.entity_id
            END AS title,
            CASE
                WHEN et.entity_kind = 'rubric' THEN r.section
                WHEN et.entity_kind = 'rule' THEN ru.category
                ELSE ''
            END AS category,
            CASE
                WHEN et.entity_kind = 'rubric' THEN
                    CASE WHEN r.detail_url != '' THEN r.detail_url ELSE r.source_url END
                WHEN et.entity_kind = 'rule' THEN ru.source_url
                ELSE ''
            END AS source_url,
            COALESCE((
                SELECT group_concat(et_all.tag, ', ')
                FROM entity_tags et_all
                WHERE et_all.entity_key = et.entity_key
            ), '') AS tags
        FROM entity_tags et
        LEFT JOIN rubrics r
          ON et.entity_kind = 'rubric'
         AND et.entity_id = r.code
         AND et.tax_year = r.tax_year
        LEFT JOIN rules ru
          ON et.entity_kind = 'rule'
         AND et.entity_id = ru.id
        WHERE {where}
        ORDER BY et.entity_kind, et.tax_year DESC, CAST(et.entity_id AS INTEGER), et.entity_id
        """,
        params,
    ).fetchall()


def tags_for_entity(
    conn: sqlite3.Connection,
    *,
    entity_kind: str,
    entity_id: str,
    tax_year: int | None = None,
) -> list[str]:
    entity_key = entity_tag_key(entity_kind, entity_id, tax_year)
    rows = conn.execute(
        "SELECT tag FROM entity_tags WHERE entity_key = ? ORDER BY tag",
        (entity_key,),
    ).fetchall()
    return [str(row["tag"]) for row in rows]


def parse_lookup_terms(query: str) -> tuple[set[str], set[str]]:
    normalized = query.casefold()
    code_terms = set(re.findall(r"\brubrik\s*(\d{2,3})\b", normalized))
    field_terms = set(
        re.findall(r"\b(?:felt|field)(?:\s*nr\.?)?\s*(\d{2,3})\b", normalized)
    )
    if normalized.strip().isdigit():
        code_terms.add(normalized.strip())
        field_terms.add(normalized.strip())
    return code_terms, field_terms


def get_rule(conn: sqlite3.Connection, rule_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()


def get_amounts(conn: sqlite3.Connection, rule_id: str | None = None) -> list[sqlite3.Row]:
    params: list[Any] = []
    where = ""
    if rule_id is not None:
        where = "WHERE a.rule_id = ?"
        params.append(rule_id)
    return conn.execute(
        f"""
        SELECT a.*, r.title AS rule_title, r.category AS category
        FROM amounts a
        JOIN rules r ON r.id = a.rule_id
        {where}
        ORDER BY a.tax_year DESC, r.category, r.title, a.label
        """,
        params,
    ).fetchall()


def list_amounts(
    conn: sqlite3.Connection,
    *,
    year: int | None = None,
    category: str | None = None,
) -> list[sqlite3.Row]:
    filters: list[str] = []
    params: list[Any] = []
    if year is not None:
        filters.append("a.tax_year = ?")
        params.append(year)
    if category is not None:
        filters.append("r.category = ?")
        params.append(category)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    return conn.execute(
        f"""
        SELECT a.*, r.title AS rule_title, r.category AS category
        FROM amounts a
        JOIN rules r ON r.id = a.rule_id
        {where}
        ORDER BY a.tax_year DESC, r.category, r.title, a.label
        """,
        params,
    ).fetchall()


def get_fields(conn: sqlite3.Connection, rule_id: str | None = None) -> list[sqlite3.Row]:
    params: list[Any] = []
    where = ""
    if rule_id is not None:
        where = "WHERE f.rule_id = ?"
        params.append(rule_id)
    return conn.execute(
        f"""
        SELECT f.*, r.title AS rule_title
        FROM return_fields f
        LEFT JOIN rules r ON r.id = f.rule_id
        {where}
        ORDER BY f.kind, CAST(f.code AS INTEGER), f.code
        """,
        params,
    ).fetchall()


def search_fields(conn: sqlite3.Connection, query: str | None = None) -> list[sqlite3.Row]:
    if not query:
        return get_fields(conn)
    like = f"%{query}%"
    return conn.execute(
        """
        SELECT f.*, r.title AS rule_title
        FROM return_fields f
        LEFT JOIN rules r ON r.id = f.rule_id
        WHERE f.code LIKE ?
           OR f.kind LIKE ?
           OR f.tax_form LIKE ?
           OR f.label LIKE ?
           OR f.category LIKE ?
           OR f.description LIKE ?
           OR r.title LIKE ?
        ORDER BY f.kind, CAST(f.code AS INTEGER), f.code
        """,
        (like, like, like, like, like, like, like),
    ).fetchall()


def iter_sources(conn: sqlite3.Connection, query: str | None = None) -> Iterable[sqlite3.Row]:
    if not query:
        return conn.execute(
            """
            SELECT id, title, url, publisher, retrieved_at, source_type, effective_year
            FROM sources
            ORDER BY effective_year DESC, title
            """
        ).fetchall()
    like = f"%{query}%"
    return conn.execute(
        """
        SELECT id, title, url, publisher, retrieved_at, source_type, effective_year
        FROM sources
        WHERE title LIKE ? OR url LIKE ? OR publisher LIKE ? OR body LIKE ?
        ORDER BY effective_year DESC, title
        """,
        (like, like, like, like),
    ).fetchall()


def to_fts_query(query: str) -> str:
    terms = expand_query_terms(query)
    if not terms:
        raise ValueError("search query must contain at least one word or number")
    return " OR ".join(_fts_term(term) for term in terms)


STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "when",
    "where",
    "with",
    "you",
    "jeg",
    "kan",
    "og",
    "til",
    "for",
    "fra",
    "hvad",
    "hvordan",
    "skal",
}

QUERY_SYNONYMS = {
    "deduct": ("fradrag", "fratrække", "fradragsberettiget"),
    "deduction": ("fradrag", "fradragsberettiget"),
    "deductions": ("fradrag", "fradragsberettiget"),
    "transport": ("transport", "befordring", "kørsel", "kørselsfradrag"),
    "commute": ("befordring", "kørsel", "kørselsfradrag"),
    "commuting": ("befordring", "kørsel", "kørselsfradrag"),
    "mileage": ("befordring", "kørsel", "kørselsfradrag"),
    "work": ("arbejde", "arbejdsplads", "lønnet"),
    "salary": ("løn", "lønindkomst"),
    "wage": ("løn", "lønindkomst"),
    "income": ("indkomst", "indtægt"),
    "rental": ("udlejning", "lejeindtægt"),
    "rent": ("udlejning", "lejeindtægt"),
    "property": ("bolig", "ejendom"),
    "home": ("bolig", "ejerbolig", "helårsbolig"),
    "interest": ("rente", "renteudgifter", "renteindtægter"),
    "debt": ("gæld", "renteudgifter"),
    "pension": ("pension", "ratepension", "livrente"),
    "travel": ("rejse", "rejseudgifter", "kost", "logi"),
    "gift": ("gaver",),
    "gifts": ("gaver",),
}


def expand_query_terms(query: str) -> list[str]:
    raw_terms = re.findall(r"[\wæøåÆØÅ]+", query.lower(), flags=re.UNICODE)
    terms: list[str] = []
    for term in raw_terms:
        if term in STOPWORDS:
            continue
        terms.append(term)
        terms.extend(QUERY_SYNONYMS.get(term, ()))
    if not terms:
        terms = raw_terms
    return list(dict.fromkeys(terms))


def _fts_term(term: str) -> str:
    if term.isdigit() or len(term) < 3:
        return term
    return f"{term}*"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"invalid JSON in {path}: {err}") from err


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required string field: {key}")
    return value


def _row_to_search_result(row: sqlite3.Row) -> SearchResult:
    keys = set(row.keys())
    return SearchResult(
        kind=str(row["kind"]),
        identifier=str(row["identifier"]),
        title=str(row["title"]),
        category=str(row["category"]),
        tax_year=row["tax_year"],
        snippet=str(row["snippet"] or ""),
        source_url=str(row["source_url"] or ""),
        tags=str(row["tags"] or "") if "tags" in keys else "",
    )
