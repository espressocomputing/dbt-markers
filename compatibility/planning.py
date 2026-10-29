"""Verify original artifact import against a real dbt compiler in a local fixture."""

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
    (project / "models").mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: import_canary\nversion: '1.0'\nconfig-version: 2\nprofile: import_canary\n")
    (project / "profiles.yml").write_text(f"import_canary:\n  target: local\n  outputs:\n    local:\n      type: duckdb\n      path: {root / 'warehouse.duckdb'}\n      schema: main\n      threads: 1\n")
    (project / "models/a.sql").write_text("select 1 as id, '{{ invocation_id }}' as invocation\n")
    (project / "models/b.sql").write_text("select * from {{ ref('a') }}\n")
    target = root / "target"
    subprocess.run([dbt, "--quiet", "--write-json", "compile", "--project-dir", str(project), "--profiles-dir", str(project), "--target-path", str(target)], check=True, capture_output=True, text=True)
    args = SimpleNamespace(artifacts=target, output=root / "capture.json", select=None, exclude=None,
                           capture_python=None, warehouse_state=None, require_complete=True,
                           warehouse_context="local/import-canary", dbt="/must/not/be/invoked")
    result = compile_capture(args)
    assert result["manifest"] == json.loads((target / "manifest.json").read_text())
    assert result["run_results"] == json.loads((target / "run_results.json").read_text())
    assert result["manifest"]["metadata"]["invocation_id"] in result["manifest"]["nodes"]["model.import_canary.a"]["compiled_code"]
    assert result["manifest"]["nodes"]["model.import_canary.b"]["depends_on"]["nodes"] == ["model.import_canary.a"]
    assert result["execution_capture"] is None
    assert not result["coverage"]["adapter_sql_generated"]
    assert not {"plan_hash", "changes", "execution_edges"} & result.keys()
    records = {"artifact_import": "passed", "dbt_version": result["manifest"]["metadata"]["dbt_version"]}
    (root / "results.json").write_text(json.dumps(records, indent=2) + "\n")
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbt", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(str(Path(args.dbt).absolute()), args.directory.resolve(), args.smoke)))
    except subprocess.CalledProcessError as error:
        print(error.stdout + error.stderr, file=sys.stderr)
        raise
