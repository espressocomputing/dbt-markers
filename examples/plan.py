"""Capture original dbt artifacts and adapter SQL for server-side planning."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = "dbt_markers.sql_capture.v1"
COLLECTIONS = ("nodes", "sources", "unit_tests", "functions")
EXECUTABLE = {"model", "test", "seed", "snapshot", "unit_test", "function"}


def make_capture(manifest, results, selected, context, capture=None):
    if manifest["metadata"]["invocation_id"] != results["metadata"]["invocation_id"]:
        raise ValueError("Manifest and results belong to different invocations")
    artifact_source = results["args"]["which"]
    if artifact_source != "compile" and (capture is None or artifact_source != capture["command"]):
        raise ValueError("Capture requires compile artifacts or matching capture artifacts")
    effective = capture.get("manifest", manifest) if capture else manifest
    resources = {key: node for collection in COLLECTIONS for key, node in effective.get(collection, {}).items()}
    if set(selected) - resources.keys():
        raise ValueError("Selected resources missing from manifest")
    required, pending = set(selected), list(selected)
    while pending:
        for parent in resources[pending.pop()].get("depends_on", {}).get("nodes", []):
            if parent not in resources:
                raise ValueError("Missing declared dependency: " + parent)
            if parent not in required:
                required.add(parent)
                pending.append(parent)
    available = {key for artifact in (manifest, effective) for collection in COLLECTIONS
                 for key, node in artifact.get(collection, {}).items()
                 if node.get("compiled") and node.get("compiled_code") and node.get("language", "sql") == "sql"}
    missing = {key for key, node in resources.items() if node["resource_type"] in EXECUTABLE and key not in available}
    return {
        "schema": SCHEMA,
        "artifact_source": artifact_source,
        "invocation_id": manifest["metadata"]["invocation_id"],
        "context": context,
        "selected_resources": sorted(set(selected)),
        "manifest": manifest,
        "run_results": results,
        "execution_capture": capture,
        "compilation_errors": {r["unique_id"]: r.get("message") for r in results["results"] if artifact_source == "compile" and r["status"] != "success"},
        "selected_bodies_available": not (missing & required),
        "coverage": {
            "compiled_bodies": {"available": sorted(available), "missing": sorted(missing)},
            "selected_dependency_bodies_missing": sorted(missing & required),
            "adapter_sql_generated": bool(capture and capture["generation_completed"]),
            "compile_time_database_reads": "not_traced",
            "physical_lineage": "not_inferred",
            "execution_order": "adapter_submission_order_only" if capture else "not_captured",
            "wire_sql_complete": False,
        },
    }


def compile_capture(args):
    project = args.project_dir.resolve()
    output = args.output.resolve()
    if output.exists():
        raise ValueError("Output already exists; use a new capture path")
    with tempfile.TemporaryDirectory(prefix="dbt-markers-capture-") as temp:
        common = ["--project-dir", str(project), "--target-path", temp, "--vars", args.vars]
        for name in ("profiles_dir", "target", "profile"):
            value = getattr(args, name)
            if value:
                common.extend(["--" + name.replace("_", "-"), str(value)])
        selection = []
        for name in ("select", "exclude"):
            if getattr(args, name):
                selection.extend(["--" + name, getattr(args, name)])
        listed = subprocess.run([args.dbt, "--quiet", "--write-json", "ls", *common, *selection, "--output", "json"], cwd=project, capture_output=True, text=True, check=True)
        selected = []
        for line in listed.stdout.splitlines():
            if not line.startswith("{"):
                continue
            node = json.loads(line)
            if node.get("resource_type") in ({"model"} if args.command == "run" else {"test", "unit_test"} if args.command == "test" else EXECUTABLE):
                selected.append(node["unique_id"])
        if not selected:
            raise ValueError("Selection contains no executable resources")
        parsed_manifest = json.loads((Path(temp) / "manifest.json").read_text())
        native_checks = None
        if getattr(args, "validate_native_checks", False):
            checked = subprocess.run([args.dbt, "check", *common, *selection], cwd=project, capture_output=True, text=True, check=True)
            native_checks = {"status": "passed", "output": checked.stdout, "scope": "requested selection"}
        command = [args.dbt, "--quiet", "--write-json", "compile", *common]
        if args.full_refresh:
            command.append("--full-refresh")
        # Compile the whole project; selection is an independent execution mask.
        compiled = subprocess.run(command, cwd=project, capture_output=True, text=True, check=not (getattr(args, "allow_partial", False) or args.capture_python))
        root = Path(temp)
        compilation_scope = "whole_project"
        if compiled.returncode and not args.capture_python:
            if not args.select:
                raise ValueError("Partial compilation requires an explicit --select for the component to retain")
            root = root / "selected-component"
            selected_command = list(command)
            selected_command[selected_command.index("--target-path") + 1] = str(root)
            subprocess.run([*selected_command, *selection], cwd=project, capture_output=True, text=True, check=True)
            compilation_scope = "selected_component"
        if compiled.returncode and args.capture_python:
            manifest, results = parsed_manifest, None
            compilation_scope = "capture_selected"
        else:
            manifest = json.loads((root / "manifest.json").read_text())
            results = json.loads((root / "run_results.json").read_text())
        context = {"target": args.target, "profile": args.profile, "vars": args.vars,
                   "full_refresh": args.full_refresh, "warehouse_context": args.warehouse_context,
                   "native_checks_passed": native_checks is not None,
                   "compilation_scope": compilation_scope,
                   "command": args.command}
        capture = None
        if args.capture_python:
            if not args.warehouse_state:
                raise ValueError("--capture-python requires --warehouse-state")
            state = json.loads(args.warehouse_state.read_text())
            if state["adapter_type"] != manifest["metadata"]["adapter_type"]:
                raise ValueError("Warehouse state adapter does not match compile artifacts")
            capture_dir = root / "capture"
            capture_common = list(common)
            capture_common[capture_common.index("--target-path") + 1] = str(capture_dir)
            capture_args = ["--quiet", "--write-json", args.command, *capture_common, *selection]
            for flag in ("event_time_start", "event_time_end"):
                if getattr(args, flag, None):
                    capture_args.extend(["--" + flag.replace("_", "-"), getattr(args, flag)])
            if args.full_refresh:
                capture_args.append("--full-refresh")
            request_path, capture_path = root / "capture-request.json", root / "capture.json"
            request_path.write_text(json.dumps({"warehouse_state": state, "command": args.command, "dbt_args": capture_args, "describe_connection_env": getattr(args, "describe_connection_env", None), "plan_through_test_gates": getattr(args, "plan_through_test_gates", False)}))
            request_path.chmod(0o600)
            subprocess.run([str(args.capture_python.absolute()), str(Path(__file__).with_name("capture_core.py")), "--request", str(request_path), "--output", str(capture_path)], cwd=project, capture_output=True, text=True, check=True)
            capture = json.loads(capture_path.read_text())
            context["warehouse_state"] = state
            context["capture_versions"] = capture["versions"]
            context["event_time_start"] = getattr(args, "event_time_start", None)
            context["event_time_end"] = getattr(args, "event_time_end", None)
            context["conditional_test_gates"] = capture["conditional_test_gates"]
            captured_manifest_path = capture_dir / "manifest.json"
            if captured_manifest_path.exists():
                captured_manifest = json.loads(captured_manifest_path.read_text())
                if captured_manifest["metadata"]["dbt_version"] != manifest["metadata"]["dbt_version"]:
                    raise ValueError("Capture and compilation must use the same dbt version")
                if {key: value["macro_sql"] for key, value in captured_manifest["macros"].items()} != {key: value["macro_sql"] for key, value in manifest["macros"].items()}:
                    raise ValueError("Project macros changed between compilation and capture")
                for collection in COLLECTIONS:
                    if captured_manifest.get(collection, {}).keys() != manifest.get(collection, {}).keys():
                        raise ValueError("Project resources changed between compilation and capture: " + collection)
                    for key, node in captured_manifest.get(collection, {}).items():
                        original = manifest.get(collection, {}).get(key)
                        if original is None or original.get("raw_code") != node.get("raw_code"):
                            raise ValueError("Project changed between compilation and capture: " + key)
                capture["manifest"] = captured_manifest
                captured_results = capture_dir / "run_results.json"
                capture["run_results"] = json.loads(captured_results.read_text()) if captured_results.exists() else None
                if results is None:
                    manifest = captured_manifest
                    captured_results = capture_dir / "run_results.json"
                    if not captured_results.exists():
                        raise ValueError("Capture could not produce capture artifacts after compilation failed")
                    results = json.loads(captured_results.read_text())
            else:
                capture["generation_completed"] = False
        elif args.warehouse_state:
            raise ValueError("--warehouse-state requires --capture-python")
        if results is None:
            raise ValueError("Neither compilation nor capture produced capture artifacts")
        plan = make_capture(manifest, results, selected, context, capture)
        plan["coverage"]["native_v2_check_gates"] = native_checks or "not_verified"
        plan["coverage"]["compilation_scope"] = compilation_scope
        if compilation_scope != "whole_project":
            plan["compilation_errors"]["whole_project"] = compiled.stdout + compiled.stderr
        if args.require_complete and not (capture["generation_completed"] if capture is not None else plan["selected_bodies_available"]):
            raise ValueError("Selected capture is incomplete: " + json.dumps(plan["coverage"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"dbt_capture": plan}, indent=2) + "\n")
        return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbt", default="dbt")
    parser.add_argument("--validate-native-checks", action="store_true", help="Run native dbt check before compiling; unsupported commands or failed checks abort capture")
    parser.add_argument("--plan-through-test-gates", action="store_true", help="Capture downstream SQL conditional on tests passing, without evaluating test outcomes")
    parser.add_argument("--describe-connection-env", help="Snowflake connector options for compile-only generated-column description; same role/session as dbt")
    parser.add_argument("--capture-python", type=Path, help="Python from the matching dbt Core environment")
    parser.add_argument("--warehouse-state", type=Path, help="Explicit metadata/results snapshot for offline SQL capture")
    parser.add_argument("--command", choices=["build", "run", "test"], default="build")
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--target")
    parser.add_argument("--vars", default="{}")
    parser.add_argument("--select")
    parser.add_argument("--exclude")
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--event-time-start", help="Explicit microbatch capture window start")
    parser.add_argument("--event-time-end", help="Explicit microbatch capture window end")
    parser.add_argument("--warehouse-context", required=True, help="Non-secret warehouse/account/role identity for server-side interpretation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--allow-partial", action="store_true", help="Retry only the explicit selected component if whole-project compilation fails; preserve missing bodies as unresolved")
    args = parser.parse_args()
    try:
        plan = compile_capture(args)
    except subprocess.CalledProcessError as error:
        parser.exit(1, error.stdout + error.stderr)
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"schema": plan["schema"], "selected_resources": plan["selected_resources"], "coverage": plan["coverage"]}))


if __name__ == "__main__":
    main()
