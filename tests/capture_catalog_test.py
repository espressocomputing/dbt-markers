import unittest

import sqlglot
from sqlglot import exp

from examples.capture_catalog import GeneratedCatalog


class GeneratedCatalogTest(unittest.TestCase):
    def test_metadata_query_expands_generated_inputs_and_renames(self):
        catalog = GeneratedCatalog("snowflake")
        for sql in ("create view DB.S.TMP as select id from DB.S.INPUT", "alter view DB.S.TMP rename to A",
                    "create temporary table DB.S.B as select id from DB.S.A"):
            catalog.record(sqlglot.parse_one(sql, read="snowflake"))
        query = catalog.expand(sqlglot.parse_one("select b.id from DB.S.B b", read="snowflake"))
        self.assertEqual([table.sql() for table in query.find_all(exp.Table)], ["DB.S.INPUT"])
        self.assertIn("AS b", query.sql())
        catalog.record(sqlglot.parse_one("drop table DB.S.B", read="snowflake"))
        self.assertNotIn(("DB", "S", "B"), catalog.queries)


if __name__ == "__main__":
    unittest.main()
