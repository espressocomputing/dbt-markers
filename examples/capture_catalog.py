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
        self.cached_descriptions = {}
        self.lock = Lock()

    def record(self, statement):
        if isinstance(statement, exp.Create):
            table = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
            query = statement.expression
            while isinstance(query, exp.Subquery):
                query = query.this
            if isinstance(table, exp.Table) and isinstance(query, exp.Query):
                self.queries[relation_key(table, self.dialect)] = query.copy()
        elif isinstance(statement, exp.Alter) and isinstance(statement.this, exp.Table):
            old = relation_key(statement.this, self.dialect)
            for action in statement.args.get("actions", []):
                if isinstance(action, exp.AlterRename):
                    new = relation_key(action.this, self.dialect)
                    if len(new) == 1:
                        new = (*old[:-1], *new)
                    if old in self.queries:
                        self.queries[new] = self.queries.pop(old)
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
        return self.describe_query(self.queries[key], key)

    def describe_query(self, expression, key=()):
        if not self.connection_env:
            return None
        if not isinstance(expression, exp.Query):
            raise ValueError("Only query schemas can be described")
        query = self.expand(expression, (key,) if key else ()).sql(dialect="snowflake")
        import snowflake.connector
        from snowflake.connector.constants import FIELD_ID_TO_NAME
        with self.lock:
            if query in self.cached_descriptions:
                return self.cached_descriptions[query]
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
                elif name == "BINARY":
                    dtype = f"BINARY({column.internal_size})"
                elif name in {"TIME", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ"}:
                    dtype = f"{name}({column.scale})" if column.scale is not None else name
                elif name in {"DATE", "BOOLEAN", "VARIANT", "GEOGRAPHY", "GEOMETRY"}:
                    dtype = name
                else:
                    raise ValueError("Unsupported described Snowflake type: " + name)
                columns.append({"name": column.name, "data_type": dtype})
            self.cached_descriptions[query] = columns
            self.descriptions.append({"relation": list(key), "query": query, "columns": columns})
            return columns

    def close(self):
        if self.connection is not None:
            self.connection.close()
