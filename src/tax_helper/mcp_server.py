from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from tax_helper import __version__
from tax_helper.cli import (
    build_field_to_code,
    build_template_payload,
    rubric_row_to_dict,
    rubric_sort_key,
    search_result_to_dict,
    tag_row_to_dict,
    tagged_entity_row_to_dict,
    template_rubric_to_dict,
)
from tax_helper.db import (
    connect_readonly,
    database_stats,
    default_db_path,
    get_rubric,
    latest_rubric_year,
    list_rubrics,
    list_tagged_entities,
    list_tags,
    lookup_rubrics,
    related_rubrics,
    search_rubrics,
    search_rules,
    search_sources,
    tags_for_entity,
)
from tax_helper.pdf_fill import fill_pdf, format_fill_value, load_fill_values
from tax_helper.tags import canonical_tag


LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    run_stdio_server(db_path=args.db, allow_write_tools=args.allow_write_tools)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taxhelper-mcp",
        description="Run the taxhelper MCP stdio server.",
    )
    parser.add_argument("--db", type=Path, default=default_db_path(), help="SQLite database path")
    parser.add_argument(
        "--allow-write-tools",
        action="store_true",
        help="Expose tools that can write files, such as PDF filling",
    )
    return parser


def run_stdio_server(*, db_path: Path, allow_write_tools: bool = False) -> None:
    server = TaxHelperMCPServer(db_path=db_path, allow_write_tools=allow_write_tools)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as err:
            write_jsonrpc_error(None, JSONRPC_PARSE_ERROR, f"Parse error: {err}")
            continue
        response = server.handle_message(message)
        if response is not None:
            write_message(response)


def write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def write_jsonrpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    data: Any | None = None,
) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    write_message({"jsonrpc": "2.0", "id": request_id, "error": error})


class TaxHelperMCPServer:
    def __init__(self, *, db_path: Path, allow_write_tools: bool = False) -> None:
        self.db_path = db_path
        self.allow_write_tools = allow_write_tools
        self.protocol_version = LATEST_PROTOCOL_VERSION

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC request")
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            return jsonrpc_error(request_id, JSONRPC_INVALID_REQUEST, "Missing method")
        if request_id is None:
            self.handle_notification(method)
            return None
        try:
            result = self.handle_request(method, message.get("params") or {})
        except ValueError as err:
            return jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, str(err))
        except sqlite3.Error as err:
            return jsonrpc_error(request_id, JSONRPC_INTERNAL_ERROR, f"SQLite error: {err}")
        except OSError as err:
            return jsonrpc_error(request_id, JSONRPC_INTERNAL_ERROR, str(err))
        if result is NotImplemented:
            return jsonrpc_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"Unknown method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def handle_notification(self, method: str) -> None:
        del method

    def handle_request(self, method: str, params: dict[str, Any]) -> dict[str, Any] | object:
        if method == "initialize":
            return self.initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools()}
        if method == "tools/call":
            return self.call_tool(params)
        return NotImplemented

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested_version = str(params.get("protocolVersion") or "")
        self.protocol_version = (
            requested_version
            if requested_version in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "taxhelper", "version": __version__},
        }

    def tools(self) -> list[dict[str, Any]]:
        tools = [
            {
                "name": "tax_lookup",
                "title": "Resolve Tax Rubric Or Field",
                "description": "Resolve a natural-language query, rubrik number, or felt/field number into source-linked tax context.",
                "inputSchema": object_schema(
                    {
                        "query": string_schema("Question, rubrik number, or field number, for example 'field 417'."),
                        "year": integer_schema("Optional rubric tax year."),
                        "limit": integer_schema("Maximum hits per result type.", default=5, minimum=1),
                    },
                    required=["query"],
                ),
            },
            {
                "name": "tax_context",
                "title": "Retrieve Agent Context",
                "description": "Return compact source-linked context for answering a tax question.",
                "inputSchema": object_schema(
                    {
                        "query": string_schema("User question or search query."),
                        "year": integer_schema("Optional rubric tax year."),
                        "tag": string_schema("Optional taxonomy tag filter."),
                        "limit": integer_schema("Maximum result count.", default=5, minimum=1),
                        "max_chars": integer_schema("Maximum rubric guidance characters.", default=1200, minimum=100),
                    },
                    required=["query"],
                ),
            },
            {
                "name": "tax_search",
                "title": "Search Tax Data",
                "description": "Search rubrikker, structured rules, and source documents.",
                "inputSchema": object_schema(
                    {
                        "query": string_schema("Search query."),
                        "year": integer_schema("Optional tax year."),
                        "tag": string_schema("Optional taxonomy tag filter for rubrics/rules."),
                        "limit": integer_schema("Maximum hits per result type.", default=8, minimum=1),
                    },
                    required=["query"],
                ),
            },
            {
                "name": "tax_rubric",
                "title": "Get One Rubric",
                "description": "Fetch one rubrik by number with tags, field number, source URL, and optional guidance text.",
                "inputSchema": object_schema(
                    {
                        "code": string_schema("Rubrik number, for example '51'."),
                        "year": integer_schema("Optional rubric tax year."),
                        "include_detail": boolean_schema("Include full guidance text when available.", default=True),
                        "max_chars": integer_schema("Maximum guidance characters.", default=2000, minimum=100),
                    },
                    required=["code"],
                ),
            },
            {
                "name": "tax_related",
                "title": "Find Related Rubrics",
                "description": "Find rubrikker related by shared taxonomy tags.",
                "inputSchema": object_schema(
                    {
                        "code": string_schema("Rubrik number."),
                        "year": integer_schema("Optional rubric tax year."),
                        "limit": integer_schema("Maximum related rubrics.", default=8, minimum=1),
                    },
                    required=["code"],
                ),
            },
            {
                "name": "tax_tags",
                "title": "List Taxonomy Tags",
                "description": "List searchable taxonomy tags and counts.",
                "inputSchema": object_schema(
                    {"query": string_schema("Optional tag search query.")},
                    required=[],
                ),
            },
            {
                "name": "tax_tagged",
                "title": "List Tagged Entities",
                "description": "List rubrikker or rules carrying a taxonomy tag.",
                "inputSchema": object_schema(
                    {
                        "tag": string_schema("Taxonomy tag or alias."),
                        "year": integer_schema("Optional year."),
                        "kind": enum_schema(["rubric", "rule"], "Optional entity kind."),
                    },
                    required=["tag"],
                ),
            },
            {
                "name": "tax_template",
                "title": "Generate Review Template",
                "description": "Generate a structured årsopgørelse/oplysningsskema review worksheet from rubrikker.",
                "inputSchema": object_schema(
                    {
                        "year": integer_schema("Optional rubric tax year; defaults to latest."),
                        "tags": array_schema("Require all of these taxonomy tags.", string_schema("Tag.")),
                        "section": string_schema("Optional section substring filter."),
                        "editable_only": boolean_schema("Only include open fields.", default=False),
                        "include_guidance": boolean_schema("Include short guidance excerpts.", default=False),
                        "max_chars": integer_schema("Guidance excerpt length.", default=500, minimum=100),
                        "limit": integer_schema("Maximum rubrikker.", minimum=1),
                    },
                    required=[],
                ),
            },
            {
                "name": "tax_stats",
                "title": "Database Coverage Stats",
                "description": "Return SQLite coverage, source, rubric, and tag statistics.",
                "inputSchema": object_schema({}, required=[]),
            },
        ]
        if self.allow_write_tools:
            tools.append(
                {
                    "name": "tax_fill_pdf",
                    "title": "Fill Oplysningsskema PDF",
                    "description": "Write a filled copy of the official 04.003 PDF. Requires an explicit output path.",
                    "inputSchema": object_schema(
                        {
                            "values": {
                                "type": "object",
                                "description": "Values keyed by rubrik or felt, for example {'rubrics': {'51': 12345}}.",
                            },
                            "values_path": string_schema("Alternative path to a JSON values file."),
                            "output_path": string_schema("Output PDF path."),
                            "pdf_path": string_schema("Optional source PDF path or URL."),
                            "year": integer_schema("Optional rubric tax year."),
                            "include_locked": boolean_schema("Also fill locked fields.", default=False),
                            "font_size": number_schema("Overlay font size.", default=8.5),
                            "dpi": integer_schema("Rasterization DPI.", default=150, minimum=72),
                        },
                        required=["output_path"],
                    ),
                }
            )
        return tools

    def call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise ValueError("tools/call requires a tool name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        handlers = {
            "tax_lookup": self.tool_lookup,
            "tax_context": self.tool_context,
            "tax_search": self.tool_search,
            "tax_rubric": self.tool_rubric,
            "tax_related": self.tool_related,
            "tax_tags": self.tool_tags,
            "tax_tagged": self.tool_tagged,
            "tax_template": self.tool_template,
            "tax_stats": self.tool_stats,
        }
        if self.allow_write_tools:
            handlers["tax_fill_pdf"] = self.tool_fill_pdf
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        try:
            payload = handler(arguments)
            return tool_result(payload)
        except (ValueError, sqlite3.Error, OSError) as err:
            return tool_result({"ok": False, "error": str(err)}, is_error=True)

    def connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise ValueError(
                f"database does not exist: {self.db_path}. Run 'taxhelper init' first "
                "or start the MCP server with --db /path/to/tax_rules.sqlite."
            )
        return connect_readonly(self.db_path)

    def tool_lookup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = require_string(arguments, "query")
        year = optional_int(arguments, "year")
        limit = int(arguments.get("limit") or 5)
        with closing(self.connect()) as conn:
            exact_rubrics = lookup_rubrics(conn, query, year=year, limit=limit)
            tags = list_tags(conn, query)
            payload = {
                "ok": True,
                "query": query,
                "year": year,
                "exact_rubrics": [
                    rubric_row_to_dict(
                        row,
                        tags=tags_for_entity(
                            conn,
                            entity_kind="rubric",
                            entity_id=str(row["code"]),
                            tax_year=row["tax_year"],
                        ),
                        include_detail=False,
                    )
                    for row in exact_rubrics
                ],
                "matching_tags": [tag_row_to_dict(row) for row in tags],
                "rubrics": [
                    search_result_to_dict(result)
                    for result in search_rubrics(conn, query, limit=limit, year=year)
                ],
                "rules": [
                    search_result_to_dict(result)
                    for result in search_rules(conn, query, limit=limit, year=year)
                ],
                "sources": [
                    search_result_to_dict(result)
                    for result in search_sources(conn, query, limit=limit, year=year)
                ],
            }
        return payload

    def tool_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = require_string(arguments, "query")
        year = optional_int(arguments, "year")
        tag = optional_string(arguments, "tag")
        limit = int(arguments.get("limit") or 5)
        max_chars = int(arguments.get("max_chars") or 1200)
        with closing(self.connect()) as conn:
            exact_rubrics = lookup_rubrics(conn, query, year=year, limit=limit)
            rubric_results = search_rubrics(conn, query, limit=limit, year=year, tag=tag)
            rule_results = search_rules(conn, query, limit=limit, year=year, tag=tag)
            source_results = [] if tag else search_sources(conn, query, limit=limit, year=year)
            rubrics_by_key: dict[tuple[int, str], sqlite3.Row] = {
                (int(row["tax_year"]), str(row["code"])): row for row in exact_rubrics
            }
            for result in rubric_results:
                if result.tax_year is None:
                    continue
                row = get_rubric(conn, result.identifier, year=int(result.tax_year))
                if row is not None:
                    rubrics_by_key[(int(row["tax_year"]), str(row["code"]))] = row
            rubric_payload = [
                rubric_row_to_dict(
                    row,
                    tags=tags_for_entity(
                        conn,
                        entity_kind="rubric",
                        entity_id=str(row["code"]),
                        tax_year=row["tax_year"],
                    ),
                    include_detail=True,
                    max_chars=max_chars,
                )
                for row in rubrics_by_key.values()
            ][:limit]
        return {
            "ok": True,
            "query": query,
            "year": year,
            "tag": tag,
            "disclaimer": "Lookup context only; not tax advice.",
            "rubrics": rubric_payload,
            "rule_hits": [search_result_to_dict(result) for result in rule_results],
            "source_hits": [search_result_to_dict(result) for result in source_results],
        }

    def tool_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = require_string(arguments, "query")
        year = optional_int(arguments, "year")
        tag = optional_string(arguments, "tag")
        limit = int(arguments.get("limit") or 8)
        with closing(self.connect()) as conn:
            return {
                "ok": True,
                "query": query,
                "year": year,
                "tag": tag,
                "rubrics": [
                    search_result_to_dict(result)
                    for result in search_rubrics(conn, query, limit=limit, year=year, tag=tag)
                ],
                "rules": [
                    search_result_to_dict(result)
                    for result in search_rules(conn, query, limit=limit, year=year, tag=tag)
                ],
                "sources": []
                if tag
                else [
                    search_result_to_dict(result)
                    for result in search_sources(conn, query, limit=limit, year=year)
                ],
            }

    def tool_rubric(self, arguments: dict[str, Any]) -> dict[str, Any]:
        code = require_string(arguments, "code")
        year = optional_int(arguments, "year")
        include_detail = bool(arguments.get("include_detail", True))
        max_chars = int(arguments.get("max_chars") or 2000)
        with closing(self.connect()) as conn:
            row = get_rubric(conn, code, year=year)
            if row is None:
                raise ValueError(f"unknown rubrik: {code}")
            tags = tags_for_entity(
                conn,
                entity_kind="rubric",
                entity_id=str(row["code"]),
                tax_year=row["tax_year"],
            )
            return {
                "ok": True,
                "rubric": rubric_row_to_dict(
                    row,
                    tags=tags,
                    include_detail=include_detail,
                    max_chars=max_chars,
                ),
            }

    def tool_related(self, arguments: dict[str, Any]) -> dict[str, Any]:
        code = require_string(arguments, "code")
        year = optional_int(arguments, "year")
        limit = int(arguments.get("limit") or 8)
        with closing(self.connect()) as conn:
            base = get_rubric(conn, code, year=year)
            if base is None:
                raise ValueError(f"unknown rubrik: {code}")
            base_tags = tags_for_entity(
                conn,
                entity_kind="rubric",
                entity_id=str(base["code"]),
                tax_year=base["tax_year"],
            )
            related = [
                rubric_row_to_dict(
                    row,
                    tags=tags_for_entity(
                        conn,
                        entity_kind="rubric",
                        entity_id=str(row["code"]),
                        tax_year=row["tax_year"],
                    ),
                    include_detail=False,
                )
                | {
                    "shared_tag_count": row["shared_tag_count"],
                    "shared_tags": split_tag_string(str(row["shared_tags"] or "")),
                }
                for row in related_rubrics(conn, code, year=year, limit=limit)
            ]
        return {
            "ok": True,
            "rubric": rubric_row_to_dict(base, tags=base_tags, include_detail=False),
            "related": related,
        }

    def tool_tags(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = optional_string(arguments, "query")
        with closing(self.connect()) as conn:
            return {
                "ok": True,
                "query": query,
                "tags": [tag_row_to_dict(row) for row in list_tags(conn, query)],
            }

    def tool_tagged(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tag = require_string(arguments, "tag")
        year = optional_int(arguments, "year")
        kind = optional_string(arguments, "kind")
        with closing(self.connect()) as conn:
            return {
                "ok": True,
                "tag": canonical_tag(tag),
                "year": year,
                "kind": kind,
                "entities": [
                    tagged_entity_row_to_dict(row)
                    for row in list_tagged_entities(conn, tag, year=year, kind=kind)
                ],
            }

    def tool_template(self, arguments: dict[str, Any]) -> dict[str, Any]:
        requested_tags = [canonical_tag(tag) for tag in arguments.get("tags") or []]
        section = optional_string(arguments, "section")
        editable_only = bool(arguments.get("editable_only", False))
        include_guidance = bool(arguments.get("include_guidance", False))
        max_chars = int(arguments.get("max_chars") or 500)
        limit = optional_int(arguments, "limit")
        with closing(self.connect()) as conn:
            year = optional_int(arguments, "year")
            if year is None:
                year = latest_rubric_year(conn)
            rows = list_rubrics(conn, year=year)
            rubrics: list[dict[str, Any]] = []
            for row in rows:
                tags = tags_for_entity(
                    conn,
                    entity_kind="rubric",
                    entity_id=str(row["code"]),
                    tax_year=row["tax_year"],
                )
                if requested_tags and not all(tag in tags for tag in requested_tags):
                    continue
                if section and section.casefold() not in str(row["section"] or "").casefold():
                    continue
                if editable_only and "aabent-felt" not in tags:
                    continue
                rubrics.append(
                    template_rubric_to_dict(
                        row,
                        tags=tags,
                        include_guidance=include_guidance,
                        max_chars=max_chars,
                    )
                )
                if limit is not None and len(rubrics) >= limit:
                    break
        return build_template_payload(
            rubrics,
            year=year,
            requested_tags=requested_tags,
            section=section,
            editable_only=editable_only,
            include_guidance=include_guidance,
        )

    def tool_stats(self, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        with closing(self.connect()) as conn:
            return {"ok": True, "database": str(self.db_path), **database_stats(conn)}

    def tool_fill_pdf(self, arguments: dict[str, Any]) -> dict[str, Any]:
        output_path = Path(require_string(arguments, "output_path")).expanduser()
        values_path_value = optional_string(arguments, "values_path")
        values = arguments.get("values")
        if values_path_value is None and not isinstance(values, dict):
            raise ValueError("tax_fill_pdf requires either values or values_path")
        year = optional_int(arguments, "year")
        include_locked = bool(arguments.get("include_locked", False))
        font_size = float(arguments.get("font_size") or 8.5)
        dpi = int(arguments.get("dpi") or 150)
        pdf_path = optional_string(arguments, "pdf_path")
        with closing(self.connect()) as conn:
            if year is None:
                year = latest_rubric_year(conn)
            rows = list_rubrics(conn, year=year)
            rubrics_by_code = {str(row["code"]): row for row in rows}
            field_to_code = build_field_to_code(rows)
            temp_values_path: Path | None = None
            if values_path_value is None:
                temp_values_path = write_temp_values_file(values)
                values_path = temp_values_path
            else:
                values_path = Path(values_path_value).expanduser()
            try:
                values_by_code, unknown_value_keys = load_fill_values(
                    values_path,
                    field_to_code=field_to_code,
                )
            finally:
                if temp_values_path is not None:
                    temp_values_path.unlink(missing_ok=True)
            tags_by_code = {
                code: tags_for_entity(
                    conn,
                    entity_kind="rubric",
                    entity_id=code,
                    tax_year=int(row["tax_year"]),
                )
                for code, row in rubrics_by_code.items()
            }
        fill_values: dict[str, Any] = {}
        skipped_locked: list[str] = []
        unknown_rubrics: list[str] = []
        blank_values: list[str] = []
        for code, value in values_by_code.items():
            if code not in rubrics_by_code:
                unknown_rubrics.append(code)
                continue
            if not include_locked and "laast-felt" in tags_by_code.get(code, []):
                skipped_locked.append(code)
                continue
            if not format_fill_value(value):
                blank_values.append(code)
                continue
            fill_values[code] = value
        result = fill_pdf(
            pdf_source=pdf_path or str(default_pdf_from_db_path(self.db_path)),
            output_path=output_path,
            values_by_code=fill_values,
            font_size=font_size,
            dpi=dpi,
        )
        return {
            "ok": True,
            "output": str(result.output_path),
            "year": year,
            "filled_codes": result.filled_codes,
            "filled_count": len(result.filled_codes),
            "missing_position_codes": result.missing_position_codes,
            "skipped_locked_codes": sorted(skipped_locked, key=rubric_sort_key),
            "blank_value_codes": sorted(blank_values, key=rubric_sort_key),
            "unknown_rubric_codes": sorted(unknown_rubrics, key=rubric_sort_key),
            "unknown_value_keys": unknown_value_keys,
            "page_count": result.page_count,
            "disclaimer": "Filled PDF copy only; review before submitting to Skattestyrelsen.",
        }


def jsonrpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def require_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required string argument: {key}")
    return value


def optional_string(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"argument must be a string: {key}")
    return value


def optional_int(arguments: dict[str, Any], key: str) -> int | None:
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"argument must be an integer: {key}")
    return int(value)


def write_temp_values_file(values: Any) -> Path:
    temp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
    with temp:
        json.dump(values, temp, ensure_ascii=False)
    return Path(temp.name)


def default_pdf_from_db_path(db_path: Path) -> Path:
    sibling = db_path.parent / "data" / "04003_januar2026-t.pdf"
    if sibling.exists():
        return sibling
    from tax_helper.cli import default_fill_pdf_path

    return default_fill_pdf_path()


def split_tag_string(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def object_schema(properties: dict[str, Any], *, required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def string_schema(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def integer_schema(
    description: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "description": description}
    if default is not None:
        schema["default"] = default
    if minimum is not None:
        schema["minimum"] = minimum
    return schema


def number_schema(description: str, *, default: float | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "number", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def boolean_schema(description: str, *, default: bool | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def enum_schema(values: list[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": description}


def array_schema(description: str, items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "description": description, "items": items}


if __name__ == "__main__":
    raise SystemExit(main())
