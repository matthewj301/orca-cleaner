"""Compile naming-format templates into name parsers.

A template is literal text plus ``{field}`` placeholders, e.g.
``"{material} - {brand} ({hardware})"``. From it we build a regex that
decomposes a profile name into named fields. The compiled behavior reproduces
the original hand-written regexes that used to live in deduplicator.py, so the
default config parses names identically to before (verified against a golden
corpus of real profile names):

  - Measurement fields (``{layer}``, ``{layer_height}``, ``{nozzle}``) must look
    like a dimension (``\\d+\\.?\\d*mm``). A name whose measurement slot doesn't
    match fails to parse (returns ``None``) — this is how a process name like
    ``"ASA-CF - 3DO"`` is correctly rejected.
  - A generic field followed by a literal delimiter matches lazily and excludes
    that delimiter's leading character, e.g. ``"{material} - "`` -> ``[^-]+?``
    and ``"{brand} ("`` -> ``[^(]+?``. This preserves quirks like
    ``"ABS (2.0) - Brand (...)"`` keeping the ``(2.0)`` in *material* (which
    excludes ``-``, not ``(``).
  - The final field matches greedily (``.+``), so ``{hardware}`` captures to the
    LAST ``)`` — e.g. ``"(Satin) - Prod (X1C - 0.4mm)"`` yields hardware
    ``"Satin) - Prod (X1C - 0.4mm)"`` (an existing quirk, faithfully kept).
  - ``{hardware}`` is a single greedy blob on the parse side; its sub-template
    only matters for rendering (not implemented yet).
  - If the full template (including its ``({hardware})`` clause) doesn't match, a
    no-hardware fallback is tried: the parenthetical clause is dropped and the
    field before it becomes the greedy tail. Trailing text after a bracketed
    template (variation suffixes like `` - beta``) is captured but ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


class GrammarError(ValueError):
    """Raised when a naming format template can't be compiled into a parser."""


# Field roles whose slot must look like a physical dimension.
_MEASUREMENT_FIELDS = frozenset({"layer", "layer_height", "layerheight", "nozzle"})
_MEASUREMENT_PATTERN = r"\d+\.?\d*mm"

_TOKEN_RE = re.compile(r"\{(\w+)\}")
_SUFFIX_GROUP = "__suffix__"


@dataclass(frozen=True)
class CompiledGrammar:
    """A parser compiled from one category's format template."""

    fields: tuple[str, ...]
    primary: re.Pattern
    fallback: re.Pattern | None

    def parse(self, name: str) -> dict[str, str] | None:
        """Decompose ``name`` into its fields, or ``None`` if it doesn't match.

        Only fields declared in the template are returned (an ignored trailing
        suffix group is dropped). Values are stripped but not lower-cased —
        callers apply whatever normalization they need.
        """
        for rx in (self.primary, self.fallback):
            if rx is None:
                continue
            m = rx.match(name.strip())
            if m:
                return {
                    k: (v or "").strip()
                    for k, v in m.groupdict().items()
                    if k != _SUFFIX_GROUP
                }
        return None


def _tokenize(fmt: str) -> list[tuple[str, str]]:
    """Split a format into ('lit', text) / ('field', name) tokens in order."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    for m in _TOKEN_RE.finditer(fmt):
        if m.start() > pos:
            tokens.append(("lit", fmt[pos:m.start()]))
        tokens.append(("field", m.group(1)))
        pos = m.end()
    if pos < len(fmt):
        tokens.append(("lit", fmt[pos:]))
    return tokens


def _literal_regex(text: str) -> str:
    """Escape a literal, letting whitespace match flexibly.

    Whitespace ADJACENT to punctuation matches ``\\s*`` (zero or more), so
    ``" - "`` becomes ``\\s*\\-\\s*`` and ``" ("`` becomes ``\\s*\\(`` — matching
    the original regexes' tolerance for irregular spacing ("ASA-3DO" as well as
    "ASA - 3DO"). But a literal that is ONLY whitespace (a bare field separator,
    e.g. the space in ``"{brand} {material}"``) must require at least one space
    (``\\s+``); otherwise a field could stop mid-token against a zero-width gap.
    """
    if text and text.strip() == "":
        return r"\s+"
    out = []
    for ch in text:
        out.append(r"\s*" if ch.isspace() else re.escape(ch))
    joined = "".join(out)
    # Collapse a run of adjacent \s* into one.
    return re.sub(r"(?:\\s\*){2,}", r"\\s*", joined)


def _field_regex(name: str, next_literal: str | None, is_last: bool) -> str:
    """Regex for one field, given the literal that follows it (if any)."""
    if name in _MEASUREMENT_FIELDS:
        body = _MEASUREMENT_PATTERN
    elif is_last:
        body = r".+"
    elif next_literal:
        stripped = next_literal.lstrip()
        delim = stripped[0] if stripped else next_literal[0]
        body = rf"[^{re.escape(delim)}]+?"
    else:
        body = r".+?"
    return rf"(?P<{name}>{body})"


def _compile_tokens(tokens: list[tuple[str, str]]) -> re.Pattern:
    """Build an anchored regex from a token list.

    Fields become named groups; the last field is greedy. If the template ends
    with a literal, an ignored suffix group is appended so trailing text (e.g.
    a variation suffix after ``)``) doesn't defeat the match.
    """
    field_positions = [i for i, (kind, _) in enumerate(tokens) if kind == "field"]
    last_field_idx = field_positions[-1] if field_positions else -1

    parts: list[str] = ["^"]
    for i, (kind, value) in enumerate(tokens):
        if kind == "lit":
            parts.append(_literal_regex(value))
        else:
            next_lit = None
            if i + 1 < len(tokens) and tokens[i + 1][0] == "lit":
                next_lit = tokens[i + 1][1]
            parts.append(_field_regex(value, next_lit, is_last=(i == last_field_idx)))

    # If the template ends on a literal, allow (and ignore) trailing text.
    if tokens and tokens[-1][0] == "lit":
        parts.append(rf"\s*(?P<{_SUFFIX_GROUP}>.*)")
    parts.append("$")
    return re.compile("".join(parts))


def _fallback_tokens(tokens: list[tuple[str, str]]) -> list[tuple[str, str]] | None:
    """Derive the no-hardware variant: drop the trailing ``(... {hardware})``
    clause (and the literal that introduced it), so a name without a hardware
    parenthetical still parses.

    Returns None — meaning "no fallback, such names simply don't parse" — unless
    ``{hardware}`` is the LAST field. When hardware sits mid-template (an
    alternate convention), what its absence should look like is ambiguous, and
    silently dropping the fields that follow it would corrupt parsing (and, via
    dedup, could mislabel unrelated profiles as duplicates). Leaving those names
    unparsed is the safe choice — dedup treats unparsed names conservatively.
    """
    field_indices = [i for i, (kind, _) in enumerate(tokens) if kind == "field"]
    hw_idx = next(
        (i for i, (kind, val) in enumerate(tokens) if kind == "field" and val == "hardware"),
        None,
    )
    if hw_idx is None or not field_indices or field_indices[-1] != hw_idx:
        return None
    # Drop everything from the literal preceding the hardware field onward
    # (that literal is the opening "(" clause), falling back to dropping from
    # the hardware field itself if there is no preceding literal.
    cut = hw_idx - 1 if hw_idx > 0 and tokens[hw_idx - 1][0] == "lit" else hw_idx
    reduced = tokens[:cut]
    # Trim any now-trailing literal so the last real field becomes the greedy tail.
    while reduced and reduced[-1][0] == "lit":
        reduced.pop()
    return reduced or None


@dataclass(frozen=True)
class RenderSpec:
    """The literal pieces of a format template that the standardizer needs to
    build/rewrite names — separator between fields and the bracket around the
    hardware field — so name construction honors the configured convention
    instead of hardcoding ``" - "`` and ``"(...)"``.

    For the default filament format ``"{material} - {brand} ({hardware})"``:
    separator=``" - "``, hw_prefix=``" ("``, hw_suffix=``")"``, hw_open=``"("``,
    hw_close=``")"``. ``has_hardware`` is False for a format with no
    ``{hardware}`` field (e.g. the machine template).
    """

    separator: str
    hw_prefix: str
    hw_suffix: str
    hw_open: str
    hw_close: str
    has_hardware: bool

    def wrap_hardware(self, hardware: str) -> str:
        """Render the trailing hardware bracket WITH its leading separator, for
        appending to a name — e.g. ``" (LGX - TK - 0.4mm)"``."""
        return f"{self.hw_prefix}{hardware}{self.hw_suffix}"

    def bracket_hardware(self, hardware: str) -> str:
        """Render just the bracketed hardware, no leading separator, for
        substituting in place — e.g. ``"(LGX - TK - 0.4mm)"``."""
        return f"{self.hw_open}{hardware}{self.hw_close}"

    def trailing_hardware_re(self, inner: str = r"[^{close}]+") -> re.Pattern:
        """A regex matching a trailing hardware bracket at end of name, with the
        bracket chars taken from this spec. ``inner`` may reference ``{close}``.
        """
        if not (self.hw_open and self.hw_close):
            # No bracketed hardware in this format — a pattern that never matches.
            return re.compile(r"(?!x)x")
        body = inner.format(close=re.escape(self.hw_close))
        return re.compile(
            re.escape(self.hw_open) + f"({body})" + re.escape(self.hw_close) + r"\s*$"
        )


@lru_cache(maxsize=None)
def render_spec(fmt: str) -> RenderSpec:
    """Extract the separator and hardware-bracket literals from a format.

    The separator is the literal between the first two fields (default ``" - "``
    if it can't be determined). The hardware bracket is the literal immediately
    before/after the ``{hardware}`` field.
    """
    tokens = _tokenize(fmt)
    field_idx = [i for i, (k, _) in enumerate(tokens) if k == "field"]

    separator = " - "
    if len(field_idx) >= 2:
        between = "".join(
            v for k, v in tokens[field_idx[0] + 1: field_idx[1]] if k == "lit"
        )
        if between.strip() or between:
            separator = between or separator

    hw_i = next(
        (i for i, (k, v) in enumerate(tokens) if k == "field" and v == "hardware"), None
    )
    if hw_i is None:
        return RenderSpec(separator, "", "", "", "", has_hardware=False)

    prefix = tokens[hw_i - 1][1] if hw_i > 0 and tokens[hw_i - 1][0] == "lit" else ""
    suffix = tokens[hw_i + 1][1] if hw_i + 1 < len(tokens) and tokens[hw_i + 1][0] == "lit" else ""
    hw_open = prefix.strip()[-1] if prefix.strip() else ""
    hw_close = suffix.strip()[0] if suffix.strip() else ""
    # has_hardware means "renderable": a usable single-character bracket exists.
    # Without one, the standardizer's name-building helpers no-op rather than
    # concatenating hardware with no delimiter (which would be undetectable on a
    # re-run and grow the name). validate_renderable() rejects such formats at
    # config load; this is defense in depth.
    return RenderSpec(
        separator, prefix, suffix, hw_open, hw_close,
        has_hardware=bool(hw_open and hw_close),
    )


def validate_renderable(fmt: str) -> None:
    """Raise GrammarError if a format has a ``{hardware}`` field that can't be
    rendered into a bracket the tool can later re-detect.

    Round-trip check: build a name with the spec's own append, then confirm the
    spec's own detector matches it. This catches (a) a hardware field with no
    surrounding bracket (would glue hardware on with no separator) and (b) a
    multi-character bracket (single-char detector can't re-find it) — both of
    which corrupt names, unboundedly, when `fix --only names` runs repeatedly.
    """
    if "{hardware}" not in fmt:
        return
    spec = render_spec(fmt)
    probe = "X" + spec.wrap_hardware("HW")
    if not (spec.has_hardware and spec.trailing_hardware_re().search(probe)):
        raise GrammarError(
            f"format {fmt!r}: the {{hardware}} field must be wrapped in a "
            f'single-character bracket, e.g. "({{hardware}})" or "[{{hardware}}]", '
            f"so standardized names can be built and re-detected reliably."
        )


@lru_cache(maxsize=None)
def compile_grammar(fmt: str) -> CompiledGrammar:
    """Compile a format template string into a cached parser.

    Raises GrammarError for a template that can't form a valid regex — most
    commonly a repeated field name (each becomes a regex group, and duplicate
    group names are illegal). Config loading validates templates up front so
    this surfaces as a clear config error, not a crash mid-scan.
    """
    tokens = _tokenize(fmt)
    fields = tuple(v for k, v in tokens if k == "field")

    dupes = {f for f in fields if fields.count(f) > 1}
    if dupes:
        raise GrammarError(
            f"format {fmt!r} repeats field name(s): {', '.join(sorted(dupes))}. "
            "Each field may appear only once."
        )
    if not fields:
        raise GrammarError(f"format {fmt!r} has no {{field}} placeholders.")

    try:
        primary = _compile_tokens(tokens)
        fb_tokens = _fallback_tokens(tokens)
        fallback = _compile_tokens(fb_tokens) if fb_tokens else None
    except re.error as e:
        raise GrammarError(f"format {fmt!r} is not a valid name pattern: {e}") from e

    return CompiledGrammar(fields=fields, primary=primary, fallback=fallback)
