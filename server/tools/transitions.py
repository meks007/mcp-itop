"""
State transition tools: Describe_state_change, Apply_stimulus_to_object.

Replaces the Apply_stimulus_to_object tool previously registered in crud.py.

Design
------
Both tools share a common lookup pattern:

  1. Fetch the transition map for obj_class via get_transition_map()
     (cached per class, TTL from TRANSITION_CACHE_TTL env var).
  2. Optionally read the current status of the ticket via a single core/get call
     (only when obj_id is provided).
  3. Look up schema["transitions"][current_state]["targets"][target_state]
     to resolve the stimulus.
  4. Look up schema["transitions"][target_state]["fields"] to find the
     fields that must be set on the ticket in that state.

Describe_state_change behaviour:
  - obj_id provided: reads current_state from iTop, validates reachability,
    returns the single matching path.
  - obj_id omitted:  schema-only mode. Finds all paths from any current_state
    to the given target_state via DFS, marks internal stimuli, and returns
    the full path list without an iTop round-trip.

Field types are resolved from schema["fields"] and surfaced as human-readable
hints so the calling agent knows whether to provide a numeric ID, plain text,
HTML, or an enum key.
"""

from __future__ import annotations

from client import ItopClient
from cache import get_transition_map
from helpers import resolve_key, format_and_cache, ensure_ref_field


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_field_lines(field_lines: str) -> dict[str, str]:
    """Parse Key=Value lines into a plain dict.

    Rules:
      - Blank lines are ignored.
      - Lines starting with '#' are treated as comments and ignored.
      - Only the first '=' is used as the delimiter; values may contain '='.
      - Whitespace around the key is stripped; value whitespace is preserved.
    """
    result: dict[str, str] = {}
    for line in (field_lines or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value
    return result


def _build_hint(ftype: str, fvalues: dict) -> str:
    """Return a one-line human-readable hint for a field based on its type."""
    if ftype.startswith("Class:"):
        cls = ftype[len("Class:"):]
        return "numeric ID -- use Load_object(obj_class=" + cls + ") to find the right ID"
    if ftype == "Value:HTML":
        return "HTML text -- e.g. <p>Your text here</p>"
    if ftype == "Value:TEXT":
        return "plain text string"
    if ftype == "Enum" and fvalues:
        options = ", ".join('"' + k + '" (' + v + ")" for k, v in fvalues.items())
        return "one of: " + options
    return "string value"


def _get_current_state(result: dict, obj_id: str) -> str | None:
    """Extract the status field from a core/get result dict."""
    objects = result.get("objects") or {}
    if not objects:
        return None
    obj_data = next(iter(objects.values()))
    return obj_data.get("fields", {}).get("status") or None


def _format_target_fields(target_state: str, transitions: dict, fields_def: dict) -> list[str]:
    """Return formatted lines describing the required fields for target_state."""
    target_state_map = transitions.get(target_state, {})
    raw_fields = target_state_map.get("fields", {})
    target_fields: dict = raw_fields if isinstance(raw_fields, dict) else {}

    if not target_fields:
        return ["  (no additional fields required)"]

    lines = []
    for field_name, field_opts in target_fields.items():
        options: list[str] = (
            field_opts.get("options", []) if isinstance(field_opts, dict) else []
        )
        if "OPT_ATT_READONLY" in options:
            continue
        flags = [o.replace("OPT_ATT_", "") for o in options if o != "OPT_ATT_READONLY"]
        flag_str = "[" + ", ".join(flags) + "]" if flags else ""
        fd = fields_def.get(field_name, {})
        hint = _build_hint(fd.get("type", ""), fd.get("values", {}))
        prefix = "  " + field_name
        if flag_str:
            prefix += " " + flag_str
        lines.append(prefix)
        lines.append("    -> " + hint)
    return lines


def _find_all_paths(
    transitions: dict,
    start: str,
    target: str,
) -> list[list[tuple[str, str, str]]]:
    """Find all non-recursive paths from start to target in the transition graph.

    Returns a list of paths. Each path is a list of steps:
        (from_state, stimulus, to_state)

    Cycles are prevented by tracking visited states per DFS branch.
    """
    results: list[list[tuple[str, str, str]]] = []

    def dfs(
        current: str,
        path: list[tuple[str, str, str]],
        visited: set[str],
    ) -> None:
        state_map = transitions.get(current, {})
        targets = state_map.get("targets", {})
        for next_state, stim_entry in targets.items():
            if next_state in visited:
                continue
            stimulus = list(stim_entry.keys())[0]
            step = (current, stimulus, next_state)
            if next_state == target:
                results.append(path + [step])
            else:
                dfs(next_state, path + [step], visited | {next_state})

    dfs(start, [], {start})
    return results


def _format_path(path: list[tuple[str, str, str]], transitions: dict) -> str:
    """Format a single path as a readable string with internal stimulus markers."""
    parts = []
    for from_state, stimulus, to_state in path:
        stim_entry = transitions.get(from_state, {}).get("targets", {}).get(to_state, {})
        stype = stim_entry.get(stimulus, {}).get("type", "")
        marker = " [internal]" if stype == "StimulusInternal" else ""
        parts.append(from_state + " -[" + stimulus + marker + "]-> " + to_state)
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(mcp, client: ItopClient) -> None:
    """Register state transition tools on the given mcp instance."""

    # ------------------------------------------------------------------ #
    #  TOOL 1: Describe_state_change                                       #
    # ------------------------------------------------------------------ #

    @mcp.tool(name="Describe_state_change")
    async def describe_state_change(
        obj_class: str,
        target_state: str,
        obj_id: str = "",
        current_state: str = "",
    ) -> str:
        """Describe how to transition an iTop object to a given target state.

        Two modes depending on whether obj_id is provided:

        WITHOUT obj_id (schema mode):
          Finds all paths from any state (or from current_state if given) to
          target_state using the class state machine. No iTop call is made.
          Useful for questions like "how do I get from new to resolved?".
          current_state is optional -- when omitted, all paths to target_state
          are shown regardless of starting state.

        WITH obj_id (object mode):
          Reads the current state of the specific object, verifies that
          target_state is reachable, and returns the single matching path
          plus the required fields.

        Parameters:
          obj_class    - iTop class name, e.g. UserRequest
          target_state - desired target state, e.g. resolved, assigned
          obj_id       - optional ticket ref or numeric ID (e.g. R-001234)
          current_state - optional starting state for schema-mode path search

        Returns:
          - All reachable paths from current to target state
          - The stimulus for each step (internal stimuli are marked)
          - Required fields for the target state with type hints
          - A ready-to-use example for Apply_stimulus_to_object (object mode only)
        """
        # 1. Fetch schema (cached per class)
        schema = await get_transition_map(obj_class, client)
        transitions = schema.get("transitions", {})
        fields_def = schema.get("fields", {})

        # ----------------------------------------------------------------
        # Object mode: obj_id provided
        # ----------------------------------------------------------------
        if obj_id and obj_id.strip():
            resolved_class, resolved = await resolve_key(obj_class, obj_id)
            result = await client.get(resolved_class, resolved, fields="status")
            cur = _get_current_state(result, obj_id)
            if not cur:
                return "Error: object " + obj_id + " not found or status field unavailable."

            current_map = transitions.get(cur, {})
            reachable = current_map.get("targets", {})
            if target_state not in reachable:
                valid = ", ".join(reachable.keys()) if reachable else "none"
                return (
                    "Error: target_state '" + target_state + "' is not reachable from "
                    "current state '" + cur + "'.\n"
                    "Valid target states from '" + cur + "': " + valid
                )

            stim_entry = reachable[target_state]
            stimulus = list(stim_entry.keys())[0]
            stype = stim_entry[stimulus].get("type", "")
            marker = " [internal]" if stype == "StimulusInternal" else ""

            lines = [
                "Transition:  " + cur + " -> " + target_state,
                "Stimulus:    " + stimulus + marker,
                "",
                "Required fields for target state '" + target_state + "':",
            ]
            lines += _format_target_fields(target_state, transitions, fields_def)
            lines += [
                "",
                "Call Apply_stimulus_to_object with:",
                "  obj_class    = " + resolved_class,
                "  obj_id       = " + obj_id,
                "  target_state = " + target_state,
                "  field_lines  =",
            ]
            target_state_map = transitions.get(target_state, {})
            raw_fields = target_state_map.get("fields", {})
            tf: dict = raw_fields if isinstance(raw_fields, dict) else {}
            for field_name, field_opts in tf.items():
                opts = field_opts.get("options", []) if isinstance(field_opts, dict) else []
                if "OPT_ATT_READONLY" not in opts:
                    lines.append("    " + field_name + "=<value>")

            return "\n".join(lines)

        # ----------------------------------------------------------------
        # Schema mode: no obj_id
        # ----------------------------------------------------------------
        all_states = list(transitions.keys())

        # Determine which starting states to search from
        if current_state and current_state.strip():
            if current_state not in transitions:
                return (
                    "Error: current_state '" + current_state + "' not found in schema.\n"
                    "Known states: " + ", ".join(all_states)
                )
            start_states = [current_state]
        else:
            start_states = all_states

        if target_state not in transitions:
            return (
                "Error: target_state '" + target_state + "' not found in schema.\n"
                "Known states: " + ", ".join(all_states)
            )

        # Collect all paths
        all_paths: list[tuple[str, list[tuple[str, str, str]]]] = []
        for start in start_states:
            if start == target_state:
                continue
            for path in _find_all_paths(transitions, start, target_state):
                all_paths.append((start, path))

        if not all_paths:
            return (
                "No paths found to target_state '" + target_state + "'"
                + (" from '" + current_state + "'" if current_state else "")
                + "."
            )

        lines = [
            "Paths to '" + target_state + "'"
            + (" from '" + current_state + "'" if current_state else "")
            + " (" + str(len(all_paths)) + " found):",
            "",
        ]
        for i, (start, path) in enumerate(all_paths, 1):
            lines.append("Path " + str(i) + ": " + _format_path(path, transitions))
        lines += [
            "",
            "Required fields for target state '" + target_state + "':",
        ]
        lines += _format_target_fields(target_state, transitions, fields_def)

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  TOOL 2: Apply_stimulus_to_object                                    #
    # ------------------------------------------------------------------ #

    @mcp.tool(name="Apply_stimulus_to_object")
    async def apply_stimulus_to_object(
        obj_class: str,
        obj_id: str,
        target_state: str,
        field_lines: str = "",
        output_fields: str = "ref, friendlyname, status",
    ) -> str:
        """Apply a lifecycle transition to an iTop object by specifying the desired
        target_state. The correct stimulus is resolved automatically from the current
        state of the object.

        Parameters:
          obj_class    - iTop class name, e.g. UserRequest
          obj_id       - numeric object ID or ticket ref (e.g. R-001234)
          target_state - desired target state (e.g. assigned, waiting_for_approval)
          field_lines  - one attribute per line in Key=Value format, e.g.:
                           agent_id=15
                           team_id=7
                           solution=<p>Fixed by restarting the service.</p>
          output_fields - comma-separated fields to return on success
                          (default: ref, friendlyname, status)

        The tool will:
          1. Verify target_state is reachable from the current state.
          2. Validate that all OPT_ATT_MANDATORY fields are present and non-empty.
          3. Apply the stimulus via iTop core/apply_stimulus.
          4. Return the updated object on success, or a descriptive error on failure.

        Use Describe_state_change first if the required fields are unknown.
        Do not use this tool for internal (system-driven) transitions such as
        ev_timeout or ev_autoresolve -- those are triggered by iTop automatically.
        """
        # 1. Fetch schema (cached per class)
        schema = await get_transition_map(obj_class, client)
        transitions = schema.get("transitions", {})
        fields_def = schema.get("fields", {})

        # 2. Read current state from the ticket
        obj_class, resolved = await resolve_key(obj_class, obj_id)
        result = await client.get(obj_class, resolved, fields="status")
        current_state = _get_current_state(result, obj_id)
        if not current_state:
            return "Error: object " + obj_id + " not found or status field unavailable."

        # 3. Verify reachability and resolve stimulus
        current_map = transitions.get(current_state, {})
        reachable = current_map.get("targets", {})
        if target_state not in reachable:
            valid = ", ".join(reachable.keys()) if reachable else "none"
            return (
                "Error: target_state '" + target_state + "' is not reachable from "
                "current state '" + current_state + "'.\n"
                "Valid target states: " + valid
            )

        stimulus_entry = reachable[target_state]
        stimulus = list(stimulus_entry.keys())[0]

        # 4. Parse field_lines -> dict
        provided = _parse_field_lines(field_lines)

        # 5. Required fields for target_state
        target_state_map = transitions.get(target_state, {})
        raw_fields = target_state_map.get("fields", {})
        target_fields: dict = raw_fields if isinstance(raw_fields, dict) else {}

        # 6. Validate mandatory fields
        missing: list[str] = []
        for field_name, field_opts in target_fields.items():
            options: list[str] = (
                field_opts.get("options", []) if isinstance(field_opts, dict) else []
            )
            if "OPT_ATT_READONLY" in options:
                continue
            if "OPT_ATT_MANDATORY" in options:
                if not provided.get(field_name, "").strip():
                    missing.append(field_name)

        if missing:
            return (
                "Error: missing mandatory fields for transition to '"
                + target_state + "':\n"
                + "\n".join("  - " + f for f in missing)
                + "\n\nCall Describe_state_change to see all required fields."
            )

        # 7. Apply stimulus
        apply_result = await client.apply_stimulus(
            obj_class,
            resolved,
            stimulus,
            fields=provided,
            output_fields=ensure_ref_field(obj_class, output_fields),
        )
        return format_and_cache(apply_result)
