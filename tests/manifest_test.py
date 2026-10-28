import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined

from examples.manifest_transport import extract_manifest


def render(execute=True, model_count=3, mode="auto", context_overrides=None):
    def compiler_error(message):
        raise ValueError(message)

    source_id = "source.demo.raw.orders"
    nodes = {}
    for index in range(model_count):
        nodes[f"model.demo.m{index}"] = {
            "resource_type": "model",
            "name": f"m{index}",
            "relation_name": '"DB"."SCHEMA"."a/*b*/雪"',
            "config": {"materialized": "ephemeral" if index == 0 else "table"},
            "depends_on": {"nodes": [source_id if index == 0 else "model.demo.m0"]},
            "language": "sql",
            "raw_code": "select * from {{ ref('input') }} where label = '/*雪*/'",
            "compiled_code": "select * from DB.SCHEMA.input where label = '/*雪*/'",
            "meta": {"private": "META_MUST_NOT_LEAK"},
        }
    graph = {
        "nodes": nodes,
        "sources": {source_id: {"resource_type": "source", "name": "orders"}},
    }
    queries = []
    context = {
        "execute": execute,
        "graph": graph,
        "selected_resources": ["model.demo.m1"],
        "invocation_id": "demo-invocation",
        "project_name": "demo",
        "dbt_version": "fixture",
        "flags": SimpleNamespace(WHICH="run"),
        "target": SimpleNamespace(name="dev"),
        "local_md5": lambda text: hashlib.md5(text.encode()).hexdigest(),
        "tojson": json.dumps,
        "run_query": queries.append,
        "exceptions": SimpleNamespace(raise_compiler_error=compiler_error),
        "var": lambda name, default: mode,
        "log": lambda message, info: None,
    }
    context.update(context_overrides or {})
    template = Environment(undefined=StrictUndefined, extensions=["jinja2.ext.do"]).from_string(
        (Path(__file__).parents[1] / "macros" / "emit_manifest.sql").read_text()
    )
    output = template.make_module(context).emit_manifest()
    assert not output.strip()
    return queries


class ManifestTest(unittest.TestCase):
    def test_macro_to_config(self):
        queries = render(model_count=250)
        self.assertEqual(len(queries), 1)
        self.assertTrue(all(query.startswith("select 1 /* ") for query in queries))
        self.assertTrue(all(query.count("/*") == query.count("*/") == 1 for query in queries))
        sql = "\n".join(reversed(queries)) + "\n" + queries[0]
        sql += '\nselect 2 /* {"dbt_markers": "1.0.0"} */'
        graph = extract_manifest(sql)
        self.assertEqual(len(graph["nodes"]), 251)
        self.assertEqual(graph["selected_resources"], ["model.demo.m1"])
        self.assertEqual(graph["nodes"]["model.demo.m0"]["materialized"], "ephemeral")
        self.assertEqual(graph["nodes"]["model.demo.m1"]["depends_on"], ["model.demo.m0"])
        self.assertEqual(graph["nodes"]["model.demo.m0"]["depends_on"], ["source.demo.raw.orders"])
        self.assertEqual(graph["nodes"]["model.demo.m1"]["relation_name"], '"DB"."SCHEMA"."a/*b*/雪"')
        self.assertEqual(graph["nodes"]["model.demo.m1"]["raw_code"],
                         "select * from {{ ref('input') }} where label = '/*雪*/'")
        self.assertEqual(graph["nodes"]["model.demo.m1"]["compiled_code"],
                         "select * from DB.SCHEMA.input where label = '/*雪*/'")
        self.assertEqual(graph["nodes"]["model.demo.m1"]["language"], "sql")
        self.assertNotIn("META_MUST_NOT_LEAK", sql)

    def test_large_graph_uses_one_query(self):
        queries = render(model_count=6000)
        self.assertEqual(len(queries), 1)
        self.assertGreater(len(queries[0].encode()), 1024 * 1024)
        self.assertEqual(len(extract_manifest(queries[0])["nodes"]), 6001)

    def test_parse_sends_nothing(self):
        self.assertEqual(render(execute=False), [])

    def test_missing_selected_resource_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "omits selected nodes"):
            render(model_count=1, mode="required")
        with self.assertRaisesRegex(ValueError, "Manifest unavailable"):
            extract_manifest("\n".join(render(model_count=1)))

    def test_disabled_delivery(self):
        self.assertEqual(render(mode="off"), [])

    def test_build_without_unit_test_visibility_falls_back(self):
        for command in ["build", "test"]:
            context = {"flags": SimpleNamespace(WHICH=command), "dbt_version": "1.10.23"}
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "unit-test execution"):
                    extract_manifest("\n".join(render(context_overrides=context)))
                with self.assertRaisesRegex(ValueError, "unit-test execution"):
                    render(mode="required", context_overrides=context)
                context["dbt_version"] = "1.7.20"
                self.assertEqual(extract_manifest("\n".join(render(context_overrides=context)))["command"], command)

    def test_core_unit_definitions_do_not_establish_unit_selection(self):
        context = {
            "flags": SimpleNamespace(WHICH="build"),
            "dbt_version": "1.12.6",
            "graph": {"unit_tests": {"unit_test.demo.check": {}}},
        }
        with self.assertRaisesRegex(ValueError, "unit-test execution"):
            extract_manifest("\n".join(render(context_overrides=context)))
        with self.assertRaisesRegex(ValueError, "unit-test execution"):
            render(mode="required", context_overrides=context)

    def test_incomplete_or_corrupt_transfer_is_rejected(self):
        queries = render(model_count=250)
        invalid = [
            "\n".join(queries[:-1]),
            "\n".join(queries).replace("ephemeral", "incorrect"),
            "\n".join(queries + [queries[0].replace("demo-invocation", "different")]),
            "\n".join(queries + [queries[0].replace("ephemeral", "incorrect")]),
        ]
        for sql in invalid:
            with self.subTest(sql=sql[:60]), self.assertRaises(ValueError):
                extract_manifest(sql)


if __name__ == "__main__":
    unittest.main()
