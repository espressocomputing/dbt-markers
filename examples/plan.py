"""Compile a dbt project and compare node plans for a future Nitro consumer."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile

import sqlglot
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from examples.plan_graph import DIALECTS, add_hooks, add_lineage, execution_edges
from examples.plan_parameters import comparison_node, parameterize

SCHEMA = "dbt_markers.plan.v3"
COLLECTIONS = ("nodes", "sources", "unit_tests", "functions")
EXECUTABLE = {"model", "test", "seed", "snapshot", "unit_test", "function"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def descendants(roots, nodes):
    children = {}
    for key, node in nodes.items():
        for parent in node["depends_on"]:
            children.setdefault(parent, set()).add(key)
    affected = set(roots)
    pending = list(roots)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in affected:
                affected.add(child)
                pending.append(child)
    return affected


def make_plan(manifest, results, selected, context, previous=None, capture=None, allow_partial=False):
    if manifest["metadata"]["invocation_id"] != results["metadata"]["invocation_id"]:
        raise ValueError("Manifest and compile results belong to different invocations")
    artifact_source = results["args"]["which"]
    if artifact_source != "compile" and capture is None:
        raise ValueError("Planning requires compile artifacts or matching capture artifacts")
    failed_compilations = {result["unique_id"]: result.get("message") for result in results["results"] if result["status"] != "success"} if artifact_source == "compile" else {}
    if failed_compilations and not allow_partial:
        raise ValueError("Compilation did not succeed for every result")
    resources = {key: node for collection in COLLECTIONS for key, node in manifest.get(collection, {}).items()}
    if set(selected) - resources.keys():
        raise ValueError("Selected resources missing from manifest")
    macros = manifest.get("macros", {})
    nodes = {}
    for key, node in sorted(resources.items()):
        kind = node["resource_type"]
        dependencies = sorted(node.get("depends_on", {}).get("nodes", []))
        reasons = ["compilation_failed"] if key in failed_compilations else []
        macro_ids = set()
        pending = list(node.get("depends_on", {}).get("macros", []))
        while pending:
            macro_id = pending.pop()
            if macro_id in macro_ids:
                continue
            macro_ids.add(macro_id)
            if macro_id not in macros:
                reasons.append("missing_macro:" + macro_id)
            else:
                pending.extend(macros[macro_id].get("depends_on", {}).get("macros", []))
        sql = node.get("compiled_code") if node.get("compiled") and node.get("language", "sql") == "sql" else None
        config = node.get("config", {})
        if kind == "operation" and not any(token in node.get("raw_code", "") for token in ("{{", "{%", "{#")):
            sql = node.get("raw_code")
        if kind in EXECUTABLE and not sql:
            reasons.append("compiled_sql_unavailable")
        if kind not in {"model", "test", "source", "operation"} and not (kind == "check" and context.get("native_checks_passed")):
            reasons.append("unsupported_resource_type:" + kind)
        if kind == "model" and config.get("materialized") not in {"view", "table", "ephemeral"}:
            reasons.append("runtime_materialization:" + str(config.get("materialized")))
        if config.get("pre-hook") or config.get("post-hook"):
            reasons.append("runtime_hooks")
        if kind == "operation" and not sql:
            reasons.append("hook_requires_runtime_render")
        generated = [statement for statement in capture["statements"] if statement["node_id"] == key] if capture else []
        generation_complete = key in capture["materializations_generated"] if capture else False
        if capture:
            if generation_complete:
                reasons = [reason for reason in reasons if not reason.startswith(("runtime_materialization:", "runtime_hooks", "hook_requires_runtime_render"))]
                if kind in {"seed", "snapshot", "function", "unit_test"}:
                    reasons = [reason for reason in reasons if reason not in {"compiled_sql_unavailable", "unsupported_resource_type:" + kind}]
            elif key in selected:
                reasons.append("execution_capture_incomplete")
        record = {
            "resource_type": kind,
            "execution_scope": "local_validation" if kind == "check" else "warehouse",
            "generated_statements": generated,
            "sql_generation_complete": generation_complete,
            "package_name": node.get("package_name"),
            "tags": node.get("tags", []),
            "hook_index": node.get("index", 0),
            "depends_on": dependencies,
            "relation_name": node.get("relation_name"),
            "database": node.get("database"),
            "schema": node.get("schema"),
            "identifier": node.get("identifier", node.get("alias")),
            "language": node.get("language"),
            "raw_code": node.get("raw_code"),
            "compiled_sql": sql,
            "config": config,
            "columns": node.get("columns", {}),
            "checksum": node.get("checksum"),
            "test_metadata": node.get("test_metadata"),
            "unit_test_definition": {field: node.get(field) for field in ("given", "expect", "overrides")} if kind == "unit_test" else None,
            "macro_hashes": {macro_id: digest(macros[macro_id]["macro_sql"]) for macro_id in sorted(macro_ids) if macro_id in macros},
            "unresolved": sorted(reasons),
        }
        nodes[key] = record
    add_hooks(nodes)
    if capture:
        for node in nodes.values():
            if node.get("hook_owner") and nodes[node["hook_owner"]]["sql_generation_complete"]:
                node["unresolved"] = []
                node["sql_capture_owner"] = node["hook_owner"]
    add_lineage(nodes, manifest["metadata"]["adapter_type"], context.get("default_database"), context.get("default_schema"))
    execution, execution_selected = execution_edges(nodes, selected, context.get("command", "build"), manifest["metadata"]["project_name"])
    parameter_columns = context.get("runtime_parameter_columns", {})
    dialect = DIALECTS.get(manifest["metadata"]["adapter_type"])
    if context.get("native_checks_passed"):
        checks = [key for key, node in nodes.items() if node["resource_type"] == "check"]
        execution.extend({"from": key, "to": target, "kind": "local_validation_gate"} for key in checks for target in selected if target != key)
        execution_selected = sorted(set(execution_selected) | set(checks))
    for node in nodes.values():
        node["unresolved"] = sorted(set(node["unresolved"]))
        node["runtime_parameters"] = parameterize(node.get("compiled_sql"), parameter_columns, dialect)[1]
        node["comparison_fingerprint"] = digest(comparison_node(node, parameter_columns, dialect))
        node["fingerprint"] = digest(node)
    missing = {parent for node in nodes.values() for parent in node["depends_on"] if parent not in nodes}
    if missing:
        raise ValueError("Unresolved dependency IDs: " + ", ".join(sorted(missing)))
    metadata = manifest["metadata"]
    context = {**context, "sqlglot_version": sqlglot.__version__, "project_name": metadata["project_name"], "adapter_type": metadata["adapter_type"],
               "dbt_version": metadata["dbt_version"], "quoting": metadata.get("quoting"),
               "materialization_macros": {key: digest(macro["macro_sql"]) for key, macro in sorted(macros.items()) if macro["name"].startswith("materialization_")},
               "project_hooks": {key: node.get("raw_code") for key, node in resources.items() if node["resource_type"] == "operation"}}
    context_hash = digest(context)
    fingerprints = {key: node["comparison_fingerprint"] for key, node in nodes.items()}
    graph_hash = digest({"context": context_hash, "nodes": fingerprints})
    selected = sorted(set(selected))
    plan = {
        "schema": SCHEMA,
        "artifact_source": artifact_source,
        "invocation_id": metadata["invocation_id"],
        "compilation_errors": failed_compilations,
        "sql_scope": "compiled_node_body_and_adapter_submissions" if capture else "compiled_node_body",
        "execution_sql_complete": False,
        "selected_sql_generation_complete": bool(capture and capture["generation_completed"]),
        "execution_capture": capture,
        "input_state_verified": False,
        "execution_reuse_authorized": False,
        "execution_policy": "Never skip work from plan equality alone; validate inputs and runtime SQL separately.",
        "context": context,
        "context_hash": context_hash,
        "graph_hash": graph_hash,
        "execution_edges": execution,
        "execution_selected_resources": execution_selected,
        "plan_hash": digest({"graph_hash": graph_hash, "selected_resources": selected, "execution_edges": execution, "execution_selected_resources": execution_selected}),
        "selected_resources": selected,
        "nodes": nodes,
    }
    required = set(execution_selected)
    pending = list(execution_selected)
    while pending:
        for parent in nodes[pending.pop()]["depends_on"]:
            if parent not in required:
                required.add(parent)
                pending.append(parent)
    plan["unresolved_nodes"] = {key: nodes[key]["unresolved"] for key in sorted(required) if nodes[key]["unresolved"] and not (nodes[key]["resource_type"] == "seed" and key not in selected)}
    plan["coverage"] = {
        "compiled_bodies": {"available": sorted(key for key, node in nodes.items() if node.get("compiled_sql")),
                            "missing": sorted(key for key, node in nodes.items() if node["resource_type"] in EXECUTABLE and not node.get("compiled_sql"))},
        "materialization_sql": {"conditional": sorted(capture["materializations_generated"]) if capture else [],
                                "not_captured": sorted(key for key in selected if nodes[key]["resource_type"] != "model" or nodes[key]["config"].get("materialized") != "ephemeral")},
        "unresolved_dependencies": plan["unresolved_nodes"],
        "runtime_validation_required": True,
        "compile_time_database_reads": "not_traced",
        "native_v2_check_gates": "not_verified",
    }
    plan["coverage"]["materialization_sql"]["not_captured"] = sorted(set(plan["coverage"]["materialization_sql"]["not_captured"]) - set(plan["coverage"]["materialization_sql"]["conditional"]))
    plan["unresolved_context"] = []
    plan["compiled_selection_complete"] = not (plan["unresolved_nodes"] or plan["unresolved_context"])
    if previous is not None:
        if previous["schema"] != SCHEMA:
            raise ValueError("Unsupported previous plan schema")
        old = previous["nodes"]
        old_fingerprints = {key: node["comparison_fingerprint"] for key, node in old.items()}
        if any(digest({k: v for k, v in node.items() if k != "fingerprint"}) != node["fingerprint"] for node in old.values()):
            raise ValueError("Previous plan node fingerprint mismatch")
        if any(digest(comparison_node(node, previous["context"].get("runtime_parameter_columns", {}), DIALECTS.get(previous["context"]["adapter_type"]))) != node["comparison_fingerprint"] for node in old.values()):
            raise ValueError("Previous plan comparison fingerprint mismatch")
        if digest(previous["context"]) != previous["context_hash"] or digest({"context": previous["context_hash"], "nodes": old_fingerprints}) != previous["graph_hash"]:
            raise ValueError("Previous plan graph fingerprint mismatch")
        if digest({"graph_hash": previous["graph_hash"], "selected_resources": previous["selected_resources"], "execution_edges": previous["execution_edges"], "execution_selected_resources": previous["execution_selected_resources"]}) != previous["plan_hash"]:
            raise ValueError("Previous plan selection fingerprint mismatch")
    else:
        old = {}
    added = nodes.keys() - old.keys()
    removed = old.keys() - nodes.keys()
    changed = {key for key in nodes.keys() & old.keys() if nodes[key]["comparison_fingerprint"] != old[key]["comparison_fingerprint"]}
    context_changed = previous is None or previous["context_hash"] != context_hash
    roots = set(nodes) | set(old) if context_changed else added | removed | changed
    # Traverse both graphs: removed edges must invalidate their former descendants.
    edges = {key: {"depends_on": sorted(set(nodes.get(key, {}).get("depends_on", [])) | set(old.get(key, {}).get("depends_on", [])))} for key in nodes.keys() | old.keys()}
    for edge in execution + (previous["execution_edges"] if previous else []):
        edges[edge["to"]]["depends_on"].append(edge["from"])
    affected = descendants(roots, edges) & nodes.keys()
    blocked = descendants(set(plan["unresolved_nodes"]), edges)
    plan["changes"] = {
        "context_changed": context_changed,
        "selection_changed": previous is None or previous["selected_resources"] != selected,
        "selection_added": sorted(set(selected) - set(previous["selected_resources"] if previous else [])),
        "selection_removed": sorted(set(previous["selected_resources"] if previous else []) - set(selected)),
        "added": sorted(added), "removed": sorted(removed), "changed": sorted(changed),
        "affected": sorted(affected), "selected_affected": sorted(affected & set(selected)),
        "unchanged": sorted(nodes.keys() - affected),
        "selected_unresolved": sorted(blocked & set(selected)),
    }
    return plan


def compile_plan(args):
    project = args.project_dir.resolve()
    output = args.output.resolve()
    previous = json.loads(args.previous.read_text())["dbt_plan"] if args.previous else None
    if args.previous and output == args.previous.resolve():
        raise ValueError("Use a different output path from the previous plan")
    if output.exists():
        raise ValueError("Output already exists; use a new plan path")
    with tempfile.TemporaryDirectory(prefix="dbt-markers-plan-") as temp:
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
        context = {"target": args.target, "profile": args.profile, "vars_hash": digest(args.vars),
                   "full_refresh": args.full_refresh, "warehouse_context": args.warehouse_context,
                   "native_checks_passed": native_checks is not None,
                   "compilation_scope": compilation_scope,
                   "runtime_parameter_columns": dict(item.split("=", 1) for item in getattr(args, "runtime_parameter_column", [])),
                   "command": args.command, "default_database": args.default_database, "default_schema": args.default_schema}
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
            context["warehouse_state_hash"] = digest({key: value for key, value in state.items() if key != "collected_at"})
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
                        if node.get("compiled"):
                            manifest[collection][key] = node
                if results is None:
                    manifest = captured_manifest
                    captured_results = capture_dir / "run_results.json"
                    if not captured_results.exists():
                        raise ValueError("Capture could not produce planning artifacts after compilation failed")
                    results = json.loads(captured_results.read_text())
            else:
                capture["generation_completed"] = False
        elif args.warehouse_state:
            raise ValueError("--warehouse-state requires --capture-python")
        if results is None:
            raise ValueError("Neither compilation nor capture produced planning artifacts")
        plan = make_plan(manifest, results, selected, context, previous, capture, allow_partial=getattr(args, "allow_partial", False))
        plan["coverage"]["native_v2_check_gates"] = native_checks or "not_verified"
        plan["coverage"]["compilation_scope"] = compilation_scope
        if compilation_scope != "whole_project":
            plan["compilation_errors"]["whole_project"] = compiled.stdout + compiled.stderr
        if args.require_complete and not plan["compiled_selection_complete"]:
            raise ValueError("Selected plan has unresolved nodes/context: " + json.dumps({"nodes": plan["unresolved_nodes"], "context": plan["unresolved_context"]}))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"dbt_plan": plan}, indent=2) + "\n")
        return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbt", default="dbt")
    parser.add_argument("--runtime-parameter-column", action="append", default=[], metavar="COLUMN=PARAMETER", help="Explicit output-literal parameter contract for stable comparison; preserves exact SQL")
    parser.add_argument("--validate-native-checks", action="store_true", help="Run native dbt check before compiling; unsupported commands or failed checks fail planning")
    parser.add_argument("--plan-through-test-gates", action="store_true", help="Capture downstream SQL conditional on tests passing, without evaluating test outcomes")
    parser.add_argument("--describe-connection-env", help="Snowflake connector options for compile-only generated-column description; same role/session as dbt")
    parser.add_argument("--capture-python", type=Path, help="Python from the matching dbt Core environment")
    parser.add_argument("--warehouse-state", type=Path, help="Explicit metadata/results snapshot for offline SQL capture")
    parser.add_argument("--command", choices=["build", "run", "test"], default="build")
    parser.add_argument("--default-database")
    parser.add_argument("--default-schema")
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
    parser.add_argument("--warehouse-context", required=True, help="Non-secret warehouse/account/role identity; changes invalidate the plan")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--allow-partial", action="store_true", help="Retry only the explicit selected component if whole-project compilation fails; preserve missing bodies as unresolved")
    args = parser.parse_args()
    try:
        plan = compile_plan(args)
    except subprocess.CalledProcessError as error:
        parser.exit(1, error.stdout + error.stderr)
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"plan_hash": plan["plan_hash"], "changes": plan["changes"], "unresolved_nodes": plan["unresolved_nodes"]}))


if __name__ == "__main__":
    main()
