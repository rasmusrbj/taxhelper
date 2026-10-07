from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tax_helper.db import (
    connect,
    default_seed_path,
    get_amounts,
    get_rule,
    list_amounts,
    seed_db,
)


class SeededIndexedAmountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "tax.sqlite"
        with closing(connect(self.db_path)) as conn:
            seed_db(conn, default_seed_path())

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_2026_personfradrag_cites_its_own_source(self) -> None:
        with closing(connect(self.db_path)) as conn:
            rows = {row["label"]: row for row in get_amounts(conn, "income-tax-brackets-2026")}
            self.assertEqual(rows["Personfradrag"]["value"], 54100)
            # The rule cites skat.dk; this amount comes from Skatteministeriet's table.
            self.assertIn("svmn.dk", rows["Personfradrag"]["source_url"])
            self.assertIn("skat.dk", rows["Mellemskat rate"]["source_url"])
            thresholds = {row["label"]: row["value"] for row in list_amounts(conn, year=2026)}
            self.assertEqual(thresholds["Aktieindkomst progression threshold"], 79400)
            self.assertEqual(thresholds["Aktiesparekonto deposit limit"], 174200)
            self.assertEqual(thresholds["Charitable gifts deduction maximum"], 20000)

    def test_2027_amounts_are_year_filtered_and_flagged(self) -> None:
        with closing(connect(self.db_path)) as conn:
            rows = list_amounts(conn, year=2027)
            values = {(row["rule_id"], row["label"]): row["value"] for row in rows}
            self.assertEqual(values[("income-tax-brackets-2027", "Personfradrag")], 55700)
            self.assertEqual(
                values[("employment-and-job-deductions-2027", "Beskæftigelsesfradrag maximum")],
                65200,
            )
            self.assertTrue(all(row["tax_year"] == 2027 for row in rows))
            # 2027 was seeded before Skattestyrelsen's guidance; keep the reminder visible
            # in every rule until it is re-verified.
            for rule_id in {rule_id for rule_id, _ in values}:
                rule = get_rule(conn, rule_id)
                self.assertIn("Needs refinement", rule["caveats"], rule_id)
                self.assertIn("svmn.dk", rule["source_url"], rule_id)


if __name__ == "__main__":
    unittest.main()
