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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql", type=Path, help="Captured SQL for one dbt invocation")
    args = parser.parse_args()
    json.dump({"dbt_manifest": extract_manifest(args.sql.read_text())}, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
