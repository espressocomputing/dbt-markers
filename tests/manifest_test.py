import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined

from examples.manifest_transport import extract_manifest


def render(execute=True, model_count=3):
    source_id = "source.demo.raw.orders"
    nodes = {}
    for index in range(model_count):
        nodes[f"model.demo.m{index}"] = {
            "resource_type": "model",
            "name": f"m{index}",
            "relation_name": '"DB"."SCHEMA"."a/*b*/雪"',
            "config": {"materialized": "ephemeral" if index == 0 else "table"},
            "depends_on": {"nodes": [source_id if index == 0 else "model.demo.m0"]},
            "raw_code": "SQL_MUST_NOT_LEAK",
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
    }
    template = Environment(undefined=StrictUndefined, extensions=["jinja2.ext.do"]).from_string(
        (Path(__file__).parents[1] / "macros" / "emit_manifest.sql").read_text()
    )
    output = template.make_module(context).emit_manifest()
    assert not output.strip()
    return queries


class ManifestTest(unittest.TestCase):
    def test_macro_to_config(self):
        queries = render(model_count=250)
        self.assertGreater(len(queries), 1)
        self.assertTrue(all(query.startswith("select 1 /* ") for query in queries))
        self.assertTrue(all(len(query.encode()) < 40000 for query in queries))
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
        self.assertNotIn("MUST_NOT_LEAK", sql)

    def test_parse_sends_nothing(self):
        self.assertEqual(render(execute=False), [])

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
