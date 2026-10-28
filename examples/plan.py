"""Compile a dbt project and compare node plans for a future Nitro consumer."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


SCHEMA = "dbt_markers.plan.v1"
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


def make_plan(manifest, results, selected, context, previous=None):
    if manifest["metadata"]["invocation_id"] != results["metadata"]["invocation_id"]:
        raise ValueError("Manifest and compile results belong to different invocations")
    if results["args"]["which"] != "compile":
        raise ValueError("Planning requires dbt compile artifacts")
    if any(result["status"] != "success" for result in results["results"]):
        raise ValueError("Compilation did not succeed for every result")
    resources = {key: node for collection in COLLECTIONS for key, node in manifest.get(collection, {}).items()}
    if set(selected) - resources.keys():
        raise ValueError("Selected resources missing from manifest")
    macros = manifest.get("macros", {})
    nodes = {}
    for key, node in sorted(resources.items()):
        kind = node["resource_type"]
        if kind == "operation":
            continue
        dependencies = sorted(node.get("depends_on", {}).get("nodes", []))
        reasons = []
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
        if kind in EXECUTABLE and not sql:
            reasons.append("compiled_sql_unavailable")
        if kind not in {"model", "test", "source"}:
            reasons.append("unsupported_resource_type:" + kind)
        if kind == "model" and config.get("materialized") not in {"view", "table", "ephemeral"}:
            reasons.append("runtime_materialization:" + str(config.get("materialized")))
        if config.get("pre-hook") or config.get("post-hook"):
            reasons.append("runtime_hooks")
        record = {
            "resource_type": kind,
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
        record["fingerprint"] = digest(record)
        nodes[key] = record
    missing = {parent for node in nodes.values() for parent in node["depends_on"] if parent not in nodes}
    if missing:
        raise ValueError("Unresolved dependency IDs: " + ", ".join(sorted(missing)))
    metadata = manifest["metadata"]
    context = {**context, "project_name": metadata["project_name"], "adapter_type": metadata["adapter_type"],
               "dbt_version": metadata["dbt_version"], "quoting": metadata.get("quoting"),
               "materialization_macros": {key: digest(macro["macro_sql"]) for key, macro in sorted(macros.items()) if macro["name"].startswith("materialization_")},
               "project_hooks": {key: node.get("raw_code") for key, node in resources.items() if node["resource_type"] == "operation"}}
    context_hash = digest(context)
    fingerprints = {key: node["fingerprint"] for key, node in nodes.items()}
    graph_hash = digest({"context": context_hash, "nodes": fingerprints})
    selected = sorted(set(selected))
    plan = {
        "schema": SCHEMA,
        "invocation_id": metadata["invocation_id"],
        "sql_scope": "compiled_node_body",
        "execution_sql_complete": False,
        "input_state_verified": False,
        "execution_reuse_authorized": False,
        "execution_policy": "Never skip work from plan equality alone; validate inputs and runtime SQL separately.",
        "context": context,
        "context_hash": context_hash,
        "graph_hash": graph_hash,
        "plan_hash": digest({"graph_hash": graph_hash, "selected_resources": selected}),
        "selected_resources": selected,
        "nodes": nodes,
    }
    required = set(selected)
    pending = list(selected)
    while pending:
        for parent in nodes[pending.pop()]["depends_on"]:
            if parent not in required:
                required.add(parent)
                pending.append(parent)
    plan["unresolved_nodes"] = {key: nodes[key]["unresolved"] for key in sorted(required) if nodes[key]["unresolved"]}
    plan["unresolved_context"] = ["project_hooks"] if context["project_hooks"] else []
    plan["compiled_selection_complete"] = not (plan["unresolved_nodes"] or plan["unresolved_context"])
    if previous is not None:
        if previous["schema"] != SCHEMA:
            raise ValueError("Unsupported previous plan schema")
        old = previous["nodes"]
        old_fingerprints = {key: node["fingerprint"] for key, node in old.items()}
        if any(digest({k: v for k, v in node.items() if k != "fingerprint"}) != node["fingerprint"] for node in old.values()):
            raise ValueError("Previous plan node fingerprint mismatch")
        if digest(previous["context"]) != previous["context_hash"] or digest({"context": previous["context_hash"], "nodes": old_fingerprints}) != previous["graph_hash"]:
            raise ValueError("Previous plan graph fingerprint mismatch")
        if digest({"graph_hash": previous["graph_hash"], "selected_resources": previous["selected_resources"]}) != previous["plan_hash"]:
            raise ValueError("Previous plan selection fingerprint mismatch")
    else:
        old = {}
    added = nodes.keys() - old.keys()
    removed = old.keys() - nodes.keys()
    changed = {key for key in nodes.keys() & old.keys() if nodes[key]["fingerprint"] != old[key]["fingerprint"]}
    context_changed = previous is None or previous["context_hash"] != context_hash
    roots = set(nodes) | set(old) if context_changed else added | removed | changed
    # Traverse both graphs: removed edges must invalidate their former descendants.
    edges = {key: {"depends_on": sorted(set(nodes.get(key, {}).get("depends_on", [])) | set(old.get(key, {}).get("depends_on", [])))} for key in nodes.keys() | old.keys()}
    affected = descendants(roots, edges) & nodes.keys()
    blocked = descendants({key for key, node in nodes.items() if node["unresolved"]}, nodes)
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
            if node.get("resource_type") in EXECUTABLE:
                selected.append(node["unique_id"])
        if not selected:
            raise ValueError("Selection contains no executable resources")
        command = [args.dbt, "--quiet", "--write-json", "compile", *common]
        if args.full_refresh:
            command.append("--full-refresh")
        # Compile the whole project; selection is an independent execution mask.
        subprocess.run(command, cwd=project, capture_output=True, text=True, check=True)
        root = Path(temp)
        manifest = json.loads((root / "manifest.json").read_text())
        results = json.loads((root / "run_results.json").read_text())
        context = {"target": args.target, "profile": args.profile, "vars_hash": digest(args.vars),
                   "full_refresh": args.full_refresh, "warehouse_context": args.warehouse_context}
        plan = make_plan(manifest, results, selected, context, previous)
        if args.require_complete and not plan["compiled_selection_complete"]:
            raise ValueError("Selected plan has unresolved nodes/context: " + json.dumps({"nodes": plan["unresolved_nodes"], "context": plan["unresolved_context"]}))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"dbt_plan": plan}, indent=2) + "\n")
        return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbt", default="dbt")
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--target")
    parser.add_argument("--vars", default="{}")
    parser.add_argument("--select")
    parser.add_argument("--exclude")
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--warehouse-context", required=True, help="Non-secret warehouse/account/role identity; changes invalidate the plan")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--require-complete", action="store_true")
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
