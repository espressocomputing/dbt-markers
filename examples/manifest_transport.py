"""Decode one invocation's captured SQL into a proposed Nitro graph config."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def extract_manifest(sql):
    payload = None
    identity = None
    for comment in re.findall(r"/\*([\s\S]*?)\*/", sql):
        try:
            envelope = json.loads(comment)
        except json.JSONDecodeError:
            continue
        if isinstance(envelope, dict) and "dbt_markers_manifest_status" in envelope:
            raise ValueError(f"Manifest unavailable: {envelope['reason']}")
        if not isinstance(envelope, dict) or "dbt_markers_manifest" not in envelope:
            continue
        if envelope["dbt_markers_manifest"] != 2:
            raise ValueError("Unsupported manifest transport version")
        current = (envelope["invocation_id"], envelope["manifest_id"])
        if identity is not None and current != identity:
            raise ValueError("Input contains multiple manifests; split by invocation and digest")
        data = envelope["data"]
        if not isinstance(data, str):
            raise ValueError("Invalid manifest data")
        if payload is not None and payload != data:
            raise ValueError("Conflicting duplicate manifest")
        identity = current
        payload = data

    if payload is None:
        raise ValueError("Missing manifest")
    if hashlib.md5(payload.encode()).hexdigest() != identity[1]:
        raise ValueError("Manifest checksum mismatch")
    manifest = json.loads(payload)
    if manifest["schema"] != "dbt_markers.graph.v1":
        raise ValueError("Unsupported graph schema")
    if manifest["invocation_id"] != identity[0]:
        raise ValueError("Invocation mismatch")
    nodes = manifest["nodes"]
    referenced = set(manifest["selected_resources"])
    for node in nodes.values():
        referenced.update(node["depends_on"])
    if referenced - nodes.keys():
        raise ValueError("Graph contains unresolved node IDs")
    return manifest


def collect_capture(statements):
    submissions = [{"sql": item} if isinstance(item, str) else dict(item) for item in statements]
    statements = [item["sql"] for item in submissions]
    graph = extract_manifest("\n".join(statements))
    observations = []
    for sequence, sql in enumerate(statements):
        for comment in re.findall(r"/\*([\s\S]*?)\*/", sql):
            try:
                marker = json.loads(comment)
            except json.JSONDecodeError:
                continue
            if not isinstance(marker, dict) or "dbt_markers" not in marker:
                continue
            if marker.get("invocation_id") != graph["invocation_id"]:
                raise ValueError("Query marker belongs to another invocation")
            if marker.get("node_compiled_code"):
                observations.append({"sequence": sequence, "node_id": marker.get("node_unique_id"),
                                     "compiled_code": marker["node_compiled_code"],
                                     "language": marker.get("node_language")})
    return {"schema": "dbt_markers.observed_capture.v1", "startup_graph": graph,
            "compiled_body_observations": observations,
            "statements": [{**item, "sequence": index} for index, item in enumerate(submissions)],
            "coverage": {"scope": "supplied_proxy_statements", "wire_bindings": "as_supplied", "execution_outcomes": "as_supplied"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql", type=Path, help="Captured SQL for one dbt invocation")
    parser.add_argument("--statement-array", action="store_true", help="Input is a JSON array of SQL strings or proxy submission objects; preserve supplied bindings/outcomes and body observations")
    args = parser.parse_args()
    result = collect_capture(json.loads(args.sql.read_text())) if args.statement_array else {"dbt_manifest": extract_manifest(args.sql.read_text())}
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
