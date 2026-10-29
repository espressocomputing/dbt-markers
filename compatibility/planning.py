"""Exercise planning and invalidation through real dbt compilation."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.plan import compile_plan


def verify(dbt, root, smoke=False):
    root.mkdir(parents=True)
    project = root / "project"
    project.mkdir()
    files = {
        "dbt_project.yml": "name: planning_canary\nversion: '1.0'\nconfig-version: 2\nprofile: planning_canary\n",
        "profiles.yml": f"planning_canary:\n  target: local\n  outputs:\n    local:\n      type: duckdb\n      path: {root / 'warehouse.duckdb'}\n      schema: canary\n      threads: 2\n",
        "seeds/input.csv": "id\n1\n2\n",
        "macros/adjust.sql": "{% macro adjust(value) %}{{ value }} + {{ var('offset', 0) }}{% endmacro %}\n",
        "models/a.sql": "select {{ adjust('id') }} as id from {{ source('raw', 'input') }}\n",
        "models/b.sql": "select id from {{ ref('a') }}\n",
        "models/c.sql": "select id from {{ ref('b') }}\n",
        "models/other.sql": "select id from {{ source('raw', 'input') }}\n",
        "models/schema.yml": "version: 2\nsources:\n  - name: raw\n    schema: canary\n    tables:\n      - name: input\nmodels:\n  - name: c\n    columns:\n      - name: id\n        tests: [not_null]\n",
        "tests/c_positive.sql": "select * from {{ ref('c') }} where id < 0\n",
    }
    for name, value in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    subprocess.run([dbt, "--quiet", "seed", "--project-dir", str(project), "--profiles-dir", str(project)], check=True, capture_output=True, text=True)
    records = {}

    def plan(label, previous=None, **overrides):
        args = SimpleNamespace(capture_python=None, warehouse_state=None, dbt=dbt, project_dir=project, profiles_dir=project, command="build", default_database="warehouse", default_schema="canary",
                               profile=None, target="local", vars="{}", select="c", exclude=None,
                               full_refresh=False, warehouse_context="duckdb/planning-canary/local",
                               output=root / (label + ".json"), previous=root / (previous + ".json") if previous else None,
                               require_complete=False)
        for key, value in overrides.items():
            setattr(args, key, value)
        result = compile_plan(args)
        records[label] = {"plan_hash": result["plan_hash"], "changes": result["changes"], "unresolved_nodes": result["unresolved_nodes"]}
        (root / "results.json").write_text(json.dumps(records, indent=2) + "\n")
        print(label, "passed", flush=True)
        return result

    a, b, c, other = ["model.planning_canary." + name for name in ("a", "b", "c", "other")]
    baseline = plan("baseline")
    assert baseline["compiled_selection_complete"] and not baseline["execution_sql_complete"]
    assert baseline["nodes"][a]["compiled_sql"].startswith("select id + 0")
    assert all("{{" not in node["compiled_sql"] for node in baseline["nodes"].values() if node["compiled_sql"])
    assert any(key.startswith("test.") for key in baseline["selected_resources"])
    repeated = plan("repeat", "baseline")
    assert baseline["invocation_id"] != repeated["invocation_id"]
    assert baseline["plan_hash"] == repeated["plan_hash"]
    assert repeated["changes"]["affected"] == []
    (project / "models/broken.sql").write_text("{% if execute %}{{ exceptions.raise_compiler_error('unavailable lookup') }}{% endif %} select 1 as id")
    partial = plan("partial_component", allow_partial=True, select="other", require_complete=True)
    assert partial["coverage"]["compilation_scope"] == "selected_component"
    assert partial["compiled_selection_complete"] and partial["nodes"][other]["compiled_sql"]
    assert not partial["nodes"]["model.planning_canary.broken"]["compiled_sql"]
    (project / "models/broken.sql").unlink()
    if smoke:
        return records

    (project / "models/a.sql").write_text(files["models/a.sql"].replace("as id", "* 2 as id"))
    changed = plan("model_edit", "baseline")
    assert a in changed["changes"]["changed"]
    assert {a, b, c}.issubset(changed["changes"]["affected"])
    assert other in changed["changes"]["unchanged"]
    (project / "models/a.sql").write_text(files["models/a.sql"])

    (project / "macros/adjust.sql").write_text(files["macros/adjust.sql"].replace("+ {{", "+ 10 + {{"))
    changed = plan("macro_edit", "baseline")
    assert a in changed["changes"]["changed"] and c in changed["changes"]["affected"]
    assert other in changed["changes"]["unchanged"]
    (project / "macros/adjust.sql").write_text(files["macros/adjust.sql"])

    changed = plan("vars_edit", "baseline", vars='{"offset": 7}')
    assert "id + 7" in changed["nodes"][a]["compiled_sql"] and c in changed["changes"]["affected"]
    changed = plan("selection_edit", "baseline", select="other")
    assert changed["graph_hash"] == baseline["graph_hash"]
    assert changed["plan_hash"] != baseline["plan_hash"] and changed["changes"]["selection_changed"]
    assert changed["changes"]["affected"] == []

    (project / "models/a.sql").write_text("{{ config(materialized='ephemeral') }}\n" + files["models/a.sql"])
    changed = plan("ephemeral_edit", "baseline")
    assert "__dbt__cte__a" in changed["nodes"][b]["compiled_sql"]
    assert c in changed["changes"]["affected"] and other in changed["changes"]["unchanged"]
    (project / "models/a.sql").write_text(files["models/a.sql"])

    (project / "models/schema.yml").write_text(files["models/schema.yml"].replace("    schema: canary", "    schema: renamed"))
    changed = plan("source_relation_edit", "baseline")
    assert "source.planning_canary.raw.input" in changed["changes"]["changed"]
    assert {a, b, c, other}.issubset(changed["changes"]["affected"])
    (project / "models/schema.yml").write_text(files["models/schema.yml"])

    (project / "tests/c_positive.sql").write_text(files["tests/c_positive.sql"].replace("< 0", "< 1"))
    changed = plan("test_edit", "baseline")
    assert changed["changes"]["changed"] == ["test.planning_canary.c_positive"]
    assert c in changed["changes"]["unchanged"]
    (project / "tests/c_positive.sql").unlink()
    changed = plan("test_removed", "baseline")
    assert changed["changes"]["removed"] == ["test.planning_canary.c_positive"]
    (project / "tests/c_positive.sql").write_text(files["tests/c_positive.sql"])

    (project / "models/c.sql").write_text(files["models/c.sql"].replace("ref('b')", "ref('other')"))
    (project / "models/b.sql").unlink()
    changed = plan("rewired_removed_parent", "baseline")
    assert b in changed["changes"]["removed"] and c in changed["changes"]["affected"]
    (project / "models/c.sql").write_text(files["models/c.sql"])
    (project / "models/b.sql").write_text(files["models/b.sql"])

    (project / "models/a.sql").write_text("{{ config(materialized='incremental') }}\n" + files["models/a.sql"])
    changed = plan("incremental_unresolved", "baseline")
    assert "runtime_materialization:incremental" in changed["unresolved_nodes"][a]
    assert c in changed["changes"]["selected_unresolved"] and not changed["compiled_selection_complete"]
    try:
        plan("strict_rejects_incremental", "baseline", require_complete=True)
    except ValueError as error:
        assert "unresolved nodes" in str(error)
    else:
        raise AssertionError("Strict planning accepted unresolved execution")
    assert not (root / "strict_rejects_incremental.json").exists()
    (project / "models/a.sql").write_text(files["models/a.sql"])

    (project / "models/b.sql").write_text("{{ config(pre_hook='select 1') }}\n" + files["models/b.sql"])
    changed = plan("hook_unresolved", "baseline")
    assert "runtime_hooks" in changed["unresolved_nodes"][b]
    assert any(node["resource_type"] == "hook" and node["compiled_sql"] == "select 1" for node in changed["nodes"].values())
    (project / "models/b.sql").write_text(files["models/b.sql"])

    (project / "dbt_project.yml").write_text(files["dbt_project.yml"] + "on-run-start: ['select 1']\n")
    changed = plan("project_hook_unresolved", "baseline")
    assert any(edge["kind"] == "project_hook_order" for edge in changed["execution_edges"])
    assert changed["compiled_selection_complete"]
    (project / "dbt_project.yml").write_text(files["dbt_project.yml"])

    (project / "models/units.yml").write_text("unit_tests:\n  - name: c_unit\n    model: c\n    given:\n      - input: ref('b')\n        rows: [{id: 1}]\n    expect:\n      rows: [{id: 1}]\n")
    changed = plan("unit_test_unresolved", "baseline")
    assert any(key.startswith("unit_test.") for key in changed["changes"]["selected_unresolved"])
    (project / "models/units.yml").unlink()

    try:
        plan("empty_selection", "baseline", select="no_such_node")
    except ValueError as error:
        assert "no executable resources" in str(error)
    else:
        raise AssertionError("Empty selection accepted")
    assert not (root / "empty_selection.json").exists()

    corrupted = json.loads((root / "baseline.json").read_text())
    corrupted["dbt_plan"]["nodes"][a]["compiled_sql"] = "select 999 as id"
    (root / "corrupted.json").write_text(json.dumps(corrupted))
    try:
        plan("corruption_rejected", "corrupted")
    except ValueError as error:
        assert "fingerprint mismatch" in str(error)
    else:
        raise AssertionError("Corrupt previous plan accepted")
    assert not (root / "corruption_rejected.json").exists()

    changed = plan("warehouse_identity_edit", "baseline", warehouse_context="different-account/role")
    assert changed["changes"]["context_changed"] and c in changed["changes"]["affected"]

    (project / "seeds/input.csv").write_text("id\n3\n4\n")
    subprocess.run([dbt, "--quiet", "seed", "--full-refresh", "--project-dir", str(project), "--profiles-dir", str(project)], check=True, capture_output=True, text=True)
    changed = plan("input_data_edit", "baseline")
    assert c in changed["changes"]["affected"]
    assert not changed["execution_sql_complete"]
    assert not changed["input_state_verified"] and not changed["execution_reuse_authorized"]
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbt", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    try:
        records = verify(str(Path(args.dbt).resolve()), args.directory.resolve(), args.smoke)
    except subprocess.CalledProcessError as error:
        print(error.stdout + error.stderr, file=sys.stderr)
        raise
    print(json.dumps({"status": "passed", "cases": list(records)}))
