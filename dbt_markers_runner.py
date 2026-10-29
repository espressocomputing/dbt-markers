"""Expose Core's parsed invocation arguments to per-query markers."""

import json
import shlex

from dbt import flags
from dbt.cli.main import dbtRunner as CoreRunner


_INVOCATION_FIELDS = (
    "which", "project_dir", "profile", "target", "select", "exclude", "selector",
    "vars", "full_refresh", "defer", "state", "defer_state", "favor_state",
    "indirect_selection", "resource_types", "exclude_resource_types", "empty",
)


class dbtRunner(CoreRunner):
    def invoke(self, args, **kwargs):
        original = flags.get_flag_obj

        def invocation_flags():
            result = original()
            parsed = flags.get_flags()
            values = {
                name: getattr(parsed, name.upper())
                for name in _INVOCATION_FIELDS
                if hasattr(parsed, name.upper())
            }
            result.DBT_MARKERS_INVOCATION_ARGS = json.loads(json.dumps(values, default=str))
            result.INVOCATION_COMMAND = shlex.join(["dbt", *args])
            return result

        # Core already uses process-global flags; restore the bridge even on failure.
        flags.get_flag_obj = invocation_flags
        try:
            return super().invoke(args, **kwargs)
        finally:
            flags.get_flag_obj = original
