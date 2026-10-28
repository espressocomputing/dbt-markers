"""Run the package against a real dbt CLI using a disposable DuckDB project."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.manifest_transport import extract_manifest


def run(dbt, directory, package_ref=None, legacy=False, unit_tests=False):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "compatibility" / "project", directory)
    packages = (
        f"packages:\n  - git: https://github.com/espressocomputing/dbt-markers.git\n"
        f"    revision: '{package_ref}'\n"
        if package_ref
        else f"packages:\n  - local: {root}\n"
    )
    (directory / "packages.yml").write_text(packages)
    (directory / "profiles.yml").write_text(
        "dbt_markers_canary:\n  target: local\n  outputs:\n    local:\n"
        "      type: duckdb\n      path: canary.duckdb\n      schema: canary\n"
        "      threads: 2\n"
    )
    # Enough unselected nodes to exercise multiple metadata statements.
    for index in range(110):
        (directory / "models" / f"extra_{index}.sql").write_text("select 1 as id\n")
    env = {
        **os.environ,
        "DBT_PROFILES_DIR": str(directory),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DBT_LOG_FORMAT": "json",
        "DBT_LOG_LEVEL": "debug",
        "DBT_LOG_LEVEL_FILE": "debug",
        "DO_NOT_TRACK": "1",
    }
    results = {}

    def invoke(label, args, expect_graph=None):
        log = directory / "logs" / "dbt.log"
        log.unlink(missing_ok=True)
        completed = subprocess.run(
            [str(dbt), *args], cwd=directory, env=env, capture_output=True, text=True,
            timeout=180,
        )
        output = completed.stdout + completed.stderr
        (directory / f"{label}.console").write_text(output)
        if completed.returncode:
            raise RuntimeError(f"{label} failed ({completed.returncode}); see {directory}/{label}.console")
        events = []
        if log.exists():
            events = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
            shutil.copyfile(log, directory / f"{label}.jsonl")
        statements = [event["data"]["sql"] for event in events if "sql" in event.get("data", {})]
        sql = "\n".join(statements)
        (directory / f"{label}.sql").write_text(sql)
        if expect_graph is not None:
            assert any('"dbt_markers"' in statement for statement in statements), label
            if expect_graph:
                graph = extract_manifest(sql)
                artifact = json.loads((directory / "target" / "manifest.json").read_text())
                expected = {
                    key: value
                    for collection in ["nodes", "sources", "exposures", "metrics", "semantic_models", "saved_queries"]
                    for key, value in artifact.get(collection, {}).items()
                }
                assert graph["nodes"].keys() == expected.keys(), label
                for key, node in expected.items():
                    assert graph["nodes"][key]["depends_on"] == sorted(node.get("depends_on", {}).get("nodes", [])), key
                assert graph["nodes"]["model.dbt_markers_canary.staging"]["materialized"] == "ephemeral"
                assert "model.dbt_markers_canary.unselected" in graph["nodes"]
                manifest_queries = [i for i, statement in enumerate(statements) if '"dbt_markers_manifest"' in statement]
                model_queries = [i for i, statement in enumerate(statements) if '"node_unique_id": "model.dbt_markers_canary.orders"' in statement]
                if model_queries:
                    assert max(manifest_queries) < min(model_queries), label
                results[label] = {"nodes": len(graph["nodes"]), "chunks": len(manifest_queries), "selected": graph["selected_resources"]}
            else:
                assert '"dbt_markers_manifest"' not in sql, label
                results[label] = "markers_only"
        else:
            results[label] = "passed"
        return sql

    invoke("deps", ["deps"])
    invoke("parse", ["parse"])
    invoke("seed", ["seed"], not legacy)
    invoke("run", ["run", "--select", "orders"], not legacy)
    invoke("cached_run", ["run", "--select", "orders"], not legacy)
    invoke("build", ["build", "--select", "orders"], not legacy)
    if not legacy:
        invoke("off", ["run", "--select", "orders", "--vars", '{"dbt_markers_manifest_mode": "off"}'], False)
        invoke("required", ["run", "--select", "orders", "--vars", '{"dbt_markers_manifest_mode": "required"}'], True)
        if unit_tests:
            (directory / "models" / "unit_tests.yml").write_text(
                "unit_tests:\n  - name: orders_fixture\n    model: orders\n"
                "    given:\n      - input: ref('staging')\n        rows: [{id: 1}]\n"
                "    expect:\n      rows: [{id: 1}]\n"
            )
            sql = invoke("unit_build", ["build", "--select", "orders"])
            assert '"dbt_markers_manifest_status"' in sql or '"dbt_markers_manifest"' in sql
            results["unit_build"] = "markers_only" if '"dbt_markers_manifest_status"' in sql else "graph"
    (directory / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbt", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--package-ref")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--unit-tests", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.dbt.resolve(), args.directory.resolve(), args.package_ref, args.legacy, args.unit_tests)))


if __name__ == "__main__":
    main()
