"""Decode one invocation's captured SQL into a proposed Nitro graph config."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def extract_manifest(sql):
    chunks = {}
    identity = None
    count = None
    for comment in re.findall(r"/\*([\s\S]*?)\*/", sql):
        try:
            envelope = json.loads(comment)
        except json.JSONDecodeError:
            continue
        if isinstance(envelope, dict) and "dbt_markers_manifest_status" in envelope:
            raise ValueError(f"Manifest unavailable: {envelope['reason']}")
        if not isinstance(envelope, dict) or "dbt_markers_manifest" not in envelope:
            continue
        if envelope["dbt_markers_manifest"] != 1:
            raise ValueError("Unsupported manifest transport version")
        current = (envelope["invocation_id"], envelope["manifest_id"])
        if identity is not None and current != identity:
            raise ValueError("Input contains multiple manifests; split by invocation and digest")
        identity = current
        total = envelope["chunk_count"]
        index = envelope["chunk_index"]
        data = envelope["data"]
        if type(total) is not int or not 1 <= total <= 4096:
            raise ValueError("Invalid chunk count")
        if type(index) is not int or not 0 <= index < total:
            raise ValueError("Invalid chunk index")
        if not isinstance(data, str) or len(data) > 16000:
            raise ValueError("Invalid chunk data")
        if count is not None and count != total:
            raise ValueError("Inconsistent chunk count")
        if index in chunks and chunks[index] != data:
            raise ValueError("Conflicting duplicate chunk")
        count = total
        chunks[index] = data

    if identity is None or len(chunks) != count:
        raise ValueError("Missing or incomplete manifest")
    payload = "".join(chunks[index] for index in range(count))
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
