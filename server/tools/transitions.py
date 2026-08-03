"""
State transition tools: Describe_state_change, Apply_stimulus_to_object.

Design
------
Both tools share a common lookup pattern:

  1. Fetch the normalised transition schema for obj_class via
     get_transition_map() (cached per class, TTL from TRANSITION_CACHE_TTL).
     The schema contains every transition returned by the REST API and only
     fields with a required option (MANDATORY, MUSTPROMPT, MUSTCHANGE).

  2. Optionally read the current status of the ticket via a core/get call
     (only when obj_id is provided -- object mode).

  3. In schema mode (no obj_id), first compute which states can reach the
     requested target_state via reverse-reachability (BFS from target
     walking edges backwards). Then run a forward DFS restricted to that
     reachable set, so the path finder never wanders into branches that
     cannot lead to target_state (e.g. it will not follow resolved->closed
     when searching for approved).

  4. Consolidate required fields PER PATH from every visited state, not
     just the target state. Fields from each state are merged; duplicate
     field names accumulate their option flags.

  5. In object mode (obj_id provided), the current state is read from iTop
     and used as the fixed starting point. Path finding then behaves
     identically to schema mode with current_state set. This means multi-
     step paths are shown when target_state is not directly reachable.
     Apply_stimulus_to_object still only accepts a single direct transition.

Stimulus classification
-----------------------
  StimulusUserAction  -- agent can invoke via Apply_stimulus_to_object.
  StimulusInternal    -- system-driven; shown in path output as [internal]
                         but cannot be executed by Apply_stimulus_to_object.

Path modes (schema mode only)
------------------------------
  "usual" (default) -- up to MAX_USUAL_PATHS paths, scored and deduplicated
                       by agent-visible (non-internal) stimulus sequence.
  "all"             -- all paths, no limit.
"""

from __future__ import annotations

import json as _json

from client import ItopClient
from cache import get_transition_map
from helpers import resolve_key, format_and_cache, ensure_ref_field


MAX_USUAL_PATHS = 10

_REQUIRED_OPTIONS: frozenset[str] = frozenset({
    "OPT_ATT_MANDATORY",
    "OPT_ATT_MUSTPROMPT",
    "OPT_ATT_MUSTCHANGE",
})

_ID_FIELDS_NOTE = "Note: for ID fields, load the referenced object only if the ID is not yet known."

# Values that iTop uses for unset/empty fields (foreign keys default to 0).
_EMPTY_VALUES: frozenset[str] = frozenset({"", "0", "null", "None"})


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

    Fallback: if the input starts with '{' it is assumed to be a JSON object
    and parsed directly. This handles agents that pass JSON instead of
    Key=Value text despite the docstring instruction. Each value is cast to
    str so the returned type is always dict[str, str].
    """
    raw = (field_lines or "").strip()

    if raw.startswith("{"):
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                return {k: str(v) for k, v in parsed.items()}
        except Exception:
            pass  # fall through to line-by-line parsing

    result: dict[str, str] = {}
    for line in raw.splitlines():
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
        return "ID (" + cls + ")"
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


def _get_current_fields(result: dict) -> dict[str, str]:
    """Extract the fields dict from a core/get result dict.

    Returns an empty dict when the result contains no objects.
    All values are cast to str so callers can compare uniformly.
    """
    objects = result.get("objects") or {}
    if not objects:
        return {}
    obj_data = next(iter(objects.values()))
    raw = obj_data.get("fields", {}) or {}
    return {k: str(v) for k, v in raw.items()}


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
    state, collects fields that carry at least one required option flag.
    Duplicate field names accumulate their option flags across states.
    """
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
            relevant = set(options) & _REQUIRED_OPTIONS
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
# Reverse reachability
# ---------------------------------------------------------------------------

def _states_reaching_target(transitions: dict, target: str) -> set[str]:
    """Return every state that has at least one path leading to target.

    Builds a reverse adjacency map (to_state -> set of from_states) and
    walks backwards from target via BFS. Only states in the returned set
    can possibly reach target through the exposed lifecycle graph.

    This is used to prune the forward DFS so that branches leading to
    genuine terminal states (e.g. closed) are never followed when they
    cannot continue toward the requested target_state.
    """
    predecessors: dict[str, set[str]] = {}
    for from_state, state_map in transitions.items():
        targets = state_map.get("targets", {})
        if not isinstance(targets, dict):
            continue
        for to_state in targets:
            predecessors.setdefault(to_state, set()).add(from_state)

    reachable: set[str] = {target}
    pending: list[str] = [target]
    while pending:
        current = pending.pop()
        for prev in predecessors.get(current, set()):
            if prev not in reachable:
                reachable.add(prev)
                pending.append(prev)

    return reachable


# ---------------------------------------------------------------------------
# Path finding
# ---------------------------------------------------------------------------

def _find_paths(
    transitions: dict,
    start: str,
    target: str,
) -> list[list[tuple[str, str, str]]]:
    """Find all non-cyclic paths from start to target.

    Before traversal, computes the reverse-reachable set for target so that
    the DFS never follows edges into states that cannot lead to target.
    This eliminates spurious branches such as resolved->closed when searching
    for a state that closed cannot reach.

    Returns a list of complete paths. Each path is a list of steps:
        (from_state, stimulus, to_state)
    """
    reachable = _states_reaching_target(transitions, target)

    results: list[list[tuple[str, str, str]]] = []

    def dfs(
        current: str,
        path: list[tuple[str, str, str]],
        visited: set[str],
    ) -> None:
        state_targets = transitions.get(current, {}).get("targets", {})
        for next_state, stim_map in state_targets.items():
            if next_state in visited:
                continue
            if next_state not in reachable:
                continue
            if not isinstance(stim_map, dict) or not stim_map:
                continue

            stimulus = list(stim_map.keys())[0]
            step = (current, stimulus, next_state)
            next_path = path + [step]

            if next_state == target:
                results.append(next_path)
            else:
                dfs(next_state, next_path, visited | {next_state})

    dfs(start, [], {start})
    return results


# ---------------------------------------------------------------------------
# Path selection and scoring
# ---------------------------------------------------------------------------

def _external_signature(
    path: list[tuple[str, str, str]],
    transitions: dict,
) -> tuple[str, ...]:
    """Return the agent-visible (non-internal) stimulus sequence for a path.

    Used to deduplicate paths that share the same user-action sequence but
    differ only in which internal edges are traversed along the way.
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
# Shared path output builder
# ---------------------------------------------------------------------------

def _render_paths(
    shown_paths: list[tuple[str, list[tuple[str, str, str]]]],
    all_paths: list[tuple[str, list[tuple[str, str, str]]]],
    target_state: str,
    start_label: str,
    transitions: dict,
    fields_def: dict,
    path_mode: str,
) -> str:
    """Render the standard multi-path output block used by both modes."""
    total = len(all_paths)
    shown = len(shown_paths)

    lines = [
        "Paths to '" + target_state + "'"
        + (" from '" + start_label + "'" if start_label else "")
        + " (" + str(shown) + " of " + str(total) + " shown):",
        _ID_FIELDS_NOTE,
        "",
    ]

    for i, (start, path) in enumerate(shown_paths, 1):
        lines.append("Path " + str(i) + ": " + _format_path(path, transitions))
        lines.append("Required fields along this path:")
        lines += _format_path_fields(start, path, transitions, fields_def)
        lines.append("")

    if path_mode == "usual" and shown < total:
        lines.append(
            "Showing " + str(shown) + " representative paths out of "
            + str(total) + " found. Strongly recommended: provide obj_id "
            "for the valid next transition and required fields."
        )

    return "\n".join(lines)


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
          states when current_state is omitted). Only paths that can actually
          reach target_state are returned; branches leading to unrelated
          terminal states are pruned before traversal.
          Each path is accompanied by its consolidated list of required
          fields, merged from every state visited along that path.
          Internal (system-driven) steps are shown with [internal] label.
          path_mode controls the output volume:
            "usual" (default) -- up to 10 deduplicated paths.
            "all"             -- all paths, no limit.

        WITH obj_id (object mode):
          Reads the current state of the specific object and uses it as the
          fixed starting point. Path finding then behaves identically to
          schema mode with current_state set -- multi-step paths are shown
          when target_state is not directly reachable from the live state.
          path_mode is respected in this mode.

        Parameters:
          obj_class     - iTop class name, e.g. UserRequest
          target_state  - desired target state, e.g. resolved, assigned
          obj_id        - optional ticket ref or numeric ID (e.g. R-001234)
          current_state - optional starting state for schema-mode search;
                          ignored when obj_id is provided
          path_mode     - "usual" (default) or "all"

        Returns:
          - Reachable paths with per-path required fields.
          - [internal] label on system-driven edges.
          - Hint to provide obj_id when the result list is capped.
        """
        if path_mode not in {"usual", "all"}:
            return "Error: path_mode must be \"usual\" or \"all\"."

        schema = await get_transition_map(obj_class, client)
        transitions = schema.get("transitions", {})
        fields_def = schema.get("fields", {})

        all_states = list(transitions.keys())

        if target_state not in transitions:
            return (
                "Error: target_state '" + target_state + "' not found in schema.\n"
                "Known states: " + ", ".join(all_states)
            )

        # ----------------------------------------------------------------
        # Resolve starting state
        # ----------------------------------------------------------------
        if obj_id and obj_id.strip():
            resolved_class, resolved = await resolve_key(obj_class, obj_id)
            result = await client.get(resolved_class, resolved, fields="status")
            cur = _get_current_state(result, obj_id)
            if not cur:
                return "Error: object " + obj_id + " not found or status field unavailable."
            start_states = [cur]
            start_label = cur
        elif current_state and current_state.strip():
            if current_state not in transitions:
                return (
                    "Error: current_state '" + current_state + "' not found in schema.\n"
                    "Known states: " + ", ".join(all_states)
                )
            start_states = [current_state]
            start_label = current_state
        else:
            start_states = all_states
            start_label = ""

        # ----------------------------------------------------------------
        # Path finding (identical for both modes)
        # ----------------------------------------------------------------
        all_paths: list[tuple[str, list[tuple[str, str, str]]]] = []
        for start in start_states:
            if start == target_state:
                continue
            for path in _find_paths(transitions, start, target_state):
                all_paths.append((start, path))

        if not all_paths:
            return (
                "No paths found to target_state '" + target_state + "'"
                + (" from '" + start_label + "'" if start_label else "")
                + "."
            )

        if path_mode == "all":
            shown_paths = all_paths
        else:
            shown_paths = _select_paths(all_paths, transitions, MAX_USUAL_PATHS)

        return _render_paths(
            shown_paths, all_paths, target_state, start_label,
            transitions, fields_def, path_mode,
        )

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

        Only a single direct transition is supported. If target_state requires
        multiple steps, use Describe_state_change to find the correct sequence
        and apply each step individually.

        Parameters:
          obj_class     - iTop class name, e.g. UserRequest
          obj_id        - numeric object ID or ticket ref (e.g. R-001234)
          target_state  - desired target state (e.g. assigned, waiting_for_approval)
          field_lines   - required fields in Key=Value format, one per line.
                          IMPORTANT: use plain Key=Value text, NOT JSON syntax.
                          Example:
                            approver_id=24
                            approval_reason=Approval requested for R-000105.
                          Do NOT pass JSON like {"approver_id": 24}.
          output_fields - comma-separated fields to return on success
                          (default: ref, friendlyname, status)

        The tool will:
          1. Verify target_state is directly reachable via a user-action stimulus.
          2. Reject the call when the stimulus is internal (system-driven).
          3. Validate that all required fields satisfy their option constraints:
               MANDATORY  -- non-empty on the object or provided in field_lines.
               MUSTPROMPT -- must be explicitly provided in field_lines.
               MUSTCHANGE -- must be provided in field_lines AND differ from
                             the current value already set on the object.
          4. Apply the stimulus via iTop core/apply_stimulus.
          5. Return the updated object on success, or a descriptive error.

        Use Describe_state_change first if the required fields are unknown.
        """
        schema = await get_transition_map(obj_class, client)
        transitions = schema.get("transitions", {})

        obj_class, resolved = await resolve_key(obj_class, obj_id)

        # Single fetch: status + all fields for validation in one round-trip.
        result = await client.get(obj_class, resolved, fields="*")
        current_state = _get_current_state(result, obj_id)
        if not current_state:
            return "Error: object " + obj_id + " not found or status field unavailable."

        current_fields = _get_current_fields(result)

        current_map = transitions.get(current_state, {})
        reachable = current_map.get("targets", {})

        if target_state not in reachable:
            valid = ", ".join(reachable.keys()) if reachable else "none"
            return (
                "Error: target_state '" + target_state + "' is not directly reachable from "
                "current state '" + current_state + "'. Only a single direct transition is "
                "supported here.\n"
                "Directly reachable states: " + valid + "\n"
                "Use Describe_state_change to find the full path to '" + target_state + "'."
            )

        stim_map = reachable[target_state]
        stimulus = list(stim_map.keys())[0]
        stype = (
            stim_map[stimulus].get("type", "")
            if isinstance(stim_map[stimulus], dict)
            else ""
        )

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

        # ----------------------------------------------------------------
        # Validate required fields respecting per-flag semantics:
        #
        #   MUSTCHANGE  -- must be provided AND differ from the current value.
        #                  Takes priority over MUSTPROMPT/MANDATORY when combined.
        #   MUSTPROMPT  -- must be explicitly provided in field_lines even when
        #                  the field already has a value on the object.
        #   MANDATORY   -- value must be non-empty either on the object already
        #                  or in field_lines. Already-set fields satisfy this.
        # ----------------------------------------------------------------
        missing: list[str] = []
        for field_name, field_opts in target_fields.items():
            options = set(
                field_opts.get("options", [])
                if isinstance(field_opts, dict)
                else []
            ) & _REQUIRED_OPTIONS

            if not options:
                continue

            provided_val = provided.get(field_name, "").strip()
            current_val  = current_fields.get(field_name, "").strip()

            if "OPT_ATT_MUSTCHANGE" in options:
                # Must be explicitly provided AND different from current value.
                if not provided_val or provided_val == current_val:
                    missing.append(
                        field_name + " (MUSTCHANGE: provide a new value different from '"
                        + current_val + "')"
                    )
            elif "OPT_ATT_MUSTPROMPT" in options:
                # Must be explicitly provided, even if already set on the object.
                if not provided_val:
                    missing.append(field_name + " (MUSTPROMPT: must be explicitly provided)")
            elif "OPT_ATT_MANDATORY" in options:
                # Satisfied by either a provided value or an existing non-empty value.
                if provided_val in _EMPTY_VALUES and current_val in _EMPTY_VALUES:
                    missing.append(field_name)

        if missing:
            return (
                "Error: field constraints not satisfied for transition to '"
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
