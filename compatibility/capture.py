"""Verify offline Snowflake SQL generation and stop-on-unknown behavior."""

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path


def verify(python, root):
    root.mkdir(parents=True)
    project = root / "project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: capture_canary\nversion: '1.0'\nconfig-version: 2\nprofile: capture_canary\n")
    (project / "profiles.yml").write_text("capture_canary:\n  target: offline\n  outputs:\n    offline:\n      type: snowflake\n      account: offline.invalid\n      user: offline\n      password: unused\n      role: ROLE\n      warehouse: WAREHOUSE\n      database: DATABASE\n      schema: SCHEMA\n      threads: 1\n")
    a = project / "models/a.sql"
    a.write_text("{{ config(materialized='table', pre_hook='select 10', post_hook='select 20') }}\nselect 1 as id\n")
    (project / "models/b.sql").write_text("select id from {{ ref('a') }}\n")
    state = {"schema": "dbt_markers.warehouse_state.v1", "adapter_type": "snowflake",
             "databases": {"DATABASE": {"schemas": {"SCHEMA": {"relations": {}}}}}}
    records = {}

    def run(label, command="run", select=None, snapshot=None):
        target = root / label
        target.mkdir()
        args = ["--quiet", "--write-json", command, "--project-dir", str(project),
                "--profiles-dir", str(project), "--target-path", str(target / "target")]
        if select:
            args.extend(["--select", select])
        request = {"warehouse_state": snapshot or state, "command": command, "dbt_args": args}
        (target / "request.json").write_text(json.dumps(request))
        process = subprocess.run([python, str(Path(__file__).parents[1] / "examples/capture_core.py"),
                                  "--request", str(target / "request.json"), "--output", str(target / "capture.json")],
                                 capture_output=True, text=True, timeout=120)
        (target / "console.txt").write_text(process.stdout + process.stderr)
        assert process.returncode == 0, (label, process.stdout, process.stderr)
        result = json.loads((target / "capture.json").read_text())
        assert result["offline"]
        records[label] = {"generation_completed": result["generation_completed"], "statements": len(result["statements"]), "blockers": result["blockers"], "node_results": result["node_results"]}
        (root / "results.json").write_text(json.dumps(records, indent=2) + "\n")
        print(label, json.dumps(records[label]), flush=True)
        return result

    result = run("table_view_hooks")
    assert result["generation_completed"], result
    sql = [statement["sql"].strip().lower() for statement in result["statements"]]
    assert sql[0] == "select 10" and "create or replace transient table" in sql[1]
    assert sql[2] == "select 20" and "create or replace" in sql[3] and "view" in sql[3]
    assert len(sql) == 4

    project_config = project / "dbt_project.yml"
    profile = project / "profiles.yml"
    original_project, original_profile = project_config.read_text(), profile.read_text()
    project_config.write_text(original_project + "quoting: {database: false, schema: false, identifier: false}\n")
    profile.write_text(original_profile.replace("schema: SCHEMA", "schema: schema"))
    existing = copy.deepcopy(state)
    existing["databases"]["DATABASE"]["schemas"]["SCHEMA"]["relations"]["A"] = {"type": "table", "columns": []}
    result = run("existing_unquoted_schema", select="a", snapshot=existing)
    assert result["generation_completed"], result
    project_config.write_text(original_project)
    profile.write_text(original_profile)

    a.write_text("{{ config(materialized='incremental', incremental_strategy='append') }}\nselect 1 as id\n")
    incremental = copy.deepcopy(state)
    incremental["databases"]["DATABASE"]["schemas"]["SCHEMA"]["relations"]["A"] = {"type": "table", "columns": [{"name": "ID", "data_type": "NUMBER"}]}
    result = run("incremental_missing_projection_metadata", select="a", snapshot=incremental)
    assert not result["generation_completed"]
    assert any(item["reason"].startswith("derived_column_metadata_missing:") for item in result["blockers"])
    create_sql = next(statement["sql"] for statement in result["statements"] if "temporary view" in statement["sql"])
    incremental["derived_columns"] = {hashlib.sha256(create_sql.encode()).hexdigest(): [{"name": "ID", "data_type": "NUMBER(1,0)"}]}
    result = run("incremental_append", select="a", snapshot=incremental)
    assert result["generation_completed"], result
    assert any("insert into" in statement["sql"].lower() for statement in result["statements"])

    a.write_text("select 1 as id\n")
    (project / "seeds").mkdir()
    (project / "seeds/input.csv").write_text("id\n1\n2\n")
    result = run("seed", "seed", "input")
    assert result["generation_completed"], result
    assert any(statement["bindings"] for statement in result["statements"])

    (project / "models/units.yml").write_text("unit_tests:\n  - name: b_unit\n    model: b\n    given:\n      - input: ref('a')\n        format: sql\n        rows: select 1 as id\n    expect:\n      format: sql\n      rows: select 1 as id\n")
    result = run("unit_missing_projection_metadata", "test", "test_type:unit", incremental)
    create_sql = next(statement["sql"] for statement in result["statements"] if "temporary table" in statement["sql"])
    incremental["derived_columns"][hashlib.sha256(create_sql.encode()).hexdigest()] = [{"name": "ID", "data_type": "NUMBER(1,0)"}]
    result = run("unit_query", "test", "test_type:unit", incremental)
    unit_sql = [statement["sql"] for statement in result["statements"] if (statement["node_id"] or "").startswith("unit_test.")]
    assert any("actual_or_expected" in sql for sql in unit_sql), result
    assert not result["generation_completed"] and any(item["stage"] == "result_validation" for item in result["blockers"])
    assert "unit_test.capture_canary.b.b_unit" in result["materializations_generated"]
    (project / "models/units.yml").unlink()

    original_project = (project / "dbt_project.yml").read_text()
    (project / "dbt_project.yml").write_text(original_project + "on-run-end: [\"select {{ results | length }}\"]\n")
    result = run("end_hook_results_blocked", select="a")
    assert not result["generation_completed"]
    assert any(item["reason"] == "query_result_required" for item in result["blockers"])
    (project / "dbt_project.yml").write_text(original_project)

    a.write_text("{% if execute %}{% set rows = run_query('select max(id) from DATABASE.SCHEMA.INPUT') %}{% set value = rows.columns[0].values()[0] %}{% endif %}\nselect 1 as id\n")
    result = run("data_result_blocked", select="a")
    assert not result["generation_completed"]
    assert any(item["reason"] == "query_result_required" for item in result["blockers"])
    assert not any("create or replace" in statement["sql"].lower() for statement in result["statements"])

    a.write_text("{% if execute %}{% call statement('write', fetch_result=False) %}create table DATABASE.SCHEMA.SIDE(id int){% endcall %}{% set n = load_result('write').response.rows_affected %}{% do run_query('select 999') %}{% endif %}\nselect 1 as id\n")
    result = run("response_blocked", select="a")
    assert not result["generation_completed"]
    assert any("statement_response_required:rows_affected" == item["reason"] for item in result["blockers"])
    assert not any("select 999" in statement["sql"] for statement in result["statements"])

    a.write_text("select 1 as id\n")
    unknown = copy.deepcopy(state)
    unknown["databases"] = {}
    result = run("metadata_blocked", select="a", snapshot=unknown)
    assert not result["generation_completed"]
    assert any("metadata_missing" in item["reason"] for item in result["blockers"])
    assert all("connection_open_forbidden" not in item["reason"] for record in records.values() for item in record["blockers"])
    print(json.dumps({"status": "passed", "cases": list(records)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    verify(str(args.python.absolute()), args.directory.resolve())
