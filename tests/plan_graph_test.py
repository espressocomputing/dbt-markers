import unittest
from graphlib import TopologicalSorter

from examples.plan_graph import add_hooks, add_lineage, execution_edges, physical_reads


def node(kind="model", parents=(), sql=None, relation=None, **kwargs):
    return {"resource_type": kind, "depends_on": list(parents), "compiled_sql": sql,
            "relation_name": relation, "database": "DB", "schema": "S", "identifier": None,
            "raw_code": sql, "config": {}, "unresolved": [], **kwargs}


class PlanGraphTest(unittest.TestCase):
    def test_physical_reads_respect_ctes_and_quoted_names(self):
        reads, issues = physical_reads('with x as (select * from db.s.a) select * from x join "DB"."S"."Mixed" using(id)', "snowflake")
        self.assertEqual(reads, [("DB", "S", "A"), ("DB", "S", "Mixed")])
        self.assertEqual(issues, [])
        self.assertTrue(physical_reads("select * from identifier('DB.S.T')", "snowflake")[1])
        self.assertTrue(physical_reads("select * from unresolved", "snowflake")[1])
        self.assertEqual(physical_reads("select * from a", "snowflake", "DB", "S")[0], [("DB", "S", "A")])

    def test_materialization_reads_keep_prior_state_and_exclude_created_temporaries(self):
        sql = "create temporary table DB.S.TMP as select * from DB.S.INPUT; insert into DB.S.DEST select * from DB.S.TMP"
        self.assertEqual(physical_reads(sql, "snowflake"), ([("DB", "S", "DEST"), ("DB", "S", "INPUT")], []))
        self.assertEqual(physical_reads("drop table DB.S.OLD", "snowflake"), ([], []))
        nodes = {"a": node(sql="select * from DB.S.INPUT", relation="DB.S.A",
                           generated_statements=[{"sql": "merge into DB.S.A using DB.S.INPUT on A.id=INPUT.id when matched then update set A.id=INPUT.id"}])}
        add_lineage(nodes, "snowflake")
        parents = [nodes[key] for key in nodes["a"]["sql_depends_on"]]
        self.assertEqual({parent["state"] for parent in parents}, {"before_node", "external"})
        self.assertNotIn("a", nodes["a"]["depends_on"])

    def test_sql_edges_external_inputs_and_cycles(self):
        nodes = {"a": node(sql="select * from EXTERNAL.S.INPUT", relation="DB.S.A"),
                 "b": node(sql="select * from DB.S.A", relation="DB.S.B")}
        add_lineage(nodes, "snowflake")
        self.assertEqual(nodes["b"]["depends_on"], ["a"])
        external = nodes["a"]["depends_on"][0]
        self.assertEqual(nodes[external]["resource_type"], "external_relation")
        cycle = {"a": node(sql="select * from DB.S.B", relation="DB.S.A"),
                 "b": node(sql="select * from DB.S.A", relation="DB.S.B")}
        add_lineage(cycle, "snowflake")
        self.assertTrue(all("sql_lineage_cycle" in n["unresolved"] for n in cycle.values()))

    def test_multi_parent_test_gates_only_common_descendants(self):
        nodes = {"a": node(), "b": node(parents=["a"]), "d": node(),
                 "e": node(parents=["b", "d"]), "t": node("test", ["a", "d"])}
        add_lineage(nodes, "snowflake")
        edges, _ = execution_edges(nodes, nodes, "build", "project")
        gates = {(e["from"], e["to"]) for e in edges if e["kind"] == "test_gate"}
        self.assertEqual(gates, {("t", "e")})
        self.assertFalse(any(e["kind"] == "test_gate" for e in execution_edges(nodes, nodes, "run", "project")[0]))

    def test_unit_tests_gate_models_without_cycles(self):
        nodes = {"a": node(), "b": node(parents=["a"]), "u": node("unit_test", ["b"])}
        add_lineage(nodes, "snowflake")
        edges, _ = execution_edges(nodes, nodes, "build", "project")
        graph = {key: [] for key in nodes}
        for e in edges:
            graph[e["to"]].append(e["from"])
        order = list(TopologicalSorter(graph).static_order())
        self.assertLess(order.index("a"), order.index("u"))
        self.assertLess(order.index("u"), order.index("b"))

    def test_hooks_bound_the_entire_model_execution(self):
        nodes = {"a": node(config={"post-hook": [{"sql": "select 1"}]}),
                 "b": node(parents=["a"], config={"pre-hook": [{"sql": "select 2"}]})}
        add_hooks(nodes)
        add_lineage(nodes, "snowflake")
        edges, selected = execution_edges(nodes, ["a", "b"], "build", "project")
        self.assertIn("hook.a.post-hook.0", selected)
        self.assertIn({"from": "hook.a.post-hook.0", "to": "hook.b.pre-hook.0", "kind": "data"}, edges)
        graph = {key: [] for key in nodes}
        for e in edges:
            graph[e["to"]].append(e["from"])
        self.assertEqual(list(TopologicalSorter(graph).static_order()), ["a", "hook.a.post-hook.0", "hook.b.pre-hook.0", "b"])


if __name__ == "__main__":
    unittest.main()
