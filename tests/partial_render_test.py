import unittest

from examples.partial_render import PartialRenderer


def render(sql, **updates):
    model = {"resource_type": "model", "name": "a", "language": "sql", "raw_code": sql,
             "depends_on": ["model.p.input"], **updates}
    graph = {"invocation_id": "invocation", "project_name": "p", "nodes": {
        "model.p.a": model, "model.p.input": {"resource_type": "model", "name": "input", "relation_name": '"DB"."S"."INPUT"'}}}
    return PartialRenderer(graph, "model.p.a").render()


class PartialRenderTest(unittest.TestCase):
    def test_static_structure_and_declared_relations(self):
        result = render("{% set cols = ['id','name'] %}select {% for c in cols %}{{ c | upper }}{% if not loop.last %}, {% endif %}{% endfor %} from {{ ref('input') }}")
        self.assertEqual(result["sql"], 'select ID, NAME from "DB"."S"."INPUT"')
        self.assertEqual(result["status"], "candidate_body")
        self.assertFalse(result["execution_authorized"])

    def test_unknown_results_and_branches_are_not_defaulted(self):
        result = render("{% set x = run_query('delete from t') %}{% if x %}{% set n = 1 %}{% else %}{% set n = 2 %}{% endif %}select {{ n }} as id, 7 as known")
        self.assertEqual(result["status"], "partial_body")
        self.assertIsNone(result["sql"])
        self.assertTrue(any(f.get("text") == " as id, 7 as known" for f in result["fragments"]))
        self.assertTrue(any(f.get("reason") == "unknown_value:n" for f in result["fragments"]))
        result = render("select {{ var('x', 123) }}")
        self.assertIsNone(result["sql"])

    def test_calls_and_unsupported_syntax_are_explicit(self):
        for sql in ["{{ adapter.execute('delete from t') }}", "{{ modules.os.system('false') }}",
                    "{% macro x() %}1{% endmacro %}{{ x() }}", "{{ missing | default(7) }}"]:
            with self.subTest(sql=sql):
                self.assertEqual(render(sql)["status"], "partial_body")
        self.assertEqual(render("{% unsupported %}")["status"], "unavailable")
        self.assertEqual(render("print('hello')", language="python")["status"], "unavailable")

    def test_actual_compiled_body_is_preserved(self):
        result = render("{{ run_query('select 1') }}", compiled_code="select 'exact value' /* keep me */\n")
        self.assertEqual(result["status"], "observed_compiled_body")
        self.assertEqual(result["sql"], "select 'exact value' /* keep me */\n")
