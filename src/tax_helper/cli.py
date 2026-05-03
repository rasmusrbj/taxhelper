from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from tax_helper import __version__
from tax_helper.db import (
    SearchResult,
    connect,
    connect_readonly,
    database_stats,
    default_data_path,
    default_db_path,
    default_seed_path,
    get_amounts,
    get_fields,
    get_rubric,
    get_rule,
    init_db,
    iter_sources,
    latest_rubric_year,
    list_amounts,
    list_rubrics,
    list_tagged_entities,
    list_tags,
    lookup_rubrics,
    rebuild_fts,
    rebuild_tags,
    related_rubrics,
    search_fields,
    search_rubrics,
    search_rules,
    search_sources,
    seed_db,
    tags_for_entity,
    upsert_rubrics,
    upsert_source,
)
from tax_helper.importer import fetch_source_document
from tax_helper.pdf_fill import fill_pdf, format_fill_value, load_fill_values
from tax_helper.rubrics import (
    DEFAULT_RUBRIC_GUIDE_URL,
    DEFAULT_RUBRIC_PDF_URL,
    fetch_bytes,
    scrape_rubrics,
)
from tax_helper.skill_install import DEFAULT_TARGETS, SUPPORTED_TARGETS, install_skill_bundle
from tax_helper.tags import canonical_tag


DEFAULT_FILL_PDF_NAME = "04003_januar2026-t.pdf"
DEFAULT_REPO_URL = "https://github.com/rasmusrbj/taxhelper.git"


def default_fill_pdf_path() -> Path:
    return default_data_path(DEFAULT_FILL_PDF_NAME)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, sqlite3.Error, OSError) as err:
        if getattr(args, "json", False):
            print_json({"ok": False, "error": str(err)})
        else:
            print(f"error: {err}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taxhelper",
        description="Look up Danish årsopgørelse rules in a local SQLite database.",
    )
    parser.add_argument("--db", type=Path, default=default_db_path(), help="SQLite database path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    command_parent = argparse.ArgumentParser(add_help=False)
    command_parent.add_argument("--db", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    command_parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_command(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, parents=[command_parent], **kwargs)

    init_parser = add_command("init", help="Bootstrap SQLite, rubrik data, tags, and local PDF")
    init_parser.add_argument("--schema-only", action="store_true", help="Only create or migrate the schema")
    init_parser.add_argument("--offline", action="store_true", help="Use the local PDF and skip online guidance pages")
    init_parser.add_argument("--refresh", action="store_true", help="Refresh the local PDF and re-scrape data")
    init_parser.add_argument("--seed", type=Path, default=default_seed_path(), help="Seed JSON path")
    init_parser.add_argument("--no-seed", action="store_true", help="Skip bundled structured seed rules")
    init_parser.add_argument("--pdf-url", default=DEFAULT_RUBRIC_PDF_URL)
    init_parser.add_argument("--pdf-path", type=Path, default=default_fill_pdf_path())
    init_parser.add_argument("--guide-url", default=DEFAULT_RUBRIC_GUIDE_URL)
    init_parser.add_argument("--tax-year", type=int, default=2025)
    init_parser.add_argument("--form-code", default="04.003")
    init_parser.add_argument("--form-title", default="Oplysningsskemaet")
    init_parser.add_argument("--guide-start-oid", type=int)
    init_parser.add_argument("--guide-end-oid", type=int)
    init_parser.add_argument("--no-guidance", action="store_true")
    init_parser.add_argument("--delay", type=float, default=0.05)
    init_parser.add_argument("--guide-timeout", type=int, default=4)
    init_parser.set_defaults(func=cmd_init)

    seed_parser = add_command("seed", help="Load the bundled source-linked seed data")
    seed_parser.add_argument("--seed", type=Path, default=default_seed_path(), help="Seed JSON path")
    seed_parser.set_defaults(func=cmd_seed)

    rebuild_parser = add_command("rebuild-fts", help="Rebuild full-text search indexes")
    rebuild_parser.set_defaults(func=cmd_rebuild_fts)

    search_parser = add_command("search", help="Search structured rules and sources")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=8)
    search_parser.add_argument("--year", type=int)
    search_parser.add_argument("--tag", help="Filter rubrics/rules by taxonomy tag")
    search_parser.set_defaults(func=cmd_search)

    show_parser = add_command("show", help="Show one structured rule by id")
    show_parser.add_argument("rule_id")
    show_parser.set_defaults(func=cmd_show)

    explain_parser = add_command(
        "explain",
        help="Print a retrieval-based explanation for a tax question",
    )
    explain_parser.add_argument("query")
    explain_parser.add_argument("--limit", type=int, default=4)
    explain_parser.add_argument("--year", type=int)
    explain_parser.add_argument("--tag", help="Filter rubrics/rules by taxonomy tag")
    explain_parser.set_defaults(func=cmd_explain)

    amounts_parser = add_command("amounts", help="List stored rates and thresholds")
    amounts_parser.add_argument("--year", type=int)
    amounts_parser.add_argument("--category")
    amounts_parser.set_defaults(func=cmd_amounts)

    fields_parser = add_command("fields", help="List annual/preliminary return fields")
    fields_parser.add_argument("query", nargs="?")
    fields_parser.set_defaults(func=cmd_fields)

    sources_parser = add_command("sources", help="List imported official source pages")
    sources_parser.add_argument("query", nargs="?")
    sources_parser.set_defaults(func=cmd_sources)

    import_parser = add_command("import-url", help="Fetch and index an official source URL")
    import_parser.add_argument("url")
    import_parser.add_argument("--publisher", default="Skattestyrelsen")
    import_parser.add_argument("--type", default="guidance", dest="source_type")
    import_parser.add_argument("--year", type=int, dest="effective_year")
    import_parser.set_defaults(func=cmd_import_url)

    scrape_parser = add_command(
        "scrape-rubrics",
        help="Scrape rubrikker from the official 04.003 PDF and TastSelv help pages",
    )
    scrape_parser.add_argument("--pdf-url", default=DEFAULT_RUBRIC_PDF_URL)
    scrape_parser.add_argument("--guide-url", default=DEFAULT_RUBRIC_GUIDE_URL)
    scrape_parser.add_argument("--tax-year", type=int, default=2025)
    scrape_parser.add_argument("--form-code", default="04.003")
    scrape_parser.add_argument("--form-title", default="Oplysningsskemaet")
    scrape_parser.add_argument("--guide-start-oid", type=int)
    scrape_parser.add_argument("--guide-end-oid", type=int)
    scrape_parser.add_argument("--no-guidance", action="store_true")
    scrape_parser.add_argument("--delay", type=float, default=0.05)
    scrape_parser.add_argument("--guide-timeout", type=int, default=4)
    scrape_parser.set_defaults(func=cmd_scrape_rubrics)

    rubrics_parser = add_command("rubrics", help="List/search scraped rubrikker")
    rubrics_parser.add_argument("query", nargs="?")
    rubrics_parser.add_argument("--year", type=int)
    rubrics_parser.add_argument("--tag", help="Filter by taxonomy tag")
    rubrics_parser.set_defaults(func=cmd_rubrics)

    rubric_parser = add_command("rubric", help="Show one scraped rubrik by number")
    rubric_parser.add_argument("code")
    rubric_parser.add_argument("--year", type=int)
    rubric_parser.set_defaults(func=cmd_rubric)

    tags_parser = add_command("tags", help="List searchable taxonomy tags")
    tags_parser.add_argument("query", nargs="?")
    tags_parser.set_defaults(func=cmd_tags)

    tag_parser = add_command("tag", help="Show rubrics/rules carrying a tag")
    tag_parser.add_argument("tag")
    tag_parser.add_argument("--year", type=int)
    tag_parser.add_argument("--kind", choices=("rubric", "rule"))
    tag_parser.set_defaults(func=cmd_tag)

    rebuild_tags_parser = add_command("rebuild-tags", help="Regenerate tags from stored data")
    rebuild_tags_parser.set_defaults(func=cmd_rebuild_tags)

    lookup_parser = add_command(
        "lookup",
        help="Resolve a query into exact rubrik/felt matches plus search hits",
    )
    lookup_parser.add_argument("query")
    lookup_parser.add_argument("--year", type=int)
    lookup_parser.add_argument("--limit", type=int, default=5)
    lookup_parser.set_defaults(func=cmd_lookup)

    context_parser = add_command(
        "context",
        help="Return compact retrieval context for agents",
    )
    context_parser.add_argument("query")
    context_parser.add_argument("--year", type=int)
    context_parser.add_argument("--tag")
    context_parser.add_argument("--limit", type=int, default=5)
    context_parser.add_argument("--max-chars", type=int, default=1200)
    context_parser.set_defaults(func=cmd_context)

    related_parser = add_command("related", help="Find related rubrikker by shared tags")
    related_parser.add_argument("code")
    related_parser.add_argument("--year", type=int)
    related_parser.add_argument("--limit", type=int, default=8)
    related_parser.set_defaults(func=cmd_related)

    template_parser = add_command(
        "template",
        help="Generate an årsopgørelse review worksheet from stored rubrikker",
    )
    template_parser.add_argument("--year", type=int, help="Rubric tax year; defaults to latest in SQLite")
    template_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="Require a taxonomy tag; repeat for multiple tags",
    )
    template_parser.add_argument("--section", help="Filter by section substring")
    template_parser.add_argument("--editable-only", action="store_true", help="Only include open fields")
    template_parser.add_argument("--include-guidance", action="store_true", help="Include short guidance excerpts")
    template_parser.add_argument("--max-chars", type=int, default=500, help="Guidance excerpt length")
    template_parser.add_argument("--limit", type=int, help="Maximum number of rubrikker")
    template_parser.set_defaults(func=cmd_template)

    fill_pdf_parser = add_command(
        "fill-pdf",
        help="Fill a copy of the official 04.003 oplysningsskema PDF from JSON values",
    )
    fill_pdf_parser.add_argument("values", type=Path, help="JSON values file keyed by rubrik or felt")
    fill_pdf_parser.add_argument(
        "--pdf",
        default=str(default_fill_pdf_path()),
        help="Source PDF path or URL",
    )
    fill_pdf_parser.add_argument("--output", type=Path, required=True, help="Filled PDF output path")
    fill_pdf_parser.add_argument("--year", type=int, help="Rubric tax year; defaults to latest in SQLite")
    fill_pdf_parser.add_argument("--include-locked", action="store_true", help="Also overlay locked fields")
    fill_pdf_parser.add_argument("--font-size", type=float, default=8.5)
    fill_pdf_parser.add_argument("--dpi", type=int, default=150, help="Rasterization DPI for the filled copy")
    fill_pdf_parser.set_defaults(func=cmd_fill_pdf)

    mcp_parser = add_command("mcp", help="Run the taxhelper MCP stdio server")
    mcp_parser.add_argument(
        "--allow-write-tools",
        action="store_true",
        help="Expose MCP tools that can write files, such as PDF filling",
    )
    mcp_parser.set_defaults(func=cmd_mcp)

    install_skills_parser = add_command(
        "install-skills",
        help="Install the bundled Agent Skill for Codex, Claude Code, and custom skill roots",
    )
    install_skills_parser.add_argument(
        "--target",
        action="append",
        choices=("all", *SUPPORTED_TARGETS),
        help="Skill host to install into; repeat for multiple targets",
    )
    install_skills_parser.add_argument(
        "--path",
        action="append",
        default=[],
        type=Path,
        help="Custom skills root directory, for example ~/.agents/skills",
    )
    install_skills_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing taxhelper skill directory",
    )
    install_skills_parser.set_defaults(func=cmd_install_skills)

    upgrade_parser = add_command(
        "upgrade",
        help="Upgrade taxhelper from GitHub and refresh the bundled Agent Skill",
    )
    upgrade_parser.add_argument(
        "--repo-url",
        default=DEFAULT_REPO_URL,
        help="Git repository URL used for pipx reinstall",
    )
    upgrade_parser.add_argument(
        "--skip-skills",
        action="store_true",
        help="Do not refresh Codex/Claude Code skills after upgrading",
    )
    upgrade_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands that would run without executing them",
    )
    upgrade_parser.set_defaults(func=cmd_upgrade)

    stats_parser = add_command("stats", help="Show database coverage and tag statistics")
    stats_parser.set_defaults(func=cmd_stats)

    return parser


def cmd_init(args: argparse.Namespace) -> int:
    with closing(connect(args.db)) as conn:
        init_db(conn)
    if args.schema_only:
        payload = {
            "ok": True,
            "database": str(args.db),
            "schema_only": True,
            "message": "initialized SQLite schema",
        }
        if args.json:
            print_json(payload)
        else:
            print(f"initialized SQLite schema at {args.db}")
        return 0

    pdf_status = ensure_local_pdf(
        pdf_path=args.pdf_path,
        pdf_url=args.pdf_url,
        refresh=args.refresh,
        offline=args.offline,
    )
    seed_source_count = 0
    seed_rule_count = 0
    seeded = False
    seed_skipped_reason = ""
    with closing(connect(args.db)) as conn:
        init_db(conn)
        if args.no_seed:
            seed_skipped_reason = "disabled"
        elif args.seed.exists():
            seed_source_count, seed_rule_count = seed_db(conn, args.seed)
            seeded = True
        else:
            seed_skipped_reason = f"seed file not found: {args.seed}"
        existing_rubrics = int(
            conn.execute("SELECT COUNT(*) AS count FROM rubrics").fetchone()["count"]
        )

    scraped = False
    scraped_rubrics = 0
    guide_pages = 0
    scanned_pages = 0
    skipped_scrape_reason = ""
    if existing_rubrics > 0 and not args.refresh:
        skipped_scrape_reason = "rubrics already exist; pass --refresh to re-scrape"
    else:
        result = scrape_rubrics(
            pdf_url=str(args.pdf_path),
            pdf_source_url=args.pdf_url,
            guide_seed_url=args.guide_url,
            tax_year=args.tax_year,
            form_code=args.form_code,
            form_title=args.form_title,
            guide_start_oid=args.guide_start_oid,
            guide_end_oid=args.guide_end_oid,
            with_guidance=not args.no_guidance and not args.offline,
            delay_seconds=args.delay,
            guide_timeout_seconds=args.guide_timeout,
        )
        with closing(connect(args.db)) as conn:
            init_db(conn)
            upsert_source(
                conn,
                {
                    "url": args.pdf_url,
                    "title": f"{args.form_code} {args.form_title} {args.tax_year}",
                    "publisher": "Skattestyrelsen",
                    "retrieved_at": date.today().isoformat(),
                    "source_type": "rubric_pdf",
                    "effective_year": args.tax_year,
                    "body": result.pdf_source_text,
                },
            )
            upsert_rubrics(conn, result.rubrics, result.guide_pages)
        scraped = True
        scraped_rubrics = len(result.rubrics)
        guide_pages = len(result.guide_pages)
        scanned_pages = result.scanned_pages

    with closing(connect_readonly(args.db)) as conn:
        stats = database_stats(conn)
    payload = {
        "ok": True,
        "database": str(args.db),
        "schema_only": False,
        "pdf": pdf_status,
        "seeded": seeded,
        "seed_source_count": seed_source_count,
        "seed_rule_count": seed_rule_count,
        "seed_skipped_reason": seed_skipped_reason,
        "scraped": scraped,
        "scraped_rubrics": scraped_rubrics,
        "guide_pages": guide_pages,
        "scanned_pages": scanned_pages,
        "skipped_scrape_reason": skipped_scrape_reason,
        "stats": stats,
    }
    if args.json:
        print_json(payload)
    else:
        print_init(payload)
    return 0


def ensure_local_pdf(
    *,
    pdf_path: Path,
    pdf_url: str,
    refresh: bool,
    offline: bool,
) -> dict[str, Any]:
    pdf_path = pdf_path.expanduser()
    if pdf_path.exists() and (offline or not refresh):
        return {
            "path": str(pdf_path),
            "source_url": pdf_url,
            "status": "existing",
            "downloaded": False,
            "bytes": pdf_path.stat().st_size,
        }
    if offline:
        raise ValueError(f"local PDF does not exist in offline mode: {pdf_path}")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_bytes = fetch_bytes(pdf_url)
    pdf_path.write_bytes(pdf_bytes)
    return {
        "path": str(pdf_path),
        "source_url": pdf_url,
        "status": "downloaded",
        "downloaded": True,
        "bytes": len(pdf_bytes),
    }


def cmd_seed(args: argparse.Namespace) -> int:
    with closing(connect(args.db)) as conn:
        source_count, rule_count = seed_db(conn, args.seed)
    print(f"seeded {rule_count} rules and {source_count} source documents into {args.db}")
    return 0


def cmd_install_skills(args: argparse.Namespace) -> int:
    targets = normalize_skill_targets(args.target)
    results = install_skill_bundle(targets=targets, custom_roots=args.path, force=args.force)
    payload = {
        "ok": True,
        "skill": "taxhelper",
        "results": [result.to_dict() for result in results],
    }
    if args.json:
        print_json(payload)
        return 0
    print("taxhelper agent skill")
    for result in results:
        status = "installed" if result.installed else f"skipped: {result.skipped_reason}"
        print(f"- {result.target}: {result.path} ({status})")
    print("Restart Codex or Claude Code after installing new skills.")
    return 0


def normalize_skill_targets(raw_targets: list[str] | None) -> tuple[str, ...]:
    if not raw_targets or "all" in raw_targets:
        return DEFAULT_TARGETS
    targets: list[str] = []
    for target in raw_targets:
        if target not in targets:
            targets.append(target)
    return tuple(targets)


def cmd_upgrade(args: argparse.Namespace) -> int:
    pipx_command = resolve_pipx_command()
    install_command = [*pipx_command, "install", "--force", f"git+{args.repo_url}"]
    commands = [install_command]
    if not args.skip_skills:
        taxhelper_command = resolve_taxhelper_command()
        if taxhelper_command is not None:
            commands.append([taxhelper_command, "install-skills", "--force"])

    if args.dry_run:
        payload = {
            "ok": True,
            "dry_run": True,
            "commands": [format_command(command) for command in commands],
        }
        if args.json:
            print_json(payload)
        else:
            for command in commands:
                print(format_command(command))
        return 0

    command_results: list[dict[str, Any]] = []
    for command in commands:
        result = run_external_command(command, capture_output=args.json)
        command_results.append(result)
        if result["returncode"] != 0:
            payload = {
                "ok": False,
                "failed_command": result["command"],
                "results": command_results,
            }
            if args.json:
                print_json(payload)
            else:
                print(f"error: command failed: {result['command']}", file=sys.stderr)
            return int(result["returncode"])

    skipped_skills = not args.skip_skills and len(commands) == 1
    payload = {
        "ok": True,
        "repo_url": args.repo_url,
        "results": command_results,
        "skills_refreshed": not args.skip_skills and not skipped_skills,
        "skills_skipped_reason": "taxhelper executable not found" if skipped_skills else "",
    }
    if args.json:
        print_json(payload)
    else:
        print("taxhelper upgraded.")
        if args.skip_skills:
            print("Skipped skill refresh.")
        elif skipped_skills:
            print("Could not find taxhelper on PATH to refresh skills.")
        else:
            print("Agent Skill refreshed.")
    return 0


def resolve_pipx_command() -> list[str]:
    pipx_path = shutil.which("pipx")
    if pipx_path:
        return [pipx_path]
    probe = [sys.executable, "-m", "pipx", "--version"]
    result = subprocess.run(probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode == 0:
        return [sys.executable, "-m", "pipx"]
    raise ValueError("pipx was not found; reinstall with the raw curl installer")


def resolve_taxhelper_command() -> str | None:
    candidates: list[str | None] = [
        shutil.which("taxhelper"),
        sys.argv[0] if sys.argv and Path(sys.argv[0]).name.startswith("taxhelper") else None,
    ]
    search_roots = []
    if os.environ.get("PIPX_BIN_DIR"):
        search_roots.append(Path(os.environ["PIPX_BIN_DIR"]))
    search_roots.append(Path.home() / ".local" / "bin")
    if os.name == "nt" and os.environ.get("APPDATA"):
        appdata_python = Path(os.environ["APPDATA"]) / "Python"
        search_roots.append(appdata_python / "Scripts")
        search_roots.extend(path / "Scripts" for path in appdata_python.glob("Python*"))
    candidate_names = ["taxhelper.exe", "taxhelper.cmd", "taxhelper"] if os.name == "nt" else ["taxhelper"]
    for root in search_roots:
        for name in candidate_names:
            candidates.append(str(root / name))
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().is_file():
            return str(Path(candidate).expanduser())
    return None


def run_external_command(command: list[str], *, capture_output: bool) -> dict[str, Any]:
    if not capture_output:
        print(f"+ {format_command(command)}")
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=True,
        check=False,
    )
    payload: dict[str, Any] = {
        "command": format_command(command),
        "returncode": result.returncode,
    }
    if capture_output:
        payload["stdout"] = result.stdout
        payload["stderr"] = result.stderr
    return payload


def format_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def cmd_rebuild_fts(args: argparse.Namespace) -> int:
    with closing(connect(args.db)) as conn:
        init_db(conn)
        rebuild_fts(conn)
    print(f"rebuilt FTS indexes and tags in {args.db}")
    return 0


def cmd_rebuild_tags(args: argparse.Namespace) -> int:
    with closing(connect(args.db)) as conn:
        init_db(conn)
        with conn:
            rebuild_tags(conn)
    print(f"rebuilt tags in {args.db}")
    return 0


def cmd_import_url(args: argparse.Namespace) -> int:
    with closing(connect(args.db)) as conn:
        init_db(conn)
        source = fetch_source_document(
            args.url,
            publisher=args.publisher,
            source_type=args.source_type,
            effective_year=args.effective_year,
        )
        with conn:
            source_id = upsert_source(conn, source)
    print(f"imported source {source_id}: {source['title']}")
    return 0


def cmd_scrape_rubrics(args: argparse.Namespace) -> int:
    result = scrape_rubrics(
        pdf_url=args.pdf_url,
        guide_seed_url=args.guide_url,
        tax_year=args.tax_year,
        form_code=args.form_code,
        form_title=args.form_title,
        guide_start_oid=args.guide_start_oid,
        guide_end_oid=args.guide_end_oid,
        with_guidance=not args.no_guidance,
        delay_seconds=args.delay,
        guide_timeout_seconds=args.guide_timeout,
    )
    with closing(connect(args.db)) as conn:
        init_db(conn)
        upsert_source(
            conn,
            {
                "url": args.pdf_url,
                "title": f"{args.form_code} {args.form_title} {args.tax_year}",
                "publisher": "Skattestyrelsen",
                "retrieved_at": "2026-05-03",
                "source_type": "rubric_pdf",
                "effective_year": args.tax_year,
                "body": result.pdf_source_text,
            },
        )
        upsert_rubrics(conn, result.rubrics, result.guide_pages)
    print(
        f"scraped {len(result.rubrics)} PDF rubrikker and "
        f"{len(result.guide_pages)} detailed guidance pages"
    )
    if not args.no_guidance:
        print(f"scanned {result.scanned_pages} info.skat.dk pages")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rubrics = search_rubrics(conn, args.query, limit=args.limit, year=args.year, tag=args.tag)
        rules = search_rules(conn, args.query, limit=args.limit, year=args.year, tag=args.tag)
        sources = [] if args.tag else search_sources(conn, args.query, limit=args.limit, year=args.year)
    if args.json:
        print_json(
            {
                "ok": True,
                "query": args.query,
                "year": args.year,
                "tag": args.tag,
                "rubrics": [search_result_to_dict(result) for result in rubrics],
                "rules": [search_result_to_dict(result) for result in rules],
                "sources": [search_result_to_dict(result) for result in sources],
            }
        )
        return 0
    print_results("Rubrics", rubrics)
    print_results("Rules", rules)
    print_results("Sources", sources)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rule = get_rule(conn, args.rule_id)
        if rule is None:
            raise ValueError(f"unknown rule id: {args.rule_id}")
        amounts = get_amounts(conn, args.rule_id)
        fields = get_fields(conn, args.rule_id)
    print_rule(rule)
    if amounts:
        print("\nAmounts")
        for row in amounts:
            print(f"- {row['tax_year']} {row['label']}: {format_number(row['value'])} {row['unit']}")
            if row["threshold_note"]:
                print(f"  {row['threshold_note']}")
    if fields:
        print("\nFields")
        for row in fields:
            print(f"- {row['kind']} {row['code']} ({row['tax_form']}): {row['label']}")
            if row["description"]:
                print(f"  {row['description']}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rubrics = search_rubrics(conn, args.query, limit=args.limit, year=args.year, tag=args.tag)
        rules = search_rules(conn, args.query, limit=args.limit, year=args.year, tag=args.tag)
        sources = [] if args.tag else search_sources(conn, args.query, limit=args.limit, year=args.year)
        rule_details = []
        for result in rules:
            rule = get_rule(conn, result.identifier)
            if rule is not None:
                rule_details.append((rule, get_amounts(conn, result.identifier), get_fields(conn, result.identifier)))
    print("Retrieval-based explanation")
    print("This is a lookup from the local database, not tax advice.\n")
    if not rubrics and not rule_details and not sources:
        print("No matching rules or sources found. Try a Danish field name, rubrik number, or source topic.")
        return 0
    if rubrics:
        print("Matching rubrikker")
        for result in rubrics:
            print(f"- {result.title} ({result.tax_year})")
            if result.category:
                print(f"  Section: {result.category}")
            if result.snippet:
                print(f"  {result.snippet}")
            print(f"  Source: {result.source_url}")
        print()
    for rule, amounts, fields in rule_details:
        print(f"{rule['title']} ({rule['id']})")
        print(f"{rule['summary']}")
        if rule["applies_to"]:
            print(f"Applies to: {rule['applies_to']}")
        if rule["caveats"]:
            print(f"Watch: {rule['caveats']}")
        if fields:
            field_text = ", ".join(f"{row['kind']} {row['code']}" for row in fields)
            print(f"Fields: {field_text}")
        if amounts:
            print("Key amounts:")
            for row in amounts[:8]:
                print(f"- {row['tax_year']} {row['label']}: {format_number(row['value'])} {row['unit']}")
        print(f"Source: {rule['source_url']}\n")
    if sources:
        print("Relevant source pages")
        for result in sources:
            year = f" ({result.tax_year})" if result.tax_year else ""
            print(f"- {result.title}{year}: {result.source_url}")
    return 0


def cmd_amounts(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rows = list_amounts(conn, year=args.year, category=args.category)
    if not rows:
        print("No amounts found.")
        return 0
    for row in rows:
        print(
            f"{row['tax_year']} | {row['category']} | {row['rule_title']} | "
            f"{row['label']}: {format_number(row['value'])} {row['unit']}"
        )
        if row["threshold_note"]:
            print(f"  {row['threshold_note']}")
    return 0


def cmd_fields(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rows = search_fields(conn, args.query)
    if not rows:
        print("No fields found.")
        return 0
    for row in rows:
        print(f"{row['kind']} {row['code']} | {row['tax_form']} | {row['label']}")
        if row["rule_title"]:
            print(f"  Rule: {row['rule_title']}")
        if row["description"]:
            print(f"  {row['description']}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rows = list(iter_sources(conn, args.query))
    if not rows:
        print("No sources found.")
        return 0
    for row in rows:
        year = f" | {row['effective_year']}" if row["effective_year"] else ""
        print(f"{row['id']} | {row['source_type']}{year} | {row['title']}")
        print(f"  {row['url']}")
        print(f"  {row['publisher']}, retrieved {row['retrieved_at']}")
    return 0


def cmd_rubrics(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rows = list_rubrics(conn, query=args.query, year=args.year, tag=args.tag)
        tags_by_key = {
            (row["tax_year"], row["code"]): tags_for_entity(
                conn,
                entity_kind="rubric",
                entity_id=str(row["code"]),
                tax_year=row["tax_year"],
            )
            for row in rows
        }
    if args.json:
        print_json(
            {
                "ok": True,
                "query": args.query,
                "year": args.year,
                "tag": args.tag,
                "rubrics": [
                    rubric_row_to_dict(
                        row,
                        tags=tags_by_key.get((row["tax_year"], row["code"]), []),
                        include_detail=False,
                    )
                    for row in rows
                ],
            }
        )
        return 0
    if not rows:
        print("No rubrikker found.")
        return 0
    for row in rows:
        print(f"Rubrik {row['code']} | felt {row['field_no']} | {row['tax_year']} | {row['label']}")
        if row["section"]:
            print(f"  Section: {row['section']}")
        if row["locked_note"]:
            print(f"  Note: {row['locked_note']}")
        tags = tags_by_key.get((row["tax_year"], row["code"]), [])
        if tags:
            print(f"  Tags: {', '.join(tags)}")
        if row["detail_url"]:
            print(f"  Guide: {row['detail_url']}")
    return 0


def cmd_rubric(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        row = get_rubric(conn, args.code, year=args.year)
        tags = (
            tags_for_entity(
                conn,
                entity_kind="rubric",
                entity_id=str(row["code"]),
                tax_year=row["tax_year"],
            )
            if row is not None
            else []
        )
    if row is None:
        raise ValueError(f"unknown rubrik: {args.code}")
    if args.json:
        print_json(
            {
                "ok": True,
                "rubric": rubric_row_to_dict(row, tags=tags, include_detail=True),
            }
        )
        return 0
    print_rubric(row, tags=tags)
    return 0


def cmd_tags(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rows = list_tags(conn, args.query)
    if args.json:
        print_json(
            {
                "ok": True,
                "query": args.query,
                "tags": [tag_row_to_dict(row) for row in rows],
            }
        )
        return 0
    if not rows:
        print("No tags found.")
        return 0
    for row in rows:
        print(
            f"{row['tag']} | {row['label']} | "
            f"{row['total_count'] or 0} matches "
            f"({row['rubric_count'] or 0} rubrics, {row['rule_count'] or 0} rules)"
        )
        if row["description"]:
            print(f"  {row['description']}")
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        rows = list_tagged_entities(conn, args.tag, year=args.year, kind=args.kind)
    if args.json:
        print_json(
            {
                "ok": True,
                "tag": args.tag,
                "year": args.year,
                "kind": args.kind,
                "entities": [tagged_entity_row_to_dict(row) for row in rows],
            }
        )
        return 0
    if not rows:
        print("No tagged entities found.")
        return 0
    for row in rows:
        year = f" ({row['tax_year']})" if row["tax_year"] else ""
        print(f"{row['entity_kind']} {row['entity_id']}{year}: {row['title']}")
        if row["category"]:
            print(f"  Category: {row['category']}")
        if row["reason"]:
            print(f"  Reason: {row['reason']}")
        if row["tags"]:
            print(f"  Tags: {row['tags']}")
        if row["source_url"]:
            print(f"  Source: {row['source_url']}")
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        exact_rubrics = lookup_rubrics(conn, args.query, year=args.year, limit=args.limit)
        rubrics = search_rubrics(conn, args.query, limit=args.limit, year=args.year)
        rules = search_rules(conn, args.query, limit=args.limit, year=args.year)
        tags = list_tags(conn, args.query)
        sources = search_sources(conn, args.query, limit=args.limit, year=args.year)
        exact_payload = [
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
        ]
    payload = {
        "ok": True,
        "query": args.query,
        "year": args.year,
        "exact_rubrics": exact_payload,
        "matching_tags": [tag_row_to_dict(row) for row in tags],
        "rubrics": [search_result_to_dict(result) for result in rubrics],
        "rules": [search_result_to_dict(result) for result in rules],
        "sources": [search_result_to_dict(result) for result in sources],
    }
    if args.json:
        print_json(payload)
        return 0
    print_lookup(payload)
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        exact_rubrics = lookup_rubrics(conn, args.query, year=args.year, limit=args.limit)
        rubric_results = search_rubrics(
            conn,
            args.query,
            limit=args.limit,
            year=args.year,
            tag=args.tag,
        )
        rule_results = search_rules(conn, args.query, limit=args.limit, year=args.year, tag=args.tag)
        source_results = [] if args.tag else search_sources(conn, args.query, limit=args.limit, year=args.year)
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
                max_chars=args.max_chars,
            )
            for row in rubrics_by_key.values()
        ][: args.limit]
    payload = {
        "ok": True,
        "query": args.query,
        "year": args.year,
        "tag": args.tag,
        "disclaimer": "Lookup context only; not tax advice.",
        "rubrics": rubric_payload,
        "rule_hits": [search_result_to_dict(result) for result in rule_results],
        "source_hits": [search_result_to_dict(result) for result in source_results],
    }
    if args.json:
        print_json(payload)
        return 0
    print_context(payload)
    return 0


def cmd_related(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        base = get_rubric(conn, args.code, year=args.year)
        rows = related_rubrics(conn, args.code, year=args.year, limit=args.limit)
        base_tags = (
            tags_for_entity(
                conn,
                entity_kind="rubric",
                entity_id=str(base["code"]),
                tax_year=base["tax_year"],
            )
            if base is not None
            else []
        )
        related_payload = [
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
                "shared_tags": split_tags(row["shared_tags"]),
            }
            for row in rows
        ]
    if base is None:
        raise ValueError(f"unknown rubrik: {args.code}")
    payload = {
        "ok": True,
        "rubric": rubric_row_to_dict(base, tags=base_tags, include_detail=False),
        "related": related_payload,
    }
    if args.json:
        print_json(payload)
        return 0
    print_related(payload)
    return 0


def cmd_template(args: argparse.Namespace) -> int:
    requested_tags = [canonical_tag(tag) for tag in args.tag]
    with closing(connect_readonly(args.db)) as conn:
        year = args.year if args.year is not None else latest_rubric_year(conn)
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
            if args.section and args.section.casefold() not in str(row["section"] or "").casefold():
                continue
            if args.editable_only and "aabent-felt" not in tags:
                continue
            rubrics.append(
                template_rubric_to_dict(
                    row,
                    tags=tags,
                    include_guidance=args.include_guidance,
                    max_chars=args.max_chars,
                )
            )
            if args.limit is not None and len(rubrics) >= args.limit:
                break
    payload = build_template_payload(
        rubrics,
        year=year,
        requested_tags=requested_tags,
        section=args.section,
        editable_only=args.editable_only,
        include_guidance=args.include_guidance,
    )
    if args.json:
        print_json(payload)
        return 0
    print_template(payload)
    return 0


def cmd_fill_pdf(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        year = args.year if args.year is not None else latest_rubric_year(conn)
        rows = list_rubrics(conn, year=year)
        rubrics_by_code = {str(row["code"]): row for row in rows}
        field_to_code = build_field_to_code(rows)
        values_by_code, unknown_value_keys = load_fill_values(args.values, field_to_code=field_to_code)
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
        if not args.include_locked and "laast-felt" in tags_by_code.get(code, []):
            skipped_locked.append(code)
            continue
        if not format_fill_value(value):
            blank_values.append(code)
            continue
        fill_values[code] = value
    result = fill_pdf(
        pdf_source=args.pdf,
        output_path=args.output,
        values_by_code=fill_values,
        font_size=args.font_size,
        dpi=args.dpi,
    )
    payload = {
        "ok": True,
        "output": str(result.output_path),
        "year": year,
        "source_pdf": args.pdf,
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
    if args.json:
        print_json(payload)
        return 0
    print_fill_pdf(payload)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from tax_helper.mcp_server import run_stdio_server

    run_stdio_server(db_path=args.db, allow_write_tools=args.allow_write_tools)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with closing(connect_readonly(args.db)) as conn:
        stats = database_stats(conn)
    payload = {"ok": True, "database": str(args.db), **stats}
    if args.json:
        print_json(payload)
        return 0
    print_stats(payload)
    return 0


def print_results(title: str, results: list[SearchResult]) -> None:
    print(title)
    if not results:
        print("- No matches")
        return
    for result in results:
        year = f" ({result.tax_year})" if result.tax_year else ""
        print(f"- {result.identifier}: {result.title}{year}")
        if result.category:
            print(f"  Category: {result.category}")
        if result.snippet:
            print(f"  {result.snippet}")
        if result.tags:
            print(f"  Tags: {result.tags}")
        print(f"  Source: {result.source_url}")


def print_rule(rule: sqlite3.Row) -> None:
    year = f" ({rule['tax_year']})" if rule["tax_year"] else ""
    print(f"{rule['title']}{year}")
    print(f"ID: {rule['id']}")
    print(f"Category: {rule['category']}")
    print(f"\n{rule['summary']}")
    if rule["applies_to"]:
        print(f"\nApplies to: {rule['applies_to']}")
    if rule["caveats"]:
        print(f"\nWatch: {rule['caveats']}")
    print(f"\nSource: {rule['source_title'] or rule['source_url']}")
    print(f"{rule['source_url']}")
    print(f"Updated: {rule['updated_at']}")


def print_rubric(row: sqlite3.Row, *, tags: list[str]) -> None:
    print(f"Rubrik {row['code']} ({row['tax_year']})")
    print(row["label"])
    if row["field_no"]:
        print(f"Felt nr.: {row['field_no']}")
    if row["section"]:
        print(f"Section: {row['section']}")
    if row["locked_note"]:
        print(f"Note: {row['locked_note']}")
    if tags:
        print(f"Tags: {', '.join(tags)}")
    if row["source_url"]:
        print(f"PDF source: {row['source_url']}")
    if row["detail_url"]:
        print(f"\nGuidance: {row['detail_title'] or row['detail_url']}")
        print(row["detail_url"])
    if row["detail_body"]:
        print("\nDetails")
        print(row["detail_body"])


def print_lookup(payload: dict[str, Any]) -> None:
    print(f"Lookup: {payload['query']}")
    if payload["exact_rubrics"]:
        print("\nExact rubrik/felt matches")
        for rubric in payload["exact_rubrics"]:
            print(f"- Rubrik {rubric['code']} | felt {rubric['field_no']} | {rubric['label']}")
            if rubric["tags"]:
                print(f"  Tags: {', '.join(rubric['tags'])}")
    if payload["matching_tags"]:
        print("\nMatching tags")
        for tag in payload["matching_tags"]:
            print(f"- {tag['tag']}: {tag['label']} ({tag['total_count']} matches)")
    if payload["rubrics"]:
        print_results("\nRubric search hits", [dict_to_search_result(item) for item in payload["rubrics"]])
    if payload["rules"]:
        print_results("\nRule search hits", [dict_to_search_result(item) for item in payload["rules"]])


def print_context(payload: dict[str, Any]) -> None:
    print(f"Context for: {payload['query']}")
    print(payload["disclaimer"])
    for rubric in payload["rubrics"]:
        print(f"\nRubrik {rubric['code']} ({rubric['tax_year']}): {rubric['label']}")
        if rubric["field_no"]:
            print(f"Felt nr.: {rubric['field_no']}")
        if rubric["tags"]:
            print(f"Tags: {', '.join(rubric['tags'])}")
        print(f"Source: {rubric['best_source_url']}")
        if rubric.get("detail_excerpt"):
            print(rubric["detail_excerpt"])
    if payload["rule_hits"]:
        print_results("\nRule hits", [dict_to_search_result(item) for item in payload["rule_hits"]])
    if payload["source_hits"]:
        print_results("\nSource hits", [dict_to_search_result(item) for item in payload["source_hits"]])


def print_related(payload: dict[str, Any]) -> None:
    rubric = payload["rubric"]
    print(f"Related to rubrik {rubric['code']}: {rubric['label']}")
    if rubric["tags"]:
        print(f"Base tags: {', '.join(rubric['tags'])}")
    if not payload["related"]:
        print("No related rubrikker found.")
        return
    for item in payload["related"]:
        print(
            f"- Rubrik {item['code']} | {item['label']} "
            f"({item['shared_tag_count']} shared tags)"
        )
        if item["shared_tags"]:
            print(f"  Shared: {', '.join(item['shared_tags'])}")
        print(f"  Source: {item['best_source_url']}")


def print_template(payload: dict[str, Any]) -> None:
    print(f"# Årsopgørelse Review Template ({payload['year'] or 'all years'})")
    print()
    print(payload["disclaimer"])
    print("Basis: generated from stored oplysningsskema/TastSelv rubrikker, not an official form.")
    print()
    filters = payload["filters"]
    filter_parts = [
        f"year={filters['year'] or 'all'}",
        f"tags={', '.join(filters['tags']) if filters['tags'] else 'any'}",
        f"section={filters['section'] or 'any'}",
        f"editable_only={filters['editable_only']}",
    ]
    print(f"Filters: {' | '.join(filter_parts)}")
    summary = payload["summary"]
    print(
        f"Rubrikker: {summary['rubric_count']} "
        f"({summary['editable_count']} open, {summary['locked_count']} locked, "
        f"{summary['blanket_count']} blanket-related)"
    )
    if not payload["sections"]:
        print("\nNo rubrikker matched the template filters.")
        return
    for section in payload["sections"]:
        print(f"\n## {section['section']}")
        for rubric in section["rubrics"]:
            field = f" | felt {rubric['field_no']}" if rubric["field_no"] else ""
            print(f"\n### Rubrik {rubric['code']}{field}: {rubric['label']}")
            print(f"- Status: {rubric['template']['status']}")
            if rubric["locked_note"]:
                print(f"- Note: {rubric['locked_note']}")
            if rubric["tags"]:
                print(f"- Tags: {', '.join(rubric['tags'])}")
            print(f"- Action: {rubric['template']['action_hint']}")
            print(f"- Source: {rubric['best_source_url']}")
            if rubric.get("guidance_excerpt"):
                print(f"- Guidance: {rubric['guidance_excerpt']}")
            print("- Existing/prefilled value:")
            print("- Calculated value:")
            print("- Evidence/document refs:")
            print("- Calculation note:")
            print("- Needs review: [ ]")


def print_fill_pdf(payload: dict[str, Any]) -> None:
    print(f"Filled PDF: {payload['output']}")
    print(f"Year: {payload['year'] or 'all'}")
    print(f"Filled rubrikker: {payload['filled_count']} ({', '.join(payload['filled_codes'])})")
    if payload["skipped_locked_codes"]:
        print(f"Skipped locked rubrikker: {', '.join(payload['skipped_locked_codes'])}")
    if payload["missing_position_codes"]:
        print(f"Missing PDF positions: {', '.join(payload['missing_position_codes'])}")
    if payload["unknown_rubric_codes"]:
        print(f"Unknown rubrikker: {', '.join(payload['unknown_rubric_codes'])}")
    if payload["unknown_value_keys"]:
        print(f"Unknown value keys: {', '.join(payload['unknown_value_keys'])}")
    print(payload["disclaimer"])


def print_init(payload: dict[str, Any]) -> None:
    print(f"initialized taxhelper at {payload['database']}")
    pdf = payload["pdf"]
    print(f"PDF: {pdf['path']} ({pdf['status']}, {pdf['bytes']} bytes)")
    if payload["seeded"]:
        print(
            f"Seed data: {payload['seed_rule_count']} rules and "
            f"{payload['seed_source_count']} sources"
        )
    elif payload["seed_skipped_reason"]:
        print(f"Seed data skipped: {payload['seed_skipped_reason']}")
    if payload["scraped"]:
        print(
            f"Rubrikker: scraped {payload['scraped_rubrics']} PDF rows and "
            f"{payload['guide_pages']} guidance pages"
        )
        if payload["scanned_pages"]:
            print(f"Guidance scan: {payload['scanned_pages']} info.skat.dk pages")
    else:
        print(f"Rubrik scrape skipped: {payload['skipped_scrape_reason']}")
    counts = payload["stats"]["counts"]
    print(
        "SQLite coverage: "
        f"{counts['rubrics']} rubrikker, "
        f"{counts['rubrics_with_guidance']} with guidance, "
        f"{counts['rules']} rules, "
        f"{counts['tags']} tags"
    )
    print("Try: taxhelper lookup 'field 417'")


def print_stats(payload: dict[str, Any]) -> None:
    print(f"Database: {payload['database']}")
    print("Counts")
    for key, value in payload["counts"].items():
        print(f"- {key}: {value}")
    if payload["rubric_years"]:
        print(f"Rubric years: {', '.join(str(year) for year in payload['rubric_years'])}")
    if payload["top_tags"]:
        print("Top tags")
        for row in payload["top_tags"]:
            print(f"- {row['tag']} ({row['label']}): {row['count']}")


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def search_result_to_dict(result: SearchResult) -> dict[str, Any]:
    return {
        "kind": result.kind,
        "identifier": result.identifier,
        "title": result.title,
        "category": result.category,
        "tax_year": result.tax_year,
        "snippet": result.snippet,
        "source_url": result.source_url,
        "tags": split_tags(result.tags),
    }


def dict_to_search_result(item: dict[str, Any]) -> SearchResult:
    return SearchResult(
        kind=str(item["kind"]),
        identifier=str(item["identifier"]),
        title=str(item["title"]),
        category=str(item.get("category") or ""),
        tax_year=item.get("tax_year"),
        snippet=str(item.get("snippet") or ""),
        source_url=str(item.get("source_url") or ""),
        tags=", ".join(item.get("tags") or []),
    )


def rubric_row_to_dict(
    row: sqlite3.Row,
    *,
    tags: list[str],
    include_detail: bool,
    max_chars: int | None = None,
) -> dict[str, Any]:
    detail_body = str(row["detail_body"] or "")
    payload: dict[str, Any] = {
        "kind": "rubric",
        "tax_year": row["tax_year"],
        "code": str(row["code"]),
        "field_no": str(row["field_no"] or ""),
        "form_code": str(row["form_code"] or ""),
        "form_title": str(row["form_title"] or ""),
        "section": str(row["section"] or ""),
        "label": str(row["label"] or ""),
        "locked_note": str(row["locked_note"] or ""),
        "tags": tags,
        "source_url": str(row["source_url"] or ""),
        "detail_url": str(row["detail_url"] or ""),
        "detail_title": str(row["detail_title"] or ""),
        "best_source_url": str(row["detail_url"] or row["source_url"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }
    if include_detail:
        payload["detail_body"] = truncate_text(detail_body, max_chars) if max_chars else detail_body
        payload["detail_excerpt"] = truncate_text(detail_body, max_chars or 800)
    return payload


def template_rubric_to_dict(
    row: sqlite3.Row,
    *,
    tags: list[str],
    include_guidance: bool,
    max_chars: int,
) -> dict[str, Any]:
    payload = rubric_row_to_dict(row, tags=tags, include_detail=False)
    payload["template"] = {
        "status": template_status(tags),
        "editable": "aabent-felt" in tags,
        "locked": "laast-felt" in tags,
        "uses_blanket": "blanket" in tags,
        "value_type": infer_template_value_type(
            " ".join([payload["section"], payload["label"], payload["locked_note"]])
        ),
        "action_hint": template_action_hint(tags),
        "fields": {
            "existing_value": None,
            "calculated_value": None,
            "evidence_document_refs": [],
            "calculation_note": "",
            "needs_review": True,
            "confidence": "unknown",
        },
        "agent_questions": template_agent_questions(payload, tags),
    }
    if include_guidance:
        payload["guidance_excerpt"] = truncate_text(str(row["detail_body"] or ""), max_chars)
    return payload


def build_template_payload(
    rubrics: list[dict[str, Any]],
    *,
    year: int | None,
    requested_tags: list[str],
    section: str | None,
    editable_only: bool,
    include_guidance: bool,
) -> dict[str, Any]:
    sections: dict[str, list[dict[str, Any]]] = {}
    for rubric in rubrics:
        section_name = rubric["section"] or "Unsectioned"
        sections.setdefault(section_name, []).append(rubric)
    return {
        "ok": True,
        "kind": "aarsopgoerelse_review_template",
        "official_template": False,
        "year": year,
        "disclaimer": "Review worksheet only; not tax advice and not an official Skattestyrelsen form.",
        "filters": {
            "year": year,
            "tags": requested_tags,
            "section": section,
            "editable_only": editable_only,
            "include_guidance": include_guidance,
        },
        "summary": {
            "rubric_count": len(rubrics),
            "editable_count": sum(1 for rubric in rubrics if rubric["template"]["editable"]),
            "locked_count": sum(1 for rubric in rubrics if rubric["template"]["locked"]),
            "blanket_count": sum(1 for rubric in rubrics if rubric["template"]["uses_blanket"]),
        },
        "sections": [
            {"section": section_name, "rubrics": section_rubrics}
            for section_name, section_rubrics in sections.items()
        ],
    }


def template_status(tags: list[str]) -> str:
    if "laast-felt" in tags and "blanket" in tags:
        return "locked_or_blanket"
    if "laast-felt" in tags:
        return "locked"
    if "blanket" in tags:
        return "blanket"
    if "aabent-felt" in tags:
        return "open"
    return "review"


def template_action_hint(tags: list[str]) -> str:
    if "laast-felt" in tags and "blanket" in tags:
        return "Verify the prefilled value and use the referenced blanket/form flow if a change is needed."
    if "laast-felt" in tags:
        return "Verify the prefilled value and source reporter; this is usually not directly editable."
    if "blanket" in tags:
        return "Use the referenced blanket/form flow, then verify that the rubrik is updated correctly."
    if "aabent-felt" in tags:
        return "Collect documentation, calculate the amount, and enter or review it in TastSelv if relevant."
    return "Review in TastSelv and keep source documentation for the value."


def infer_template_value_type(text: str) -> str:
    normalized = text.casefold()
    if "markér" in normalized or "marker" in normalized:
        return "choice"
    if "dato" in normalized:
        return "date"
    if "cvr" in normalized:
        return "identifier"
    return "amount_dkk"


def template_agent_questions(rubric: dict[str, Any], tags: list[str]) -> list[str]:
    questions = [
        "What value is currently prefilled on the årsopgørelse?",
        "What documents or calculations support changing or accepting this value?",
    ]
    if "laast-felt" in tags:
        questions.append("Who reported the locked value, and can it be corrected at the source?")
    if "blanket" in tags:
        questions.append("Which official blanket/form is required before this rubrik can be updated?")
    if "fradrag" in tags:
        questions.append("Is the expense deductible for this tax year, and is the amount net of any thresholds?")
    if rubric["field_no"]:
        questions.append(f"Does TastSelv field {rubric['field_no']} map to rubrik {rubric['code']} for this year?")
    return questions


def build_field_to_code(rows: list[sqlite3.Row]) -> dict[str, str]:
    field_to_code: dict[str, str] = {}
    for row in rows:
        code = str(row["code"])
        for field_no in split_field_numbers(str(row["field_no"] or "")):
            field_to_code[field_no] = code
    return field_to_code


def split_field_numbers(value: str) -> list[str]:
    return [item for item in re.findall(r"\d{2,3}", value)]


def rubric_sort_key(code: str) -> tuple[int, str]:
    return (int(code) if code.isdigit() else 9999, code)


def tag_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "tag": str(row["tag"]),
        "label": str(row["label"]),
        "description": str(row["description"] or ""),
        "total_count": int(row["total_count"] or 0),
        "rubric_count": int(row["rubric_count"] or 0),
        "rule_count": int(row["rule_count"] or 0),
    }


def tagged_entity_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "kind": str(row["entity_kind"]),
        "identifier": str(row["entity_id"]),
        "tax_year": row["tax_year"],
        "title": str(row["title"] or ""),
        "category": str(row["category"] or ""),
        "reason": str(row["reason"] or ""),
        "tags": split_tags(row["tags"]),
        "source_url": str(row["source_url"] or ""),
    }


def split_tags(value: str | None) -> list[str]:
    if not value:
        return []
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def truncate_text(value: str, max_chars: int) -> str:
    value = value.strip()
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}..."


def format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")
