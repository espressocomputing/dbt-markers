"""Read an explicit set of Snowflake schemas without executing workload SQL."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(connection, schemas):
    cursor = connection.cursor()
    cursor.execute("select current_account(), current_user(), current_role(), current_warehouse()")
    identity = dict(zip(("account", "user", "role", "warehouse"), cursor.fetchone(), strict=True))
    databases = {}
    for database, requested in schemas.items():
        cursor.execute("show schemas in database " + quoted(database))
        columns = [column[0].lower() for column in cursor.description]
        names = [dict(zip(columns, row, strict=True))["name"] for row in cursor.fetchall()]
        values = {name: {"relations": None} for name in names}
        databases[database] = {"schemas": values}
        for schema in requested:
            if schema not in values:
                raise ValueError("Requested schema is absent or inaccessible: " + database + "." + schema)
            cursor.execute("select table_name, table_type, is_transient from " + quoted(database) +
                           ".information_schema.tables where table_schema = %s order by table_name", (schema,))
            relations = {}
            for name, kind, transient in cursor.fetchall():
                if kind not in {"BASE TABLE", "VIEW"}:
                    raise ValueError("Unsupported catalog relation type: " + kind)
                cursor.execute("describe table " + ".".join(map(quoted, (database, schema, name))))
                columns = [column[0].lower() for column in cursor.description]
                rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
                relations[name] = {"type": "table" if kind == "BASE TABLE" else "view", "is_transient": transient == "YES",
                                   "columns": [{"name": row["name"], "data_type": row["type"]} for row in rows if row["kind"] == "COLUMN"]}
            values[schema]["relations"] = relations
    cursor.close()
    return {"schema": "dbt_markers.warehouse_state.v1", "adapter_type": "snowflake",
            "collected_at": datetime.now(timezone.utc).isoformat(), "identity": identity, "databases": databases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schemas", type=Path, required=True, help='JSON mapping actual database names to schema names')
    parser.add_argument("--connection-env", default="SNOWFLAKE_CONNECTION_JSON", help="Environment variable holding Snowflake connector options")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output must be a new path")
    import snowflake.connector
    with snowflake.connector.connect(**json.loads(os.environ[args.connection_env])) as connection:
        result = snapshot(connection, json.loads(args.schemas.read_text()))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "databases": len(result["databases"])}))
