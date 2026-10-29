"""Compare captured incremental SQL with real runs in a disposable Snowflake database."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import snowflake.connector
import sqlglot
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.snowflake_catalog import quoted, snapshot

CASES = {
    "merge": ({}, "select 1::number(38,0) as id", []),
    "delete+insert": ({}, "select 1::number(38,0) as id", []),
    "append": ({}, "select 1::number(38,0) as id", []),
    "insert_overwrite": ({}, "select 1::number(38,0) as id", []),
    "append_new_columns": ({"incremental_strategy": "merge", "on_schema_change": "append_new_columns"}, "select 1::number(38,0) as id, 'new'::varchar(20) as label", []),
    "sync_all_columns": ({"incremental_strategy": "merge", "on_schema_change": "sync_all_columns"}, "select 1::number(38,0) as id, 'new'::varchar(20) as label", []),
    "full_refresh": ({"incremental_strategy": "merge"}, "select 1::number(38,0) as id", ["--full-refresh"]),
    "microbatch": ({"incremental_strategy": "microbatch", "event_time": "event_at", "begin": "2026-10-01", "batch_size": "day", "lookback": 0, "concurrent_batches": False}, "select 1::number(38,0) as id, '2026-10-01'::timestamp_ntz as event_at", ["--event-time-start", "2026-10-01", "--event-time-end", "2026-10-03"]),
}


def writes(sql):
    return [node.sql(dialect="snowflake", comments=False) for node in sqlglot.parse(sql, read="snowflake")
            if isinstance(node, (sqlglot.exp.Create, sqlglot.exp.Drop, sqlglot.exp.Alter, sqlglot.exp.Merge, sqlglot.exp.Insert, sqlglot.exp.Delete, sqlglot.exp.Update))]


def verify(args):
    root = args.directory.absolute()
    root.mkdir()
    project = root / "project"
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: strategies\nversion: '1.0'\nconfig-version: 2\nprofile: strategies\n")
    options = json.loads(os.environ[args.connection_env])
    options.update(database=args.database, schema="PUBLIC")
    env = {**os.environ, "DBT_MARKERS_DESCRIBE": json.dumps(options)}
    profile = {"type": "snowflake", "threads": 1}
    for key in ("account", "host", "user", "role", "warehouse", "database", "schema", "password", "private_key", "private_key_file", "private_key_file_pwd", "authenticator", "token"):
        if key in options:
            variable = "DBT_MARKERS_TEST_" + key.upper()
            env[variable] = str(options[key])
            profile[{"private_key_file": "private_key_path", "private_key_file_pwd": "private_key_passphrase"}.get(key, key)] = "{{ env_var('" + variable + "') }}"
    (project / "profiles.yml").write_text(yaml.safe_dump({"strategies": {"target": "live", "outputs": {"live": profile}}}))
    results = {}
    with snowflake.connector.connect(**{key: value for key, value in options.items() if key not in {"database", "schema"}}) as connection:
        cursor = connection.cursor()
        # Fail if the database exists; cleanup must only remove what this run created.
        cursor.execute("create transient database " + quoted(args.database))
        try:
            cursor.execute("use database " + quoted(args.database))
            for name in args.case or CASES:
                extra, sql, flags = CASES[name]
                cursor.execute("create or replace table PUBLIC.A (ID NUMBER(38,0))")
                if name == "microbatch":
                    cursor.execute("alter table PUBLIC.A add column EVENT_AT TIMESTAMP_NTZ")
                if name == "sync_all_columns":
                    cursor.execute("alter table PUBLIC.A add column OLD_FIELD VARCHAR(20)")
                config = {"materialized": "incremental", "incremental_strategy": name, "unique_key": "id", **extra}
                (project / "models/a.sql").write_text("{{ config(" + ", ".join(key + "=" + repr(value) for key, value in config.items()) + ") }}\n" + sql)
                output = root / name
                output.mkdir()
                common = ["--project-dir", str(project), "--profiles-dir", str(project), *flags]
                request = {"command": "run", "warehouse_state": snapshot(connection, {args.database: ["PUBLIC"]}), "describe_connection_env": "DBT_MARKERS_DESCRIBE",
                           "dbt_args": ["--quiet", "--write-json", "run", *common, "--target-path", str(output / "capture-target")]}
                (output / "request.json").write_text(json.dumps(request))
                process = subprocess.run([str(args.python.absolute()), str(Path(__file__).resolve().parents[1] / "examples/capture_core.py"), "--request", str(output / "request.json"), "--output", str(output / "capture.json")], env=env, capture_output=True, text=True)
                (output / "capture.log").write_text(process.stdout + process.stderr)
                assert process.returncode == 0, name
                captured = json.loads((output / "capture.json").read_text())
                assert captured["generation_completed"], captured["blockers"]
                cursor.execute("select count(*) from PUBLIC.A")
                assert cursor.fetchone()[0] == 0, "Capture changed target data"
                actual = subprocess.run([str(args.python.absolute().with_name("dbt")), "--debug", "--log-format", "json", "run", *common, "--target-path", str(output / "actual-target")], env=env, capture_output=True, text=True)
                (output / "actual.jsonl").write_text(actual.stdout + actual.stderr)
                assert actual.returncode == 0, name
                expected = [sql for statement in captured["statements"] for sql in writes(statement["sql"])]
                observed = []
                for line in actual.stdout.splitlines():
                    if not line.startswith("{"):
                        continue
                    event = json.loads(line)
                    if event.get("info", {}).get("name") == "SQLQuery" and event["data"].get("node_info", {}).get("unique_id") == "model.strategies.a":
                        observed.extend(writes(event["data"]["sql"]))
                assert expected == observed, {"expected": expected, "observed": observed}
                results[name] = {"matching_ddl_dml_statements": len(expected), "schema_descriptions": len(captured["generated_column_descriptions"]), "capture_changed_data": False}
                (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
                print(name, "passed", flush=True)
        finally:
            cursor.execute("drop database " + quoted(args.database))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--database", required=True, help="New disposable database; must not exist")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--connection-env", default="SNOWFLAKE_CONNECTION_JSON")
    parser.add_argument("--case", action="append", choices=CASES)
    print(json.dumps(verify(parser.parse_args()), indent=2))
