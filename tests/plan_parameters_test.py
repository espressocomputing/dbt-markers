import unittest

from examples.plan import digest
from examples.plan_parameters import comparison_node, parameterize


class ParametersTest(unittest.TestCase):
    def test_only_declared_output_literal_changes_are_ignored(self):
        columns = {"dbt_batch_id": "invocation_id", "dbt_batch_ts": "run_started_at"}
        sql = "select 'one'::varchar as dbt_batch_id, '2026-01-01'::timestamp as dbt_batch_ts, 'one' as business_value where id=1"
        template, values = parameterize(sql, columns, "snowflake")
        self.assertEqual([item["value"] for item in values], ["one", "2026-01-01"])
        self.assertIn("'one' as business_value where id=1", template)
        changed = sql.replace("'one'::varchar", "'two'::varchar").replace("2026-01-01", "2026-02-02")
        node = lambda text: {"compiled_sql": text, "generated_statements": [{"sql": "create table DB.S.T as " + text}]}
        fingerprint = lambda text: digest(comparison_node(node(text), columns, "snowflake"))
        self.assertEqual(fingerprint(sql), fingerprint(changed))
        self.assertNotEqual(fingerprint(sql), fingerprint(sql.replace("id=1", "id=2")))
        self.assertNotEqual(fingerprint(sql), fingerprint(sql.replace("'one' as business_value", "'two' as business_value")))
        with self.assertRaisesRegex(ValueError, "string literal"):
            parameterize("select source_id as dbt_batch_id", columns, "snowflake")


if __name__ == "__main__":
    unittest.main()
