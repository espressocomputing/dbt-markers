"""Verify compile-time writes are captured without executing them."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.plan import compile_capture


def verify(python, root, native_python=None):
    root.mkdir(parents=True)
    project = root / "project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: safe_capture\nversion: '1.0'\nconfig-version: 2\nprofile: safe_capture\n")
    warehouse = root / "warehouse.duckdb"
    (project / "profiles.yml").write_text(f"safe_capture:\n  target: local\n  outputs:\n    local:\n      type: duckdb\n      path: {warehouse}\n      schema: main\n      threads: 1\n")
    write_sql = "create table warehouse.main.must_not_exist(id int)"
    (project / "models/a.sql").write_text("{% if execute %}{% do run_query('" + write_sql + "') %}{% endif %}\nselect 1 as id\n")
    query = "select database_name, type from duckdb_databases()"
    read = subprocess.run([str(python), "-c", "import duckdb,json,sys; c=duckdb.connect(sys.argv[1]); print(json.dumps(c.execute(sys.argv[2]).fetchall())); c.close()", str(warehouse), query], check=True, capture_output=True, text=True)
    state = {"schema": "dbt_markers.warehouse_state.v1", "adapter_type": "duckdb",
             "databases": {"warehouse": {"schemas": {"main": {"relations": {}}}}},
             "query_results": {query: {"columns": ["database_name", "type"], "rows": json.loads(read.stdout), "kind": "metadata"}}}
    constraints = "select table_name, constraint_type from duckdb_constraints()\n                    where lower(database_name) = lower('warehouse')\n                      and lower(schema_name) = lower('main')\n                      and constraint_type in ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY')"
    read_constraints = subprocess.run([str(python), "-c", "import duckdb,json,sys; c=duckdb.connect(sys.argv[1],read_only=True); print(json.dumps(c.execute(sys.argv[2]).fetchall())); c.close()", str(warehouse), constraints], check=True, capture_output=True, text=True)
    state["query_results"][constraints] = {"columns": ["table_name", "constraint_type"], "rows": json.loads(read_constraints.stdout), "kind": "metadata"}
    state_path = root / "state.json"
    state_path.write_text(json.dumps(state))
    args = SimpleNamespace(dbt=str(python.with_name("dbt")), capture_python=python, warehouse_state=state_path,
                           project_dir=project, profiles_dir=project, profile=None, target="local", command="run",
                           vars="{}", select="a", exclude=None, full_refresh=False, warehouse_context="local",
                           require_complete=True, output=root / "capture.json")
    result = compile_capture(args)
    assert any(s["sql"].strip() == write_sql for s in result["compilation_capture"]["statements"])
    assert any(s["sql"].strip() == write_sql for s in result["execution_capture"]["statements"])
    assert result["execution_capture"]["generation_completed"]
    checked = subprocess.run([str(python), "-c", "import duckdb,json,sys; c=duckdb.connect(sys.argv[1],read_only=True); print(json.dumps(c.execute('show tables').fetchall())); c.close()", str(warehouse)], check=True, capture_output=True, text=True)
    assert json.loads(checked.stdout) == [], checked.stdout
    print("compile and materialization writes intercepted; warehouse unchanged", flush=True)
    args.output = root / "rejected.json"
    args.capture_python = None
    try:
        compile_capture(args)
    except ValueError as error:
        assert "compilation is intercepted" in str(error)
    else:
        raise AssertionError("Unintercepted compilation was allowed")
    if native_python:
        args.capture_python = native_python
        try:
            compile_capture(args)
        except ValueError as error:
            assert "Native dbt v2" in str(error)
        else:
            raise AssertionError("Native runtime was passed to Python Core hooks")
    assert not args.output.exists()
    print("unsupported generation rejected before warehouse execution", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--native-python", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    verify(args.python.absolute(), args.directory.resolve(), args.native_python.absolute() if args.native_python else None)
