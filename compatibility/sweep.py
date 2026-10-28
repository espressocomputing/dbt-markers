"""Test published stable dbt versions in isolated environments; retain all evidence."""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path


def releases(package):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json") as response:
        data = json.load(response)
    return sorted(
        (version for version, files in data["releases"].items()
         if re.fullmatch(r"\d+\.\d+\.\d+", version)
         and files and not all(file.get("yanked") for file in files)),
        key=lambda version: tuple(map(int, version.split("."))),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--versions", help="Optional comma-separated Core versions")
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True)
    versions = args.versions.split(",") if args.versions else [
        version for version in releases("dbt-core")
        if (1, 6, 0) <= tuple(map(int, version.split("."))) < (2, 0, 0)
    ]
    groups = {}
    for version in versions:
        groups.setdefault(".".join(version.split(".")[:2]), []).append(version)
    adapters = releases("dbt-duckdb")
    snowflake = releases("dbt-snowflake")
    results = {}
    lock = threading.Lock()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def group(minor, group_versions):
        venv = root / f"core-{minor}"
        subprocess.run([args.uv, "--no-config", "venv", str(venv), "--python", "3.11"], check=True, capture_output=True)
        adapter_minor = minor if any(v.startswith(minor + ".") for v in adapters) else "1.11"
        adapter = [v for v in adapters if v.startswith(adapter_minor + ".")][-1]
        sf = [v for v in snowflake if v.startswith(minor + ".")][-1]
        for version in reversed(group_versions):
            case = root / version
            install = subprocess.run([
                args.uv, "--no-config", "pip", "install", "--python", str(venv / "bin/python"),
                f"dbt-core=={version}", f"dbt-duckdb=={adapter}", f"dbt-snowflake=={sf}",
            ], env=env, capture_output=True, text=True, timeout=180)
            (root / f"{version}.install.log").write_text(install.stdout + install.stderr)
            result = {"adapter": adapter, "snowflake_adapter": sf, "status": "install_failed"}
            if install.returncode == 0:
                freeze = subprocess.run([args.uv, "--no-config", "pip", "freeze", "--python", str(venv / "bin/python")], capture_output=True, text=True, check=True)
                (root / f"{version}.freeze.txt").write_text(freeze.stdout)
                command = [sys.executable, str(Path(__file__).with_name("run.py")), "--dbt", str(venv / "bin/dbt"), "--directory", str(case)]
                if tuple(map(int, minor.split("."))) >= (1, 8):
                    command.append("--unit-tests")
                process = subprocess.run(command, env=env, capture_output=True, text=True, timeout=1200)
                (root / f"{version}.test.log").write_text(process.stdout + process.stderr)
                result["status"] = "passed" if process.returncode == 0 else "failed"
                if process.returncode == 0:
                    result["checks"] = json.loads((case / "results.json").read_text())
            with lock:
                results[version] = result
                (root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
                print(f"{version}: {result['status']} ({len(results)}/{len(versions)})", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(group, minor, values) for minor, values in groups.items()]
        for future in concurrent.futures.as_completed(futures):
            future.result()
    if any(result["status"] != "passed" for result in results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
