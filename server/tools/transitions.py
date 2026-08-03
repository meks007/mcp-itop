"""
State transition tools: Describe_state_change, Apply_stimulus_to_object.

Replaces the Apply_stimulus_to_object tool previously registered in crud.py.

Design
------
Both tools share a common lookup pattern:

  1. Fetch the normalised transition schema for obj_class via
     get_transition_map() (cached per class, TTL from TRANSITION_CACHE_TTL).
     The schema contains only user-action and selected internal stimuli, and
     only fields with a required option (MANDATORY, MUSTPROMPT, MUSTCHANGE).

  2. Optionally read the current status of the ticket via a core/get call
     (only when obj_id is provided -- object mode).

  3. In schema mode (no obj_id), traverse the graph with DFS to find:
       a. Complete paths: reach target_state through user actions.
       b. Partial paths: reach a dead end before target_state because no
          further user-action or exposed internal stimulus is available
          (e.g. the ticket is now waiting for an internal system event).

  4. Consolidate required fields PER PATH from every visited state, not
     just the target state. Fields from each state along the path are merged;
     duplicate field names accumulate their option flags.

  5. In object mode (obj_id provided), read the current state, verify that
     target_state is directly reachable via a user-action stimulus, and return
     only the fields required for that single immediate transition.

Stimulus classification
-----------------------
  StimulusUserAction  -- agent can invoke via Apply_stimulus_to_object.
  StimulusInternal    -- system-driven; shown in path output as [internal]
                         but cannot be executed by Apply_stimulus_to_object.
                         Only included when listed in EXPOSED_INTERNAL_STIMULI
                         (see cache.py).

Path modes (schema mode only)
------------------------------
  "usual" (default) -- up to MAX_USUAL_PATHS complete paths, scored and
                       deduplicated by agent-visible stimulus sequence.
                       Remaining slots filled with partial paths.
  "all"             -- all complete paths then all partial paths, no limit.
"""

from __future__ import annotations

from client import ItopClient
from cache import get_transition_map
from helpers import resolve_key, format_and_cache, ensure_ref_field


MAX_USUAL_PATHS = 10

# Required option flags used when consolidating path fields.
# Mirrors REQUIRED_FIELD_OPTIONS in cache.py; duplicated here so transitions.py
# has no import dependency on the cache constants.
_REQUIRED_OPTIONS: frozenset[str] = frozenset({
    "OPT_ATT_MANDATORY",
    "OPT_ATT_MUSTPROMPT",
    "OPT_ATT_MUSTCHANGE",
})


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


def _is_internal(from_state: str, stimulus: str, to_state: str, transitions: dict) -> bool:
    """Return True when the stimulus is system-driven (StimulusInternal)."""
    stim_entry = (
        transitions
        .get(from_state, {})
        .get("targets", {})
        .get(to_state, {})
    )
    return stim_entry.get(stimulus, {}).get("type", "") == "StimulusInternal"


def _format_path(path: list[tuple[str, str, str]], transitions: dict) -> str:
    """Format a path as a readable string; internal stimuli are labelled."""
    parts = []
    for from_state, stimulus, to_state in path:
        marker = " [internal]" if _is_internal(from_state, stimulus, to_state, transitions) else ""
        parts.append(from_state + " -[" + stimulus + marker + "]-> " + to_state)
    return " | ".join(parts)


def _format_path_fields(
    start: str,
    path: list[tuple[str, str, str]],
    transitions: dict,
    fields_def: dict,
) -> list[str]:
    """Return required fields consolidated across every state visited by path.

    Visits the start state and every destination state in order. For each
    state, collects fields that carry at least one required option flag
    (MANDATORY, MUSTPROMPT, MUSTCHANGE). Duplicate field names accumulate
    their option flags across states (e.g. team_id required in both 'new'
    and 'assigned' merges into a single entry with combined flags).

    Fields without any required option are excluded (they were already
    stripped by cache.py normalisation, but this guard is kept for safety).
    """
    required_options = _REQUIRED_OPTIONS
    state_names = [start] + [to_state for _, _, to_state in path]
    collected: dict[str, set[str]] = {}

    for state_name in state_names:
        state_fields = transitions.get(state_name, {}).get("fields", {})
        if not isinstance(state_fields, dict):
            continue
        for field_name, field_opts in state_fields.items():
            options = (
                field_opts.get("options", [])
                if isinstance(field_opts, dict)
                else []
            )
            relevant = set(options) & required_options
            if relevant:
                collected.setdefault(field_name, set()).update(relevant)

    if not collected:
        return ["  (no required fields along this path)"]

    lines = []
    for field_name, options in collected.items():
        flags = [
            opt.replace("OPT_ATT_", "")
            for opt in ("OPT_ATT_MANDATORY", "OPT_ATT_MUSTPROMPT", "OPT_ATT_MUSTCHANGE")
            if opt in options
        ]
        fd = fields_def.get(field_name, {})
        hint = _build_hint(fd.get("type", ""), fd.get("values", {}))
        lines.append("  " + field_name + " [" + ", ".join(flags) + "]")
        lines.append("    -> " + hint)
    return lines


def _format_state_fields(state: str, transitions: dict, fields_def: dict) -> list[str]:
    """Return required fields for a single state (used in object mode)."""
    state_fields = transitions.get(state, {}).get("fields", {})
    if not isinstance(state_fields, dict) or not state_fields:
        return ["  (no required fields)"]

    lines = []
    for field_name, field_opts in state_fields.items():
        options = (
            field_opts.get("options", [])
            if isinstance(field_opts, dict)
            else []
        )
        relevant = set(options) & _REQUIRED_OPTIONS
        if not relevant:
            continue
        flags = [
            opt.replace("OPT_ATT_", "")
            for opt in ("OPT_ATT_MANDATORY", "OPT_ATT_MUSTPROMPT", "OPT_ATT_MUSTCHANGE")
            if opt in relevant
        ]
        fd = fields_def.get(field_name, {})
        hint = _build_hint(fd.get("type", ""), fd.get("values", {}))
        lines.append("  " + field_name + " [" + ", ".join(flags) + "]")
        lines.append("    -> " + hint)
    return lines if lines else ["  (no required fields)"]


# ---------------------------------------------------------------------------
# Path finding
# ---------------------------------------------------------------------------

def _find_paths(
    transitions: dict,
    start: str,
    target: str,
) -> tuple[
    list[list[tuple[str, str, str]]],
    list[list[tuple[str, str, str]]],
]:
    """Find complete and partial paths from start toward target.

    A complete path reaches target_state via one or more user-action or
    exposed internal stimulus edges without revisiting any state.

    A partial path ends at a state that has no further outgoing edges in
    the normalised schema (i.e. only system-driven edges were available
    and none are in EXPOSED_INTERNAL_STIMULI). The partial path has not
    yet reached target_state.

    Returns:
        (complete, partial) -- each a list of paths.
        Each path is a list of (from_state, stimulus, to_state) steps.
    """
    complete: list[list[tuple[str, str, str]]] = []
    partial: list[list[tuple[str, str, str]]] = []

    def dfs(
        current: str,
        path: list[tuple[str, str, str]],
        visited: set[str],
    ) -> None:
        targets = transitions.get(current, {}).get("targets", {})

        if not targets:
            # Dead end before reaching target -- record as partial if we moved
            if path:
                partial.append(path)
            return

        advanced = False
        for next_state, stim_map in targets.items():
            if next_state in visited:
                continue
            if not isinstance(stim_map, dict) or not stim_map:
                continue

            stimulus = list(stim_map.keys())[0]
            step = (current, stimulus, next_state)
            next_path = path + [step]

            advanced = True
            if next_state == target:
                complete.append(next_path)
            else:
                dfs(next_state, next_path, visited | {next_state})

        # If every outgoing edge was to an already-visited state and we have
        # not reached target, treat current position as a dead end.
        if not advanced and path:
            partial.append(path)

    dfs(start, [], {start})
    return complete, partial


# ---------------------------------------------------------------------------
# Path selection and scoring
# ---------------------------------------------------------------------------

def _external_signature(
    path: list[tuple[str, str, str]],
    transitions: dict,
) -> tuple[str, ...]:
    """Return the agent-visible (non-internal) stimulus sequence for a path.

    Used to deduplicate paths that share the same user-action sequence but
    differ only in which system-driven side steps are included.
    """
    return tuple(
        stimulus
        for from_state, stimulus, to_state in path
        if not _is_internal(from_state, stimulus, to_state, transitions)
    )


def _score_path(
    path: list[tuple[str, str, str]],
    transitions: dict,
) -> tuple[int, int]:
    """Return a sort key for a path. Lower is better.

    Criteria:
      1. Number of internal steps (prefer fewer).
      2. Total path length (prefer shorter).
    """
    internal_count = sum(
        1 for from_state, stimulus, to_state in path
        if _is_internal(from_state, stimulus, to_state, transitions)
    )
    return (internal_count, len(path))


def _select_paths(
    paths: list[tuple[str, list[tuple[str, str, str]]]],
    transitions: dict,
    limit: int,
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """Return at most limit deduplicated paths, best-scored first.

    Deduplication key: (start_state, external_signature).
    Within the same signature the shortest / fewest-internal-step path wins.
    """
    sorted_paths = sorted(
        paths,
        key=lambda item: _score_path(item[1], transitions),
    )
    selected: list[tuple[str, list[tuple[str, str, str]]]] = []
    seen: set[tuple[str, ...]] = set()

    for start, path in sorted_paths:
        sig = (start,) + _external_signature(path, transitions)
        if sig in seen:
            continue
        seen.add(sig)
        selected.append((start, path))
        if len(selected) >= limit:
            break

    return selected


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
        path_mode: str = "usual",
    ) -> str:
        """Describe how to transition an iTop object to a given target state.

        Two modes depending on whether obj_id is provided:

        WITHOUT obj_id (schema mode):
          Traverses the class lifecycle graph from current_state (or all
          states when current_state is omitted) and returns:
            - Complete paths: reach target_state through user actions.
            - Partial paths: reach a dead end before target_state because no
              further user action is available at that point (e.g. the ticket
              must wait for a system event such as approval).
          Each path is accompanied by its own consolidated list of required
          fields, merged from every state visited along that path.
          path_mode controls the output volume:
            "usual" (default) -- up to 10 paths (complete first, then partial).
            "all"             -- all complete paths then all partial paths.

        WITH obj_id (object mode):
          Reads the current state of the specific object, verifies that
          target_state is directly reachable via a user-action stimulus, and
          returns the single immediate transition with its required fields.
          path_mode is ignored in this mode.

        Parameters:
          obj_class     - iTop class name, e.g. UserRequest
          target_state  - desired target state, e.g. resolved, assigned
          obj_id        - optional ticket ref or numeric ID (e.g. R-001234)
          current_state - optional starting state for schema-mode search
          path_mode     - "usual" (default) or "all"; ignored when obj_id set

        Returns:
          - Reachable paths from current to target state (schema mode), or
            single direct transition (object mode)
          - Required fields consolidated per path (schema) or per target (object)
          - [internal] label on system-driven edges
          - Hint to provide obj_id when the result list is capped
        """
        if path_mode not in {"usual", "all"}:
            return "Error: path_mode must be \"usual\" or \"all\"."

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
                    "Error: target_state '" + target_state + "' is not directly reachable "
                    "from current state '" + cur + "' via a user action.\n"
                    "Directly reachable states from '" + cur + "': " + valid
                )

            stim_map = reachable[target_state]
            stimulus = list(stim_map.keys())[0]
            stype = stim_map[stimulus].get("type", "") if isinstance(stim_map[stimulus], dict) else ""

            if stype != "StimulusUserAction":
                return (
                    "Error: transition to '" + target_state + "' uses an internal stimulus '"
                    + stimulus + "' and cannot be applied by this tool. "
                    "iTop triggers it automatically."
                )

            lines = [
                "Transition:  " + cur + " -> " + target_state,
                "Stimulus:    " + stimulus,
                "",
                "Required fields for target state '" + target_state + "':",
            ]
            lines += _format_state_fields(target_state, transitions, fields_def)
            lines += [
                "",
                "Call Apply_stimulus_to_object with:",
                "  obj_class    = " + resolved_class,
                "  obj_id       = " + obj_id,
                "  target_state = " + target_state,
                "  field_lines  =",
            ]
            state_fields = transitions.get(target_state, {}).get("fields", {})
            if isinstance(state_fields, dict):
                for field_name, field_opts in state_fields.items():
                    opts = field_opts.get("options", []) if isinstance(field_opts, dict) else []
                    if set(opts) & _REQUIRED_OPTIONS:
                        lines.append("    " + field_name + "=<value>")

            return "\n".join(lines)

        # ----------------------------------------------------------------
        # Schema mode: no obj_id
        # ----------------------------------------------------------------
        all_states = list(transitions.keys())

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

        all_complete: list[tuple[str, list[tuple[str, str, str]]]] = []
        all_partial: list[tuple[str, list[tuple[str, str, str]]]] = []

        for start in start_states:
            if start == target_state:
                continue
            c, p = _find_paths(transitions, start, target_state)
            all_complete.extend((start, path) for path in c)
            all_partial.extend((start, path) for path in p)

        if not all_complete and not all_partial:
            return (
                "No paths found to target_state '" + target_state + "'"
                + (" from '" + current_state + "'" if current_state else "")
                + "."
            )

        if path_mode == "all":
            shown_complete = all_complete
            shown_partial = all_partial
        else:
            shown_complete = _select_paths(all_complete, transitions, MAX_USUAL_PATHS)
            remaining = MAX_USUAL_PATHS - len(shown_complete)
            shown_partial = (
                _select_paths(all_partial, transitions, remaining)
                if remaining > 0
                else []
            )

        total_complete = len(all_complete)
        total_partial = len(all_partial)
        shown_total = len(shown_complete) + len(shown_partial)
        grand_total = total_complete + total_partial

        prefix = (
            "Paths to '" + target_state + "'"
            + (" from '" + current_state + "'" if current_state else "")
            + " (" + str(shown_total) + " of " + str(grand_total) + " shown):"
        )
        lines = [prefix, ""]

        path_num = 1
        for start, path in shown_complete:
            lines.append("Path " + str(path_num) + ": " + _format_path(path, transitions))
            lines.append("Required fields along this path:")
            lines += _format_path_fields(start, path, transitions, fields_def)
            lines.append("")
            path_num += 1

        for start, path in shown_partial:
            terminal = path[-1][2] if path else start
            lines.append(
                "Partial path " + str(path_num) + ": "
                + _format_path(path, transitions)
            )
            lines.append(
                "Stops at '" + terminal
                + "': no further transition is available in the exposed lifecycle map."
            )
            lines.append("Required fields along this path:")
            lines += _format_path_fields(start, path, transitions, fields_def)
            lines.append("")
            path_num += 1

        if path_mode == "usual" and shown_total < grand_total:
            lines.append(
                "Showing " + str(shown_total) + " representative paths out of "
                + str(grand_total) + " found. Strongly recommended: provide obj_id "
                "for the valid next transition and required fields."
            )

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
        """Apply a lifecycle transition to an iTop object by specifying the
        desired target_state. The correct stimulus is resolved automatically
        from the current state of the object.

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
          1. Verify target_state is directly reachable via a user-action stimulus.
          2. Reject the call when the stimulus is internal (system-driven).
          3. Validate that all MANDATORY fields are present and non-empty.
          4. Apply the stimulus via iTop core/apply_stimulus.
          5. Return the updated object on success, or a descriptive error.

        Use Describe_state_change first if the required fields are unknown.
        """
        schema = await get_transition_map(obj_class, client)
        transitions = schema.get("transitions", {})
        fields_def = schema.get("fields", {})

        obj_class, resolved = await resolve_key(obj_class, obj_id)
        result = await client.get(obj_class, resolved, fields="status")
        current_state = _get_current_state(result, obj_id)
        if not current_state:
            return "Error: object " + obj_id + " not found or status field unavailable."

        current_map = transitions.get(current_state, {})
        reachable = current_map.get("targets", {})

        if target_state not in reachable:
            valid = ", ".join(reachable.keys()) if reachable else "none"
            return (
                "Error: target_state '" + target_state + "' is not reachable from "
                "current state '" + current_state + "'.\n"
                "Reachable states: " + valid
            )

        stim_map = reachable[target_state]
        stimulus = list(stim_map.keys())[0]
        stype = stim_map[stimulus].get("type", "") if isinstance(stim_map[stimulus], dict) else ""

        if stype != "StimulusUserAction":
            return (
                "Error: transition to '" + target_state + "' uses an internal stimulus '"
                + stimulus + "' and cannot be applied by this tool. "
                "iTop triggers it automatically."
            )

        provided = _parse_field_lines(field_lines)

        target_state_map = transitions.get(target_state, {})
        state_fields = target_state_map.get("fields", {})
        target_fields: dict = state_fields if isinstance(state_fields, dict) else {}

        missing: list[str] = []
        for field_name, field_opts in target_fields.items():
            options = (
                field_opts.get("options", [])
                if isinstance(field_opts, dict)
                else []
            )
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

        apply_result = await client.apply_stimulus(
            obj_class,
            resolved,
            stimulus,
            fields=provided,
            output_fields=ensure_ref_field(obj_class, output_fields),
        )
        return format_and_cache(apply_result)
