import unittest

from examples.plan import make_capture


class CaptureArtifactTest(unittest.TestCase):
    def test_original_artifacts_and_sql_are_preserved(self):
        sql = "select 'invocation-123' as dbt_batch_id /* preserve whitespace */\n"
        manifest = {"metadata": {"invocation_id": "compile"}, "nodes": {
            "model.p.a": {"resource_type": "model", "compiled": True, "compiled_code": sql,
                          "depends_on": {"nodes": []}, "config": {"materialized": "table"}}},
            "macros": {"macro.p.m": {"macro_sql": "{% macro m() %}1{% endmacro %}"}}}
        results = {"metadata": {"invocation_id": "compile"}, "args": {"which": "compile"}, "results": []}
        capture = {"command": "run", "generation_completed": True, "statements": [
            {"node_id": "model.p.a", "sql": "insert into t values (%s)", "bindings": ["exact value"]}]}
        artifact = make_capture(manifest, results, ["model.p.a"], {"vars": '{"offset": 7}'}, capture)
        self.assertIs(artifact["manifest"], manifest)
        self.assertIs(artifact["run_results"], results)
        self.assertIs(artifact["execution_capture"], capture)
        self.assertEqual(artifact["manifest"]["nodes"]["model.p.a"]["compiled_code"], sql)
        self.assertTrue(artifact["selected_bodies_available"])
        self.assertFalse({"changes", "plan_hash", "graph_hash", "nodes", "execution_edges"} & artifact.keys())
        capture["manifest"] = {"nodes": {"model.p.a": {"resource_type": "model", "depends_on": {"nodes": []}}}}
        self.assertTrue(make_capture(manifest, results, ["model.p.a"], {}, capture)["selected_bodies_available"])
        manifest["nodes"]["model.p.a"].pop("compiled_code")
        artifact = make_capture(manifest, results, ["model.p.a"], {})
        self.assertFalse(artifact["selected_bodies_available"])
        self.assertEqual(artifact["coverage"]["selected_dependency_bodies_missing"], ["model.p.a"])


if __name__ == "__main__":
    unittest.main()
