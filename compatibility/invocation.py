"""Reproduce lost Core runner arguments and verify per-query invocation metadata."""

import argparse
import json
import os
import re
import sys
from importlib.metadata import version
from pathlib import Path

from dbt import flags
from dbt.cli.main import dbtRunner as CoreRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dbt_markers_runner import dbtRunner


def run(directory):
    package = Path(__file__).resolve().parents[1]
    directory.mkdir(parents=True)
    (directory / "models").mkdir()
    (directory / "models" / "orders.sql").write_text("select 1 as id\n")
    (directory / "models" / "other.sql").write_text("select 2 as id\n")
    (directory / "dbt_project.yml").write_text(
        "name: invocation_canary\nversion: '1.0'\nconfig-version: 2\n"
        "profile: invocation_canary\n"
        'query-comment:\n  comment: "{{ dbt_markers.query_comment(node) }}"\n  append: true\n'
    )
    (directory / "profiles.yml").write_text(
        "invocation_canary:\n  target: prd\n  outputs:\n    prd:\n"
        "      type: duckdb\n      path: data.duckdb\n      schema: canary\n      threads: 2\n"
    )
    (directory / "packages.yml").write_text(f"packages:\n  - local: {package}\n")
    (directory / "selectors.yml").write_text(
        "selectors:\n  - name: hourly\n    definition:\n      method: fqn\n      value: orders\n"
    )
    os.chdir(directory)
    os.environ.update(
        DBT_PROFILES_DIR=str(directory), DBT_SEND_ANONYMOUS_USAGE_STATS="false",
        DBT_LOG_LEVEL="debug", DBT_LOG_FORMAT="json", DO_NOT_TRACK="1",
    )
    # A worker process does not put dbtRunner.invoke() arguments in sys.argv.
    sys.argv = ["worker.py"]
    statements = []

    def capture(event):
        if event.info.name == "SQLQuery":
            statements.append(event.data.sql)

    core = CoreRunner(callbacks=[capture])
    assert core.invoke(["deps"]).success

    def invoke(runner, label, argv, **kwargs):
        statements.clear()
        result = runner.invoke(argv, **kwargs)
        assert result.success, result.exception
        markers = []
        for sql in statements:
            for raw in re.findall(r"/\*\s*(\{[\s\S]*?\})\s*\*/", sql):
                meta = json.loads(raw)
                if "dbt_markers" in meta:
                    markers.append(meta)
        assert markers, label
        (directory / f"{label}.sql").write_text("\n".join(statements) + "\n")
        (directory / f"{label}.markers.json").write_text(json.dumps(markers, indent=2) + "\n")
        assert any("node_unique_id" not in meta for meta in markers), label
        assert any(meta.get("node_resource_type") == "model" for meta in markers), label
        return markers

    baseline = invoke(core, "baseline", ["build", "--select", "orders"])
    assert {meta["invocation_command"] for meta in baseline} == {"dbt"}
    assert all("invocation_args" not in meta for meta in baseline)
    runner = dbtRunner(callbacks=[capture])
    original = flags.get_flag_obj
    results = {"dbt_core": version("dbt-core"), "baseline_command": "dbt"}
    cases = [
        ("selected", ["run", "--select", "orders", "--exclude", "other", "--vars", '{"window": "one two"}'], {}),
        ("repeated", ["run", "--select", "orders", "--exclude", "other", "--vars", '{"window": "one two"}'], {}),
        ("other", ["run", "--select", "other"], {}),
        ("selector", ["build", "--selector", "hourly", "--full-refresh"], {}),
        ("kwargs", ["run"], {"select": ["orders"], "exclude": ["other"], "vars": {"window": "kwargs"}}),
    ]
    captured = {}
    for label, argv, kwargs in cases:
        markers = invoke(runner, label, argv, **kwargs)
        assert flags.get_flag_obj is original
        assert sys.argv == ["worker.py"]
        invocation = markers[0]["invocation_args"]
        assert all(meta["invocation_args"] == invocation for meta in markers), label
        assert invocation["project_dir"] == str(directory)
        assert markers[0]["project_name"] == "invocation_canary"
        assert markers[0]["target_name"] == "prd"
        if label == "selector":
            assert invocation["which"] == "build"
            assert invocation["selector"] == "hourly"
            assert invocation["full_refresh"] is True
            assert not invocation["select"]
            assert not invocation["exclude"]
            assert invocation["vars"] == {}
        elif label == "other":
            assert invocation["select"] == ["other"]
            assert not invocation["exclude"]
            assert invocation["vars"] == {}
            assert any(meta.get("node_unique_id") == "model.invocation_canary.other" for meta in markers)
        else:
            assert invocation["which"] == "run"
            assert invocation["select"] == ["orders"]
            assert invocation["exclude"] == ["other"]
            assert invocation["vars"] == {"window": "kwargs" if label == "kwargs" else "one two"}
            assert json.loads(markers[0]["invocation_vars"]) == invocation["vars"]
        captured[label] = markers
        results[label] = {"statements": len(markers), "invocation_args": invocation}
    assert captured["selected"][0]["invocation_args"] == captured["repeated"][0]["invocation_args"]
    assert captured["selected"][0]["invocation_id"] != captured["repeated"][0]["invocation_id"]
    assert not runner.invoke(["not-a-command"]).success
    assert flags.get_flag_obj is original
    unwrapped = invoke(core, "unwrapped-after", ["run", "--select", "orders"])
    assert all("invocation_args" not in meta for meta in unwrapped)
    assert {meta["invocation_command"] for meta in unwrapped} == {"dbt"}
    (directory / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    run(parser.parse_args().directory.resolve())
