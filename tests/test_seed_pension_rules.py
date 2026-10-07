from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tax_helper.db import connect, default_seed_path, get_amounts, search_rules, seed_db


class SeededPensionRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "tax.sqlite"
        with closing(connect(self.db_path)) as conn:
            seed_db(conn, default_seed_path())

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def amounts(self, rule_id: str) -> dict[tuple[int, str], dict]:
        with closing(connect(self.db_path)) as conn:
            return {
                (row["tax_year"], row["label"]): dict(row) for row in get_amounts(conn, rule_id)
            }

    def test_aldersopsparing_limits(self) -> None:
        rows = self.amounts("old-age-savings-2026")
        self.assertEqual(rows[(2026, "Aldersopsparing basic contribution limit")]["value"], 9900)
        higher = "Aldersopsparing contribution limit within 7 years of folkepensionsalder"
        self.assertEqual(rows[(2026, higher)]["value"], 64200)
        self.assertEqual(rows[(2025, "Aldersopsparing basic contribution limit")]["value"], 9400)

    def test_extra_pension_deduction(self) -> None:
        rows = self.amounts("extra-pension-deduction-2026")
        basis = "Extra pension deduction maximum calculation basis"
        self.assertEqual(rows[(2026, "Extra pension deduction rate")]["value"], 12)
        self.assertEqual(rows[(2026, basis)]["value"], 87800)
        # The 2025 basis comes from the English skat.dk page, not the rule's own source.
        self.assertIn("skat.dk/en-us", rows[(2025, basis)]["source_url"])

    def test_lump_sum_spreading(self) -> None:
        rows = self.amounts("pension-lump-sum-deduction-2026")
        self.assertEqual(rows[(2026, "Lump-sum deduction spreading period")]["value"], 10)
        self.assertEqual(rows[(2026, "Opfyldningsfradrag amount")]["value"], 63200)

    def test_danish_terms_find_the_rules(self) -> None:
        with closing(connect(self.db_path)) as conn:
            for query, rule_id in (
                ("aldersopsparing", "old-age-savings-2026"),
                ("ekstra pensionsfradrag", "extra-pension-deduction-2026"),
                ("engangsindskud", "pension-lump-sum-deduction-2026"),
            ):
                hits = [hit.identifier for hit in search_rules(conn, query, limit=5)]
                self.assertIn(rule_id, hits, query)


if __name__ == "__main__":
    unittest.main()
