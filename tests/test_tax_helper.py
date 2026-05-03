from __future__ import annotations

import io
import sqlite3
import tempfile
import tomllib
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tax_helper.cli import build_parser, ensure_local_pdf, infer_template_value_type, template_status
from tax_helper.db import (
    connect,
    default_data_path,
    default_db_path,
    default_seed_path,
    expand_query_terms,
    get_amounts,
    get_rule,
    init_db,
    latest_rubric_year,
    parse_lookup_terms,
    search_fields,
    search_rules,
    seed_db,
)
from tax_helper.importer import HTMLTextExtractor, clean_rubric_guide_body
from tax_helper.mcp_server import TaxHelperMCPServer
from tax_helper.pdf_fill import format_fill_value, load_fill_values, normalize_rubric_key
from tax_helper.rubrics import fetch_bytes, parse_pdf_rubrics
from tax_helper.skill_install import install_skill_bundle
from tax_helper.tags import tag_rubric


class TaxHelperTests(unittest.TestCase):
    def test_pyproject_exposes_taxhelper_command(self) -> None:
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        scripts = pyproject["project"]["scripts"]
        self.assertEqual(scripts["taxhelper"], "tax_helper.cli:main")
        self.assertEqual(scripts["tax-helper"], "tax_helper.cli:main")
        self.assertEqual(scripts["taxhelper-mcp"], "tax_helper.mcp_server:main")

    def test_common_flags_work_after_subcommand_for_agents(self) -> None:
        args = build_parser().parse_args(["lookup", "field 417", "--db", "tax.sqlite", "--json"])
        self.assertEqual(args.db, Path("tax.sqlite"))
        self.assertTrue(args.json)

    def test_version_flag_exits_cleanly(self) -> None:
        with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()) as stdout:
            build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("taxhelper", stdout.getvalue())

    def test_template_parser_accepts_agent_filters(self) -> None:
        args = build_parser().parse_args(
            [
                "template",
                "--tag",
                "transport",
                "--section",
                "Ligningsmæssige",
                "--editable-only",
                "--include-guidance",
                "--limit",
                "2",
                "--json",
            ]
        )
        self.assertEqual(args.command, "template")
        self.assertEqual(args.tag, ["transport"])
        self.assertEqual(args.section, "Ligningsmæssige")
        self.assertTrue(args.editable_only)
        self.assertTrue(args.include_guidance)
        self.assertEqual(args.limit, 2)
        self.assertTrue(args.json)

    def test_fill_pdf_parser_accepts_values_and_output(self) -> None:
        args = build_parser().parse_args(
            [
                "fill-pdf",
                "values.json",
                "--pdf",
                "04003.pdf",
                "--output",
                "filled.pdf",
                "--include-locked",
                "--json",
            ]
        )
        self.assertEqual(args.command, "fill-pdf")
        self.assertEqual(args.values, Path("values.json"))
        self.assertEqual(args.pdf, "04003.pdf")
        self.assertEqual(args.output, Path("filled.pdf"))
        self.assertTrue(args.include_locked)
        self.assertTrue(args.json)

    def test_mcp_parser_accepts_write_tool_flag(self) -> None:
        args = build_parser().parse_args(["mcp", "--db", "tax.sqlite", "--allow-write-tools"])
        self.assertEqual(args.command, "mcp")
        self.assertEqual(args.db, Path("tax.sqlite"))
        self.assertTrue(args.allow_write_tools)

    def test_install_skills_parser_accepts_targets_and_custom_path(self) -> None:
        args = build_parser().parse_args(
            [
                "install-skills",
                "--target",
                "codex",
                "--target",
                "claude",
                "--path",
                "custom-skills",
                "--force",
                "--json",
            ]
        )
        self.assertEqual(args.command, "install-skills")
        self.assertEqual(args.target, ["codex", "claude"])
        self.assertEqual(args.path, [Path("custom-skills")])
        self.assertTrue(args.force)
        self.assertTrue(args.json)

    def test_upgrade_parser_accepts_repo_and_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "upgrade",
                "--repo-url",
                "https://example.test/taxhelper.git",
                "--skip-skills",
                "--dry-run",
                "--json",
            ]
        )
        self.assertEqual(args.command, "upgrade")
        self.assertEqual(args.repo_url, "https://example.test/taxhelper.git")
        self.assertTrue(args.skip_skills)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.json)

    def test_init_parser_defaults_to_bootstrap_with_schema_escape_hatch(self) -> None:
        args = build_parser().parse_args(["init", "--offline", "--schema-only", "--json"])
        self.assertEqual(args.command, "init")
        self.assertTrue(args.offline)
        self.assertTrue(args.schema_only)
        self.assertFalse(args.refresh)
        self.assertTrue(args.json)

    def test_default_db_path_prefers_project_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("pathlib.Path.cwd", return_value=Path(tmpdir)):
                self.assertEqual(default_db_path(), Path("tax_rules.sqlite").resolve())

    def test_default_data_path_falls_back_to_packaged_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with (
                patch("pathlib.Path.cwd", return_value=tmp_path),
                patch("tax_helper.db.project_root", return_value=tmp_path),
            ):
                path = default_data_path("seed_rules.json")
        self.assertTrue(path.exists())
        self.assertIn("tax_helper", str(path))

    def test_install_skill_bundle_copies_bundled_skill_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skills"
            first = install_skill_bundle(targets=(), custom_roots=[root])
            second = install_skill_bundle(targets=(), custom_roots=[root])
            skill_path = root / "taxhelper" / "SKILL.md"
            self.assertTrue(first[0].installed)
            self.assertFalse(second[0].installed)
            self.assertTrue(skill_path.exists())
            self.assertIn("name: taxhelper", skill_path.read_text(encoding="utf-8"))

    def test_seeded_search_finds_commuting_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tax.sqlite"
            with closing(connect(db_path)) as conn:
                seed_db(conn, default_seed_path())
                results = search_rules(conn, "rubrik 51", limit=5)
                self.assertTrue(any(result.identifier == "commuting-deduction-2026" for result in results))

    def test_seeded_rule_has_amounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tax.sqlite"
            with closing(connect(db_path)) as conn:
                seed_db(conn, default_seed_path())
                rule = get_rule(conn, "travel-deduction-2026")
                self.assertIsNotNone(rule)
                amounts = get_amounts(conn, "travel-deduction-2026")
                labels = {row["label"] for row in amounts}
                self.assertIn("Annual travel deduction cap", labels)

    def test_field_search_finds_rejsefradrag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tax.sqlite"
            with closing(connect(db_path)) as conn:
                seed_db(conn, default_seed_path())
                rows = search_fields(conn, "53")
                self.assertEqual(rows[0]["rule_id"], "travel-deduction-2026")

    def test_html_extractor_collects_title_and_text(self) -> None:
        parser = HTMLTextExtractor()
        parser.feed(
            """
            <html>
              <head><title>Test page</title><style>.x{}</style></head>
              <body><h1>Kørselsfradrag</h1><script>ignore()</script><p>Rubrik 51</p></body>
            </html>
            """
        )
        self.assertEqual(parser.title, "Test page")
        self.assertIn("Kørselsfradrag", parser.text())
        self.assertIn("Rubrik 51", parser.text())
        self.assertNotIn("ignore", parser.text())

    def test_parse_pdf_rubrics_handles_multiline_rows(self) -> None:
        text = """
Personlig indkomst, hvoraf der skal betales AM-bidrag (8%) Rubrik Beløb i kroner Felt nr.
Lønindkomst, bestyrelseshonorar, fri telefon, fri bil mv. 11 Felt låst/Anvendblanket04.072 202
Anden personlig indkomst. Fx mindre personalegoder med en samlet værdi over 1.400 kr.,
indtægter ved langtidsudleje af leje- eller andelsbolig
20 250
Kapitalindkomst Fradragsberettigede tab angives med minus. Rubrik Beløb i kroner Felt nr.
Renteindtægter af indestående i pengeinstitutter mv.
31
Felt låst/Anvend
blanket 04.072
233
"""
        rubrics = parse_pdf_rubrics(
            text,
            tax_year=2025,
            form_code="04.003",
            form_title="Oplysningsskemaet",
            source_url="https://example.test/form.pdf",
        )
        by_code = {rubric.code: rubric for rubric in rubrics}
        self.assertEqual(by_code["11"].field_no, "202")
        self.assertEqual(by_code["11"].locked_note, "Felt låst/Anvend blanket 04.072")
        self.assertIn("langtidsudleje", by_code["20"].label)
        self.assertEqual(by_code["31"].field_no, "233")

    def test_fetch_bytes_accepts_local_pdf_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source.pdf"
            path.write_bytes(b"%PDF-test")
            self.assertEqual(fetch_bytes(str(path)), b"%PDF-test")

    def test_ensure_local_pdf_uses_existing_file_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "source.pdf"
            path.write_bytes(b"%PDF-test")
            status = ensure_local_pdf(
                pdf_path=path,
                pdf_url="https://example.test/source.pdf",
                refresh=True,
                offline=True,
            )
        self.assertFalse(status["downloaded"])
        self.assertEqual(status["status"], "existing")

    def test_clean_rubric_guide_body_removes_page_footer(self) -> None:
        body = "Rubrikken omfatter:\nRelevant text\nDokumentet gælder for 2025:\nVersion: 2025"
        self.assertEqual(clean_rubric_guide_body(body), "Rubrikken omfatter:\nRelevant text")

    def test_lookup_terms_accept_english_field(self) -> None:
        codes, fields = parse_lookup_terms("field 417")
        self.assertEqual(codes, set())
        self.assertEqual(fields, {"417"})

    def test_english_query_expands_to_danish_terms(self) -> None:
        terms = expand_query_terms("can I deduct transport to work?")
        self.assertIn("fradrag", terms)
        self.assertIn("befordring", terms)
        self.assertIn("arbejde", terms)
        self.assertNotIn("can", terms)

    def test_rubric_tags_use_metadata_not_guidance_cross_mentions(self) -> None:
        tags = {
            assignment.tag
            for assignment in tag_rubric(
                code="51",
                section="Ligningsmæssige fradrag",
                label="Befordringsfradrag (kørselsfradrag)",
                locked_note="",
                detail_title="Rubrik 51",
                detail_body="Du kan ikke få fradrag hvis du har fri bil.",
            )
        }
        self.assertIn("befordring", tags)
        self.assertNotIn("fri-bil", tags)

    def test_latest_rubric_year_returns_max_year(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tax.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
                self.assertIsNone(latest_rubric_year(conn))
                conn.execute(
                    """
                    INSERT INTO rubrics(tax_year, code, field_no, label)
                    VALUES (?, ?, ?, ?), (?, ?, ?, ?)
                    """,
                    (2025, "51", "417", "Befordringsfradrag", 2026, "51", "417", "Befordringsfradrag"),
                )
                self.assertEqual(latest_rubric_year(conn), 2026)

    def test_template_helpers_classify_field_type_and_status(self) -> None:
        self.assertEqual(template_status(["aabent-felt"]), "open")
        self.assertEqual(template_status(["laast-felt", "blanket"]), "locked_or_blanket")
        self.assertEqual(infer_template_value_type("Revisorbistandens art (markér)"), "choice")
        self.assertEqual(infer_template_value_type("CVR-nummer"), "identifier")

    def test_fill_value_loader_accepts_rubrics_fields_and_template_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            values_path = Path(tmpdir) / "values.json"
            values_path.write_text(
                """
                {
                  "rubrics": {"51": 12345},
                  "fields": {"429": 8000},
                  "sections": [
                    {
                      "rubrics": [
                        {
                          "code": "166",
                          "template": {
                            "fields": {
                              "calculated_value": true
                            }
                          }
                        }
                      ]
                    }
                  ]
                }
                """,
                encoding="utf-8",
            )
            values, unknown = load_fill_values(values_path, field_to_code={"429": "53"})
        self.assertEqual(values["51"], 12345)
        self.assertEqual(values["53"], 8000)
        self.assertIs(values["166"], True)
        self.assertEqual(unknown, [])

    def test_fill_value_formatting_and_keys(self) -> None:
        self.assertEqual(normalize_rubric_key("rubrik 51"), "51")
        self.assertEqual(normalize_rubric_key("rubrik_460"), "460")
        self.assertEqual(format_fill_value(True), "X")
        self.assertEqual(format_fill_value(False), "")
        self.assertEqual(format_fill_value(1234.0), "1234")

    def test_mcp_initialize_and_tools_list(self) -> None:
        server = TaxHelperMCPServer(db_path=Path("missing.sqlite"))
        initialized = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "taxhelper")
        tools = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("tax_lookup", tool_names)
        self.assertIn("tax_stats", tool_names)
        self.assertNotIn("tax_fill_pdf", tool_names)

    def test_mcp_write_tool_is_opt_in(self) -> None:
        server = TaxHelperMCPServer(db_path=Path("missing.sqlite"), allow_write_tools=True)
        tools = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("tax_fill_pdf", tool_names)

    def test_mcp_stats_tool_uses_sqlite_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "tax.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
            server = TaxHelperMCPServer(db_path=db_path)
            response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "tax_stats", "arguments": {}},
                }
            )
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["counts"]["rubrics"], 0)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
