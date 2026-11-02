"""Measure startup capture and partial rendering during ordinary native dbt runs."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.manifest_transport import collect_capture, extract_manifest
from examples.partial_render import PartialRenderer


def verify(dbt, root):
    root.mkdir(parents=True)
    (root / "models").mkdir()
    (root / "macros").mkdir()
    package = Path(__file__).resolve().parents[1]
    (root / "dbt_project.yml").write_text("name: upfront\nversion: '1.0'\nconfig-version: 2\nprofile: upfront\nquery-comment:\n  comment: \"{{ dbt_markers.query_comment(node) }}\"\n  append: true\non-run-start:\n  - \"{{ dbt_markers.emit_manifest() }}\"\n")
    (root / "packages.yml").write_text(f"packages:\n  - local: {package}\n")
    (root / "profiles.yml").write_text(f"upfront:\n  target: local\n  outputs:\n    local:\n      type: duckdb\n      path: {root / 'warehouse.duckdb'}\n      schema: main\n      threads: 1\n")
    models = {
        "constant": "{% set x = 3 %}select {{ x + 1 }} as id",
        "loop": "{% set names = ['id', 'n'] %}select {% for name in names %}1 as {{ name }}{% if not loop.last %}, {% endif %}{% endfor %}",
        "reference": "select id from {{ ref('constant') }}",
        "result_value": "{% if execute %}{% set r = run_query('select 7 as n') %}{% set n = r.columns[0].values()[0] %}{% endif %}select {{ n }} as id",
        "variable": "select {{ var('value', 9) }} as id",
        "custom_macro": "select {{ answer() }} as id",
        "ephemeral": "{{ config(materialized='ephemeral') }}select 1 as id",
        "ephemeral_consumer": "select id from {{ ref('ephemeral') }}",
    }
    (root / "macros/answer.sql").write_text("{% macro answer() %}11{% endmacro %}")
    for name, sql in models.items():
        (root / "models" / (name + ".sql")).write_text(sql)
    env = {**os.environ, "DBT_PROFILES_DIR": str(root), "DBT_LOG_LEVEL_FILE": "debug", "DBT_LOG_FORMAT": "json", "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    for command in ["deps", "run"]:
        process = subprocess.run([str(dbt), command], cwd=root, env=env, capture_output=True, text=True)
        (root / (command + ".console")).write_text(process.stdout + process.stderr)
        process.check_returncode()
    events = [json.loads(line) for line in (root / "logs/dbt.log").read_text().splitlines() if line.startswith("{")]
    statements = [e["data"]["sql"] for e in events if "sql" in e.get("data", {})]
    graph = extract_manifest("\n".join(statements))
    observed = collect_capture(statements)
    assert len(observed["statements"]) == len(statements)
    metadata_index = next(i for i, sql in enumerate(statements) if '"dbt_markers_manifest"' in sql)
    result_queries = [i for i, sql in enumerate(statements) if sql.split("/*", 1)[0].strip() == "select 7 as n"]
    assert len(result_queries) == 1 and metadata_index < result_queries[0], result_queries
    manifest = json.loads((root / "target/manifest.json").read_text())
    candidates, partial, updates = [], {}, {}
    for statement in statements:
        for comment in re.findall(r"/\*([\s\S]*?)\*/", statement):
            try:
                marker = json.loads(comment)
            except ValueError:
                continue
            if isinstance(marker, dict) and marker.get("node_compiled_code"):
                updates.setdefault(marker["node_unique_id"], []).append(marker["node_compiled_code"])
    for name in models:
        key = "model.upfront." + name
        rendered = PartialRenderer(graph, key).render()
        if rendered["status"] == "candidate_body":
            assert rendered["sql"] == manifest["nodes"][key]["compiled_code"], (name, rendered)
            candidates.append(name)
        else:
            assert rendered["status"] == "partial_body", rendered
            partial[name] = [f["reason"] for f in rendered["fragments"] if f["kind"] == "unresolved"]
        if name != "ephemeral":
            assert key in updates, name
            assert manifest["nodes"][key]["compiled_code"] in updates[key], name
    assert set(partial) == {"result_value", "variable", "custom_macro", "ephemeral_consumer"}, partial
    assert not graph["coverage"]["extra_compilation_executed"]
    result = {"dbt_version": manifest["metadata"]["dbt_version"], "ordinary_commands": ["dbt deps", "dbt run"],
              "model_count": len(models), "startup_compiled_bodies": sum(bool(n.get("compiled_code")) for n in graph["nodes"].values()),
              "candidate_bodies_matching_native": candidates, "explicit_partial_bodies": partial,
              "observed_compiled_body_nodes": sorted(updates), "query_count": len(statements), "result_query_count": len(result_queries), "result_query_after_startup": True,
              "limits": "Restricted server renderer candidates are not native compilation or executable fusion plans"}
    (root / "graph.json").write_text(json.dumps(graph, indent=2))
    (root / "statements.json").write_text(json.dumps(statements, indent=2))
    (root / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbt", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    verify(args.dbt.absolute(), args.directory.resolve())
