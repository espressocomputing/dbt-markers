"""Resolve generated relation schemas with Snowflake's compile-only describe API."""

import json
import os
from threading import Lock

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers


def relation_key(table, dialect):
    return tuple(part.name for part in normalize_identifiers(table.copy(), dialect=dialect).parts)


class GeneratedCatalog:
    def __init__(self, dialect, connection_env=None):
        self.dialect = dialect
        self.connection_env = connection_env
        self.queries = {}
        self.connection = None
        self.descriptions = []
        self.lock = Lock()

    def record(self, statement):
        if isinstance(statement, exp.Create):
            table = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
            query = statement.expression
            while isinstance(query, exp.Subquery):
                query = query.this
            if isinstance(table, exp.Table) and isinstance(query, exp.Query):
                self.queries[relation_key(table, self.dialect)] = query.copy()
        elif isinstance(statement, exp.Drop) and isinstance(statement.this, exp.Table):
            self.queries.pop(relation_key(statement.this, self.dialect), None)

    def expand(self, query, seen=()):
        result = query.copy()
        for table in list(result.find_all(exp.Table)):
            key = relation_key(table, self.dialect)
            if key in self.queries and key not in seen:
                replacement = self.expand(self.queries[key], (*seen, key)).subquery()
                replacement.set("alias", table.args.get("alias") or exp.TableAlias(this=table.this.copy()))
                table.replace(replacement)
        return result

    def describe(self, table):
        key = relation_key(table, self.dialect)
        if not self.connection_env or key not in self.queries:
            return None
        if self.dialect != "snowflake":
            raise ValueError("Live generated-column description currently requires Snowflake")
        query = self.expand(self.queries[key], (key,)).sql(dialect="snowflake")
        import snowflake.connector
        from snowflake.connector.constants import FIELD_ID_TO_NAME
        with self.lock:
            if self.connection is None:
                self.connection = snowflake.connector.connect(**json.loads(os.environ[self.connection_env]))
            with self.connection.cursor() as cursor:
                metadata = cursor.describe(query)
            columns = []
            for column in metadata:
                name = FIELD_ID_TO_NAME[column.type_code]
                if name == "FIXED":
                    dtype = f"NUMBER({column.precision},{column.scale})"
                elif name == "TEXT":
                    dtype = f"VARCHAR({column.internal_size})"
                elif name == "REAL":
                    dtype = "FLOAT"
                elif name in {"DATE", "BOOLEAN", "VARIANT", "OBJECT", "ARRAY", "BINARY", "TIME", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "GEOGRAPHY", "GEOMETRY"}:
                    dtype = name
                else:
                    raise ValueError("Unsupported described Snowflake type: " + name)
                columns.append({"name": column.name, "data_type": dtype})
            self.descriptions.append({"relation": list(key), "query": query, "columns": columns})
            return columns

    def close(self):
        if self.connection is not None:
            self.connection.close()
