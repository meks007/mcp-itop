"""
helpers package -- re-exports all public names so existing imports continue
to work without change after the flat helpers.py was split into submodules.

    from helpers import format_and_cache, resolve_key, ...   # unchanged
"""

from helpers.html import (
    _strip_html,
    strip_html_recursive,
    parse_objects,
    _BLOCK_TAGS,
    _MSO_CONDITIONAL_RE,
    _ANY_TAG_RE,
    _HTML_ENTITIES,
    _HTML_ENTITY_RE,
    _INLINE_IMG_RE,
    _decode_entity,
)

from helpers.sla import (
    SLA_ANALYSIS_FIELDS,
    sla_is_passed,
    sla_is_breached,
    _SLA_PASSED_VALUES,
    _SLA_BREACHED_VALUES,
)

from helpers.utils import (
    str_or,
    parse_key,
    parse_json_arg,
    parse_date_range,
    coerce_ref,
    is_bare_number,
    _try_json_parse,
    CLASSES_WITH_REF,
    _REF_PATTERN,
    _BARE_NUMBER_PATTERN,
    _SYNTHETIC_FIELDS,
)

from helpers.stripping import (
    _LEAN_STRIP,
    apply_field_strip,
)

from helpers.resolvers import (
    ensure_ref_field,
    ensure_class_exists,
    resolve_output_fields,
    resolve_ref_class_by_ref_part,
    resolve_key,
    fetch_image_counts,
)

from helpers.formatters import (
    extract_objects,
    _format_objects,
    format_objects,
    format_and_cache,
    format_table,
    format_duration,
)

# Cache helpers imported by some tool modules via "from helpers import ..."
from cache import (
    registry_get_fields,
    registry_get_meta,
    registry_set_meta,
    seed_field_cache,
    _registry_entry,
)

# Client context helpers -- allow "from helpers import get_client, set_client"
from client import get_client, set_client

__all__ = [
    # html
    "_strip_html", "strip_html_recursive", "parse_objects",
    # sla
    "SLA_ANALYSIS_FIELDS", "sla_is_passed", "sla_is_breached",
    # utils
    "str_or", "parse_key", "parse_json_arg", "parse_date_range",
    "coerce_ref", "is_bare_number", "_try_json_parse",
    "CLASSES_WITH_REF", "_SYNTHETIC_FIELDS",
    # stripping
    "_LEAN_STRIP", "apply_field_strip",
    # resolvers
    "ensure_ref_field", "ensure_class_exists", "resolve_output_fields",
    "resolve_ref_class_by_ref_part", "resolve_key", "fetch_image_counts",
    # formatters
    "extract_objects", "_format_objects", "format_objects",
    "format_and_cache", "format_table", "format_duration",
    # cache pass-throughs
    "registry_get_fields", "registry_get_meta", "registry_set_meta",
    "seed_field_cache", "_registry_entry",
    # client context
    "get_client", "set_client",
]
