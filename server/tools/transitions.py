"""
State transition tools: Describe_state_change, Apply_stimulus_to_object.

Replaces the Apply_stimulus_to_object tool previously registered in crud.py.

Design
------
Both tools share a common lookup pattern:

  1. Fetch the transition map for obj_class via get_transition_map()
     (cached per class, TTL from TRANSITION_CACHE_TTL env var).
  2. Read the current status of the ticket via a single core/get call.
  3. Look up schema["transitions"][current_state]["targets"][target_state]
     to resolve the stimulus.
  4. Look up schema["transitions"][target_state]["fields"] to find the
     fields that must be set on the ticket in that state.

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

    Example input:
        agent_id=15
        solution=<p>Fixed by restarting the service.</p>
        # this is a comment

    Returns:
        {"agent_id": "15", "solution": "<p>Fixed by restarting the service.</p>"}
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
    """Return a one-line human-readable hint for a field based on its type.

    Args:
        ftype:   The 'type' value from schema["fields"][field_name].
        fvalues: The 'values' dict for Enum fields; empty for all other types.
    """
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
    """Extract the status field from a core/get result dict.

    Returns the status string, or None when the object was not found or
    the status field is absent.
    """
    objects = result.get("objects") or {}
    if not objects:
        return None
    obj_data = next(iter(objects.values()))
    return obj_data.get("fields", {}).get("status") or None


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
        obj_id: str,
        target_state: str,
    ) -> str:
        """Describe the fields required to transition an iTop object to a target state.

        Provide obj_class, obj_id, and the desired target_state (e.g. "assigned",
        "waiting_for_approval", "queued_human").

        The tool reads the current state of the object, verifies that the
        target_state is reachable from it, and returns:
          - the stimulus that will be applied (e.g. ev_assign)
          - all fields required in the target state, with constraint flags
            (MANDATORY, MUSTPROMPT) and type hints
          - for Class: fields: the iTop class to query with Load_object to find the ID
          - for Enum fields: the list of valid key/label pairs
          - for Value:HTML fields: a reminder to supply HTML markup
          - a ready-to-use example call for Apply_stimulus_to_object

        Call this tool before Apply_stimulus_to_object whenever the required
        fields for a transition are unknown.
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

        # 3. Check reachability
        current_map = transitions.get(current_state, {})
        reachable = current_map.get("targets", {})
        if target_state not in reachable:
            valid = ", ".join(reachable.keys()) if reachable else "none"
            return (
                "Error: target_state '" + target_state + "' is not reachable from "
                "current state '" + current_state + "'.\n"
                "Valid target states from '" + current_state + "': " + valid
            )

        # 4. Resolve stimulus
        stimulus_entry = reachable[target_state]
        stimulus = list(stimulus_entry.keys())[0]
        stimulus_type = stimulus_entry[stimulus].get("type", "")

        # 5. Required fields for target_state
        target_state_map = transitions.get(target_state, {})
        raw_fields = target_state_map.get("fields", {})
        target_fields: dict = raw_fields if isinstance(raw_fields, dict) else {}

        # 6. Build output
        lines = [
            "Transition:  " + current_state + " -> " + target_state,
            "Stimulus:    " + stimulus + " (" + stimulus_type + ")",
            "",
        ]

        if not target_fields:
            lines.append("No additional fields required for this transition.")
        else:
            lines.append("Required fields for target state '" + target_state + "':")
            example_lines: list[str] = []
            for field_name, field_opts in target_fields.items():
                options: list[str] = field_opts.get("options", []) if isinstance(field_opts, dict) else []
                # READONLY fields are informational only -- skip them
                if "OPT_ATT_READONLY" in options:
                    continue
                flags = [o.replace("OPT_ATT_", "") for o in options if o != "OPT_ATT_READONLY"]
                flag_str = ", ".join(flags) if flags else ""

                fd = fields_def.get(field_name, {})
                ftype = fd.get("type", "")
                fvalues = fd.get("values", {})
                hint = _build_hint(ftype, fvalues)

                prefix = "  " + field_name
                if flag_str:
                    prefix += " [" + flag_str + "]"
                lines.append(prefix)
                lines.append("    -> " + hint)
                example_lines.append("    " + field_name + "=<value>")

            lines += [
                "",
                "Call Apply_stimulus_to_object with:",
                "  obj_class    = " + obj_class,
                "  obj_id       = " + obj_id,
                "  target_state = " + target_state,
                "  field_lines  =",
            ] + example_lines

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
            options: list[str] = field_opts.get("options", []) if isinstance(field_opts, dict) else []
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
