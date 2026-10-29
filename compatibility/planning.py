"""Verify lossless artifact capture through real dbt compilation."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.plan import compile_capture


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

    def snapshot(label, **overrides):
        args = SimpleNamespace(capture_python=None, warehouse_state=None, dbt=dbt, project_dir=project, profiles_dir=project,
                               command="build", profile=None, target="local", vars="{}", select="c", exclude=None,
                               full_refresh=False, warehouse_context="duckdb/capture-canary/local",
                               output=root / (label + ".json"), require_complete=False)
        for key, value in overrides.items():
            setattr(args, key, value)
        result = compile_capture(args)
        assert result["schema"] == "dbt_markers.sql_capture.v1"
        assert not {"changes", "plan_hash", "graph_hash", "context_hash", "execution_edges", "nodes"} & result.keys()
        assert json.loads(args.output.read_text())["dbt_capture"] == result
        records[label] = result["coverage"]
        (root / "results.json").write_text(json.dumps(records, indent=2) + "\n")
        print(label, "passed", flush=True)
        return result

    a, b, c, other = ["model.planning_canary." + name for name in ("a", "b", "c", "other")]
    baseline = snapshot("baseline")
    assert baseline["selected_bodies_available"]
    assert baseline["manifest"]["nodes"][a]["compiled_code"].startswith("select id + 0")
    assert baseline["manifest"]["nodes"][c]["depends_on"]["nodes"] == [b]
    assert any(key.startswith("test.") for key in baseline["selected_resources"])
    repeated = snapshot("repeat")
    assert baseline["invocation_id"] != repeated["invocation_id"]
    assert baseline["manifest"]["nodes"][a]["compiled_code"] == repeated["manifest"]["nodes"][a]["compiled_code"]
    (project / "models/broken.sql").write_text("{% if execute %}{{ exceptions.raise_compiler_error('unavailable lookup') }}{% endif %} select 1 as id")
    partial = snapshot("partial_component", allow_partial=True, select="other", require_complete=True)
    assert partial["coverage"]["compilation_scope"] == "selected_component"
    assert partial["selected_bodies_available"]
    assert "model.planning_canary.broken" in partial["coverage"]["compiled_bodies"]["missing"]
    assert "model.planning_canary.broken" in partial["manifest"]["nodes"]
    (project / "models/broken.sql").unlink()
    if smoke:
        return records

    changed = snapshot("vars_edit", vars='{"offset": 7}')
    assert "id + 7" in changed["manifest"]["nodes"][a]["compiled_code"]
    assert changed["context"]["vars"] == '{"offset": 7}'
    changed = snapshot("selection_edit", select="other")
    assert other in changed["selected_resources"] and c not in changed["selected_resources"]
    assert c in changed["manifest"]["nodes"]
    (project / "models/a.sql").write_text("{{ config(materialized='incremental') }}\n" + files["models/a.sql"])
    changed = snapshot("incremental_body")
    assert changed["manifest"]["nodes"][a]["config"]["materialized"] == "incremental"
    assert changed["manifest"]["nodes"][a]["compiled_code"]
    assert not changed["coverage"]["adapter_sql_generated"]
    (project / "models/a.sql").write_text(files["models/a.sql"])
    (project / "models/b.sql").write_text("{{ config(pre_hook='select 1') }}\n" + files["models/b.sql"])
    changed = snapshot("hooks_preserved")
    assert changed["manifest"]["nodes"][b]["config"]["pre-hook"][0]["sql"] == "select 1"
    assert changed["manifest"]["macros"]
    try:
        snapshot("empty_selection", select="no_such_node")
    except ValueError as error:
        assert "no executable resources" in str(error)
    else:
        raise AssertionError("Empty selection accepted")
    assert not (root / "empty_selection.json").exists()
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
