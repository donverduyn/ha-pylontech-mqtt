"""Schema-driven parsing engine for whitespace-delimited console tables,
colon-separated key:value blocks, loosely-keyed "key: value" info dumps, and
regex-extracted counters.

This module knows nothing about Pylontech, or any other BMS protocol — it
contains no column names, command names, or field names. All of that lives
in the schema objects a caller builds and passes in (see
src/parser_schema.py for the Pylontech-specific schemas). This
module never imports parser_schema (or anything else
protocol-specific) — parser_schema imports the schema dataclasses
defined here to build its schema instances, but the dependency only runs
that one direction. Whatever wires a schema to the engine (src/main.py's
``Parser(SOME_SCHEMA)``) is the one place that imports from both.

Every entrypoint below takes exactly the raw text and a schema (plus, for
the ones that mutate an existing object rather than build a new one, that
target object) — there is no separate row-construction or field-assignment
callback at the call site; a schema fully owns how its own output is built,
via fields (row_factory, aggregate) declared on the schema object itself.
"""

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

Row = list[str]
Transform = Callable[[Row, int], Any]


# ---------------------------------------------------------------------------
# Generic numeric/string cell transforms — parametrized purely by (row, idx),
# no protocol-specific names. "required_*" raise on a missing/invalid token,
# which the table engine treats as "skip this whole row"; "optional_*"
# tolerate a missing token or a "-" placeholder and return None instead.
# ---------------------------------------------------------------------------


def required_int(parts: Row, idx: int) -> int:
    return int(parts[idx])


def required_milli(parts: Row, idx: int) -> float:
    return int(parts[idx]) / 1000.0


def required_str(parts: Row, idx: int) -> str:
    return parts[idx]


def required_percent_int(parts: Row, idx: int) -> int:
    return int(parts[idx].replace("%", ""))


def optional_milli(parts: Row, idx: int) -> float | None:
    if len(parts) <= idx or parts[idx] == "-":
        return None
    return int(parts[idx]) / 1000.0


def optional_status(parts: Row, idx: int) -> str | None:
    if len(parts) <= idx:
        return None
    v = parts[idx].strip()
    return v if v != "-" else None


def optional_str_by_bounds(parts: Row, idx: int) -> str | None:
    """Like optional_status but without "-"-placeholder stripping — some
    tables (e.g. 'bat N') never emit a dash placeholder for these columns,
    only ever a bounds-driven absence on a short/headerless row."""
    return parts[idx] if len(parts) > idx else None


def optional_int_by_bounds(parts: Row, idx: int) -> int | None:
    return int(parts[idx]) if len(parts) > idx else None


def percent_int_or_zero(parts: Row, idx: int) -> int:
    return int(parts[idx].replace("%", "")) if len(parts) > idx else 0


def parse_number(value: str) -> int | None:
    """Parse a decimal or '0x'-prefixed hex integer; None for empty,
    placeholder ("-"), or otherwise unparseable input."""
    value = value.strip()
    if not value or value == "-":
        return None
    try:
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    except ValueError:
        _LOGGER.warning("Could not parse numeric value: %r", value)
        return None


# ---------------------------------------------------------------------------
# Table schema: a whitespace-delimited console table with an optional header
# line (e.g. Pylontech's 'pwr' or 'bat N' output).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a table schema.

    header_tokens: the consecutive header token(s) identifying this column
      (matched case-insensitively). Length > 1 for a compound header made of
      several header tokens (e.g. ("Base", "State")).
    data_width: how many data-row tokens this column occupies. Usually equal
      to len(header_tokens), but not always — a header token can expand into
      several data tokens (e.g. a "Time" header producing a date token and a
      clock token), or several header tokens can collapse into one data
      token (e.g. "Base State" -> one status word per row).
    field: the row-dict key this column's data populates, or None if this
      column exists only to consume header/data width (e.g. "Time" itself
      carries no field of its own).
    default_index: the data-row index to use when no header line is found at
      all (a fully positional fallback); None if this column has no defined
      position without a header.
    transform: given the row's tokens and this column's resolved data index,
      return the field's value. Only called when field is not None. May
      raise (IndexError/ValueError) to signal "this row is invalid" — the
      whole row is then skipped and logged. A transform that should instead
      tolerate a missing/placeholder token must catch internally and return
      None (see the optional_* helpers above).
    """

    header_tokens: tuple[str, ...]
    data_width: int = 1
    field: str | None = None
    default_index: int | None = None
    transform: Transform | None = None


@dataclass(frozen=True)
class TableSchema:
    """Declarative description of one console table, including how a parsed
    row becomes an output object (row_factory) and, optionally, how the full
    set of rows updates some target object (aggregate) — e.g. table-level
    summary fields, or simply assigning the row list to one of its
    attributes. Everything parse_table needs to go from raw text to a fully
    built result lives here; nothing is passed in alongside the schema."""

    header_first_token: str
    header_must_contain: str
    columns: Sequence[ColumnSpec]
    is_data_row: Callable[[Row], bool]
    row_factory: Callable[[dict[str, Any]], Any]
    skip_row: Callable[[str], bool] | None = None
    # Field whose resolved index sets this row's minimum required length
    # (rows shorter than that are skipped rather than raising deep inside a
    # transform); None if is_data_row already enforces a sufficient length.
    min_index_field: str | None = None
    # Escape hatch for header quirks that depend on *which* header tokens
    # were actually seen (not just their positions) — e.g. a firmware that
    # omits one header label but still emits its data token. Receives the
    # {field: data_index} map resolved from this header plus the raw set of
    # header tokens seen, and may return an adjusted map.
    header_postprocess: Callable[[dict[str, int], set[str]], dict[str, int]] | None = (
        None
    )
    # Called with (rows, target) once every row is parsed, when parse_table
    # was given a target — e.g. to set table-level summary fields, or to
    # assign the row list to one of the target's attributes.
    aggregate: Callable[[list[Any], Any], None] | None = None
    # Used only to format the per-row error log message (see parse_table) —
    # a diagnostic label, not protocol grammar.
    row_error_label: str = "table"


def _matches_header(schema: TableSchema, parts: Row) -> bool:
    return (
        bool(parts)
        and parts[0] == schema.header_first_token
        and schema.header_must_contain in parts
    )


def _resolve_indices(schema: TableSchema, parts: Row) -> dict[str, int]:
    """Walk header tokens left to right, matching the longest declared
    column at each position, and return {field: data_row_index} for every
    named column found in *this* header line."""
    indices: dict[str, int] = {}
    seen_tokens = set(parts)
    by_length = sorted(schema.columns, key=lambda c: -len(c.header_tokens))

    hdr_i = 0
    data_i = 0
    while hdr_i < len(parts):
        for col in by_length:
            width = len(col.header_tokens)
            candidate = tuple(t.lower() for t in parts[hdr_i : hdr_i + width])
            if candidate == tuple(t.lower() for t in col.header_tokens):
                if col.field is not None:
                    indices[col.field] = data_i
                hdr_i += width
                data_i += col.data_width
                break
        else:
            # Unrecognized header token: every currently-known Pylontech
            # console column (named or not) occupies exactly one data token,
            # so assume the same for anything the schema didn't declare.
            hdr_i += 1
            data_i += 1

    if schema.header_postprocess is not None:
        indices = schema.header_postprocess(indices, seen_tokens)
    return indices


def parse_table(raw_text: str, schema: TableSchema, target: Any = None) -> list[Any]:
    """Parse every data row of *raw_text* per *schema* into a list of
    schema.row_factory(field_values) objects. If *target* is given and
    schema declares an aggregate hook, it is called as
    schema.aggregate(rows, target) before returning.

    Column positions come from the header line if one is present (mapped by
    name, so reordered/inserted columns are handled automatically); any
    column absent from that header — or the case where no header line is
    found at all — falls back to its schema-declared default_index.
    """
    lines = raw_text.splitlines()

    indices: dict[str, int | None] = {
        c.field: c.default_index for c in schema.columns if c.field is not None
    }
    for line in lines:
        parts = line.split()
        if _matches_header(schema, parts):
            indices.update(_resolve_indices(schema, parts))
            break

    columns_by_field = {c.field: c for c in schema.columns if c.field is not None}

    rows: list[Any] = []
    for line in lines:
        parts = line.split()
        if not schema.is_data_row(parts):
            continue
        if schema.skip_row is not None and schema.skip_row(line):
            continue
        if schema.min_index_field is not None:
            min_idx = indices.get(schema.min_index_field)
            if min_idx is not None and len(parts) < min_idx + 1:
                continue
        try:
            values: dict[str, Any] = {}
            for field_name, col in columns_by_field.items():
                idx = indices.get(field_name)
                if col.transform is not None and idx is not None:
                    values[field_name] = col.transform(parts, idx)
            rows.append(schema.row_factory(values))
        except (ValueError, IndexError) as error:
            _LOGGER.error(
                "Error parsing %s line '%s': %s", schema.row_error_label, line, error
            )
            continue

    if target is not None and schema.aggregate is not None:
        schema.aggregate(rows, target)
    return rows


def has_header(raw_text: str, schema: TableSchema) -> bool:
    """Return whether *raw_text* contains *schema*'s header line."""
    return any(_matches_header(schema, line.split()) for line in raw_text.splitlines())


# ---------------------------------------------------------------------------
# Key:value block schema (e.g. Pylontech's vertical 'pwr N' response).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KVField:
    key: str
    field: str
    transform: Callable[[str], Any] | None = None
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class KeyValueSchema:
    not_found_marker: str
    invalid_if: Callable[[dict[str, str]], bool]
    fields: Sequence[KVField]
    # Builds the final result from the mapped field values (plus any *extra*
    # context passed to parse_keyvalue, e.g. an id that identifies which
    # block this was but isn't itself part of the block's own text). None
    # means "return the plain {field: value} dict".
    row_factory: Callable[[dict[str, Any]], Any] | None = None


def parse_keyvalue(
    raw_text: str, schema: KeyValueSchema, extra: dict[str, Any] | None = None
) -> Any | None:
    """Parse a "Key: value ..." block per *schema*, taking only the first
    whitespace token of each value. Returns None if schema's not-found
    marker is present, the block is empty, schema.invalid_if says so, or any
    required field is missing/unparseable. Otherwise returns
    schema.row_factory(field_values) (or the plain dict, if no row_factory
    is declared) — *extra* is merged in before that field_values map is
    built, for context that comes from outside the block's own text."""
    if schema.not_found_marker in raw_text:
        return None

    raw_fields: dict[str, str] = {}
    for line in raw_text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        tokens = rest.split()
        if not key or not tokens:
            continue
        raw_fields[key] = tokens[0]

    if not raw_fields or schema.invalid_if(raw_fields):
        return None

    result: dict[str, Any] = dict(extra or {})
    for kv in schema.fields:
        raw_val = raw_fields.get(kv.key)
        try:
            if raw_val is None:
                raise KeyError(kv.key)
            result[kv.field] = kv.transform(raw_val) if kv.transform else raw_val
        except (KeyError, ValueError) as error:
            if kv.required:
                _LOGGER.error(
                    "Error parsing key:value block field %r: %s", kv.key, error
                )
                return None
            result[kv.field] = kv.default

    return schema.row_factory(result) if schema.row_factory else result


# ---------------------------------------------------------------------------
# Loosely-keyed schema (e.g. Pylontech's 'info' response — case-insensitive,
# substring-matched keys with normalized whitespace).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LooseKeyField:
    match: Callable[[str], bool]
    field: str
    transform: Callable[[str], Any] | None = None


@dataclass(frozen=True)
class LooseKeySchema:
    fields: Sequence[LooseKeyField]


def _normalize_key(raw_key: str) -> str:
    return re.sub(r"\s+", " ", raw_key.strip().lower())


def parse_loose_keys(raw_text: str, schema: LooseKeySchema, target: Any) -> Any:
    """Parse "Key: value" lines into attributes set directly on *target*
    (returned for convenience). The key is matched loosely — schema decides
    what "loosely" means per field (substring, exact, etc.) against the
    whitespace-normalized, lowercased key. First matching field wins, same
    as an if/elif chain."""
    for line in raw_text.splitlines():
        if ":" not in line:
            continue
        raw_key, val = line.split(":", 1)
        key = _normalize_key(raw_key)
        val = val.strip()
        for lk in schema.fields:
            if lk.match(key):
                try:
                    value = lk.transform(val) if lk.transform else val
                except (ValueError, AttributeError) as error:
                    _LOGGER.warning(
                        "Could not parse field %r from line %r: %s",
                        lk.field,
                        val,
                        error,
                    )
                    break
                setattr(target, lk.field, value)
                break
    return target


# ---------------------------------------------------------------------------
# Regex-extracted fields (e.g. Pylontech's 'stat' counters and 'time' stamp).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegexField:
    pattern: str
    field: str
    transform: Callable[[str], Any] = str
    # False means "a miss leaves target's existing attribute value alone"
    # instead of overwriting it with None — e.g. a still-current bms_time
    # from a previous poll shouldn't be clobbered by one bad read.
    always_set: bool = True


def parse_regex_fields(raw_text: str, fields: Sequence[RegexField], target: Any) -> Any:
    """Regex-search *raw_text* for each field's pattern (case-insensitive,
    one capture group) and set it as an attribute on *target* (returned for
    convenience), per each field's always_set rule."""
    for rf in fields:
        m = re.search(rf.pattern, raw_text, re.IGNORECASE)
        value = rf.transform(m.group(1)) if m else None
        if value is not None or rf.always_set:
            setattr(target, rf.field, value)
    return target


# ---------------------------------------------------------------------------
# Parser — binds one schema to the engine function that knows how to walk
# it. Instantiate once per schema (e.g. one Parser per BMS command, built at
# module load time), then call .parse(...) as many times as needed; callers
# never need to know or care which parse_* function above actually applies
# to their schema.
# ---------------------------------------------------------------------------

Schema = TableSchema | KeyValueSchema | LooseKeySchema | Sequence[RegexField]


class Parser:
    """A parser bound to a single schema."""

    def __init__(self, schema: Schema) -> None:
        self._schema = schema

    def parse(
        self,
        raw_text: str,
        target: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> Any:
        """Parse *raw_text* per the bound schema.

        *target* is the object to mutate (and return) for schemas that
        update an existing object in place (TableSchema with an aggregate
        hook, LooseKeySchema, RegexField lists) — ignored otherwise.
        *extra* is context to merge into a KeyValueSchema's result that
        isn't itself present in the raw text (e.g. which slot was queried).
        """
        schema = self._schema
        if isinstance(schema, TableSchema):
            return parse_table(raw_text, schema, target=target)
        if isinstance(schema, KeyValueSchema):
            return parse_keyvalue(raw_text, schema, extra=extra)
        if isinstance(schema, LooseKeySchema):
            return parse_loose_keys(raw_text, schema, target)
        return parse_regex_fields(raw_text, schema, target)
