"""JSON form of a ``DesignGraph``, so elaboration can be cached and inspected.

``dump_design_graph`` writes a graph as deterministic JSON text and
``load_design_graph`` reads it back to an equal graph. The format is internal
to Track B. It is not a shared contract: a track that wants to consume it
proposes that through ``spec.md`` section 2.

Format, version 1. A top-level object with exactly these fields:

* ``format_version``: the integer ``1``. Any other version is rejected.
* ``pyslang_version``: the pyslang that elaborated the sources (informational).
* ``top_module``, ``sources``, ``signals``, ``drivers``, ``aliases``,
  ``diagnostics``.

Structural edges are not stored; they are derived from the drivers on load.
Every IR node is a tagged object, ``{"k": "binary", "width": 4, "signed":
false, "op": "+", "left": {...}, "right": {...}}``, encoded and decoded through
one table keyed by node class, so a node class without an entry cannot be
written. Field names are snake_case, unknown and missing fields are rejected,
and no pickle is involved.

A graph says which file contents it was built from, through the SHA-256 in
each ``SourceFile``. ``verify_sources`` re-hashes the files; passing ``root`` to
``load_design_graph`` adds its findings to the graph's diagnostics, and the
slicer repeats them in every report, so a stale graph is never sliced silently.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import Final

from src.causal import expr as ir
from src.causal.ast_graph import (
    ClockSpec,
    DesignGraph,
    Driver,
    GuardedAssign,
    ProcessKind,
    SignalInfo,
    SignalKind,
    structural_edges,
)
from src.causal.fourstate import BinaryOp, CaseFlavor, UnaryOp
from src.contracts import BitRange, Diagnostic, EdgeType, SourceFile, SourceSpan

FORMAT_VERSION: Final = 1
_BITS_RE: Final = re.compile(r"[01xz]+")


class GraphFormatError(ValueError):
    """The text is not a design graph in a format version this code reads."""


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def dump_design_graph(graph: DesignGraph) -> str:
    """Deterministic JSON text for ``graph``: equal graphs give identical text."""
    document = {
        "format_version": FORMAT_VERSION,
        "pyslang_version": metadata.version("pyslang"),
        "top_module": graph.top_module,
        "sources": [
            {"id": source.id, "path": source.path, "sha256": source.sha256}
            for source in graph.sources
        ],
        "signals": [_encode_signal(info) for info in graph.signals.values()],
        "drivers": [
            _encode_driver(driver) for found in graph.drivers.values() for driver in found
        ],
        "aliases": [[child, parent] for child, parent in graph.aliases.items()],
        "diagnostics": [_encode_diagnostic(diagnostic) for diagnostic in graph.diagnostics],
    }
    return json.dumps(document, indent=1, ensure_ascii=True) + "\n"


_GRAPH_FIELDS: Final = frozenset(
    {
        "format_version", "pyslang_version", "top_module", "sources", "signals", "drivers",
        "aliases", "diagnostics",
    }  # fmt: skip
)


def load_design_graph(text: str, *, root: Path | str | None = None) -> DesignGraph:
    """Read a graph written by ``dump_design_graph``.

    Raises ``GraphFormatError`` for anything that is not a version 1 graph.
    With ``root``, the source files are re-hashed and any mismatch is added to
    the graph's diagnostics (see ``verify_sources``).
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise GraphFormatError(f"not JSON: {error}") from error
    if not isinstance(document, dict):
        raise GraphFormatError("a design graph is a JSON object")
    if "format_version" not in document:
        raise GraphFormatError("graph: missing field format_version")
    version = document["format_version"]
    if isinstance(version, bool) or version != FORMAT_VERSION:
        raise GraphFormatError(
            f"unsupported format_version {version!r}; this code reads version {FORMAT_VERSION}"
        )
    fields = _fields(document, _GRAPH_FIELDS, "graph")
    _str(fields["pyslang_version"], "pyslang_version")
    sources = tuple(_decode_source(item) for item in _list(fields["sources"], "sources"))
    signals: dict[str, SignalInfo] = {}
    for item in _list(fields["signals"], "signals"):
        info = _decode_signal(item)
        if info.name in signals:
            raise GraphFormatError(f"signal {info.name} is declared twice")
        signals[info.name] = info
    drivers: dict[str, list[Driver]] = {}
    for item in _list(fields["drivers"], "drivers"):
        driver = _decode_driver(item)
        drivers.setdefault(driver.target, []).append(driver)
    grouped = {target: tuple(found) for target, found in drivers.items()}
    aliases: dict[str, str] = {}
    for item in _list(fields["aliases"], "aliases"):
        pair = _list(item, "alias")
        if len(pair) != 2:
            raise GraphFormatError("alias: expected a [child, parent] pair")
        aliases[_str(pair[0], "alias")] = _str(pair[1], "alias")
    diagnostics = tuple(
        _decode_diagnostic(item) for item in _list(fields["diagnostics"], "diagnostics")
    )
    if root is not None:
        stale = DesignGraph(
            top_module="", sources=sources, signals={}, drivers={}, edges=(), diagnostics=()
        )
        found = verify_sources(stale, root)
        diagnostics = (*diagnostics, *(d for d in found if d not in diagnostics))
    return DesignGraph(
        top_module=_str(fields["top_module"], "top_module"),
        sources=sources,
        signals=MappingProxyType(signals),
        drivers=MappingProxyType(grouped),
        edges=structural_edges(grouped),
        diagnostics=diagnostics,
        aliases=MappingProxyType(aliases),
    )


def verify_sources(graph: DesignGraph, root: Path | str) -> tuple[Diagnostic, ...]:
    """Re-hash every source file under ``root``; one diagnostic per file that differs."""
    base = Path(root)
    found: list[Diagnostic] = []
    for source in graph.sources:
        path = base / source.path
        if not path.is_file():
            found.append(
                Diagnostic(
                    code="SOURCE_MISSING",
                    message=f"{source.path} was used to build the design graph but is not "
                    f"present under {base}; the graph may not describe the current design.",
                )
            )
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != source.sha256:
            found.append(
                Diagnostic(
                    code="SOURCE_CHANGED",
                    message=f"{source.path} has changed since the design graph was built "
                    f"(SHA-256 {digest}, graph has {source.sha256}); its drivers and source "
                    "lines may be stale.",
                )
            )
    return tuple(found)


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------

_Encoder = Callable[[object], object]
_Decoder = Callable[[object, str], object]


def _encode_exprs(value: object) -> object:
    assert isinstance(value, tuple)
    return [_encode_expr(item) for item in value]


def _encode_plain(value: object) -> object:
    return value


def _encode_enum(value: object) -> object:
    assert isinstance(value, (UnaryOp, BinaryOp, ir.SelectMode))
    return value.value


def _encode_span(value: object) -> object:
    if value is None:
        return None
    assert isinstance(value, SourceSpan)
    return {"source_id": value.source_id, "line": value.line, "end_line": value.end_line}


def _encode_ranges(value: object) -> object:
    assert isinstance(value, tuple)
    return [list(bounds) for bounds in value]


def _encode_ints(value: object) -> object:
    assert isinstance(value, tuple)
    return list(value)


def _encode_one(value: object) -> object:
    assert isinstance(value, ir.Expr)
    return _encode_expr(value)


_ENCODERS: Final[Mapping[str, _Encoder]] = MappingProxyType(
    {
        "int": _encode_plain,
        "bool": _encode_plain,
        "str": _encode_plain,
        "expr": _encode_one,
        "exprs": _encode_exprs,
        "refs": _encode_exprs,
        "unary_op": _encode_enum,
        "binary_op": _encode_enum,
        "mode": _encode_enum,
        "span": _encode_span,
        "ranges": _encode_ranges,
        "ints": _encode_ints,
    }
)


def _decode_exprs(value: object, where: str) -> object:
    return tuple(_decode_expr(item, where) for item in _list(value, where))


def _decode_refs(value: object, where: str) -> object:
    refs = tuple(_decode_expr(item, where) for item in _list(value, where))
    if not all(isinstance(ref, ir.SignalRef) for ref in refs):
        raise GraphFormatError(f"{where}: every element must be a signal reference")
    return refs


def _decode_ranges(value: object, where: str) -> object:
    ranges: list[tuple[int, int]] = []
    for item in _list(value, where):
        bounds = _list(item, where)
        if len(bounds) != 2:
            raise GraphFormatError(f"{where}: expected a [left, right] pair")
        ranges.append((_int(bounds[0], where), _int(bounds[1], where)))
    return tuple(ranges)


def _decode_ints(value: object, where: str) -> object:
    return tuple(_int(item, where) for item in _list(value, where))


def _decode_int(value: object, where: str) -> object:
    return _int(value, where)


def _decode_str(value: object, where: str) -> object:
    return _str(value, where)


def _decode_bool(value: object, where: str) -> object:
    return _bool(value, where)


def _decode_one(value: object, where: str) -> object:
    return _decode_expr(value, where)


def _decode_span_field(value: object, where: str) -> object:
    return _decode_span(value, where)


def _decode_unary(value: object, where: str) -> object:
    return _enum(UnaryOp, value, where)


def _decode_binary(value: object, where: str) -> object:
    return _enum(BinaryOp, value, where)


def _decode_mode(value: object, where: str) -> object:
    return _enum(ir.SelectMode, value, where)


_DECODERS: Final[Mapping[str, _Decoder]] = MappingProxyType(
    {
        "int": _decode_int,
        "bool": _decode_bool,
        "str": _decode_str,
        "expr": _decode_one,
        "exprs": _decode_exprs,
        "refs": _decode_refs,
        "unary_op": _decode_unary,
        "binary_op": _decode_binary,
        "mode": _decode_mode,
        "span": _decode_span_field,
        "ranges": _decode_ranges,
        "ints": _decode_ints,
    }
)

EXPR_CODECS: Final[Mapping[type[ir.Expr], tuple[str, tuple[tuple[str, str], ...]]]] = (
    MappingProxyType(
        {
            ir.Const: ("const", (("bits", "str"),)),
            ir.SignalRef: ("ref", (("signal", "str"),)),
            ir.PartSelect: ("part", (("value", "expr"), ("offset", "int"), ("label", "str"))),
            ir.IndexedSelect: (
                "indexed",
                (
                    ("value", "expr"), ("index", "expr"), ("left", "int"), ("right", "int"),
                    ("elem_width", "int"), ("mode", "mode"),
                ),  # fmt: skip
            ),
            ir.ArrayElement: ("element", (("array", "expr"), ("index", "expr"))),
            ir.ArrayRead: (
                "array_read",
                (
                    ("name", "str"), ("elements", "refs"), ("indices", "exprs"),
                    ("ranges", "ranges"), ("decisions", "ints"),
                ),  # fmt: skip
            ),
            ir.Unary: ("unary", (("op", "unary_op"), ("operand", "expr"))),
            ir.Binary: ("binary", (("op", "binary_op"), ("left", "expr"), ("right", "expr"))),
            ir.Conditional: (
                "conditional",
                (
                    ("decision", "int"), ("predicate", "expr"), ("if_true", "expr"),
                    ("if_false", "expr"), ("span", "span"),
                ),  # fmt: skip
            ),
            ir.Concat: ("concat", (("parts", "exprs"),)),
            ir.Replicate: ("replicate", (("count", "int"), ("operand", "expr"))),
            ir.Convert: ("convert", (("operand", "expr"), ("two_state", "bool"))),
            ir.Unsupported: ("unsupported", (("what", "str"), ("operands", "exprs"))),
        }
    )
)
"""One entry per IR node class: its tag and its fields beyond ``width`` and ``signed``."""

_BY_TAG: Final = {tag: (cls, fields) for cls, (tag, fields) in EXPR_CODECS.items()}


def _encode_expr(expr: ir.Expr) -> dict[str, object]:
    entry = EXPR_CODECS.get(type(expr))
    if entry is None:
        raise GraphFormatError(f"no encoder for IR node {type(expr).__name__}")
    tag, fields = entry
    out: dict[str, object] = {"k": tag, "width": expr.width, "signed": expr.signed}
    for name, kind in fields:
        out[name] = _ENCODERS[kind](getattr(expr, name))
    return out


def _decode_expr(value: object, where: str) -> ir.Expr:
    if not isinstance(value, dict) or not isinstance(value.get("k"), str):
        raise GraphFormatError(f"{where}: an IR node is an object with a string tag 'k'")
    tag = value["k"]
    if tag not in _BY_TAG:
        raise GraphFormatError(f"{where}: unknown IR node tag {tag!r}")
    cls, fields = _BY_TAG[tag]
    here = f"{where}.{tag}"
    data = _fields(value, {"k", "width", "signed", *(name for name, _ in fields)}, here)
    width = _int(data["width"], here)
    if width < 1:
        raise GraphFormatError(f"{here}: width must be at least 1")
    arguments = {name: _DECODERS[kind](data[name], f"{here}.{name}") for name, kind in fields}
    node = cls(width=width, signed=_bool(data["signed"], here), **arguments)
    if isinstance(node, ir.Const) and (
        len(node.bits) != width or not _BITS_RE.fullmatch(node.bits)
    ):
        raise GraphFormatError(f"{here}: bits must be {width} characters from 01xz")
    return node


# --------------------------------------------------------------------------
# Guards, assignments, drivers, signals
# --------------------------------------------------------------------------

_GUARD_FIELDS: Final = frozenset(
    {
        "decision", "kind", "predicate", "text", "polarity", "flavor", "items", "earlier",
        "span", "element",
    }  # fmt: skip
)


def _encode_guard(guard: ir.Guard) -> dict[str, object]:
    return {
        "decision": guard.decision,
        "kind": guard.kind.value,
        "predicate": _encode_expr(guard.predicate),
        "text": guard.text,
        "polarity": guard.polarity,
        "flavor": guard.flavor.value,
        "items": [_encode_expr(item) for item in guard.items],
        "earlier": [_encode_expr(item) for item in guard.earlier],
        "span": _encode_span(guard.span),
        "element": guard.element,
    }


def _decode_guard(value: object, where: str) -> ir.Guard:
    data = _fields(value, _GUARD_FIELDS, where)
    element = data["element"]
    return ir.Guard(
        decision=_int(data["decision"], where),
        kind=_enum(ir.GuardKind, data["kind"], where),
        predicate=_decode_expr(data["predicate"], f"{where}.predicate"),
        text=_str(data["text"], where),
        polarity=_bool(data["polarity"], where),
        flavor=_enum(CaseFlavor, data["flavor"], where),
        items=tuple(_decode_expr(item, f"{where}.items") for item in _list(data["items"], where)),
        earlier=tuple(
            _decode_expr(item, f"{where}.earlier") for item in _list(data["earlier"], where)
        ),
        span=_decode_span(data["span"], where),
        element=None if element is None else _int(element, where),
    )


_ASSIGN_FIELDS: Final = frozenset(
    {
        "target", "full_write", "rhs", "guards", "index_operands", "blocking", "span", "text",
        "written",
    }  # fmt: skip
)


def _encode_assign(assign: GuardedAssign) -> dict[str, object]:
    return {
        "target": assign.target,
        "full_write": assign.full_write,
        "rhs": _encode_expr(assign.rhs),
        "guards": [_encode_guard(guard) for guard in assign.guards],
        "index_operands": [_encode_expr(operand) for operand in assign.index_operands],
        "blocking": assign.blocking,
        "span": _encode_span(assign.span),
        "text": assign.text,
        "written": None if assign.written is None else list(assign.written),
    }


def _decode_assign(value: object, where: str) -> GuardedAssign:
    data = _fields(value, _ASSIGN_FIELDS, where)
    written: tuple[int, int] | None = None
    if data["written"] is not None:
        pair = _list(data["written"], where)
        if len(pair) != 2:
            raise GraphFormatError(f"{where}: written is an [offset, width] pair or null")
        written = (_int(pair[0], where), _int(pair[1], where))
    return GuardedAssign(
        target=_str(data["target"], where),
        full_write=_bool(data["full_write"], where),
        rhs=_decode_expr(data["rhs"], f"{where}.rhs"),
        guards=tuple(
            _decode_guard(item, f"{where}.guards") for item in _list(data["guards"], where)
        ),
        index_operands=tuple(
            _decode_expr(item, f"{where}.index_operands")
            for item in _list(data["index_operands"], where)
        ),
        blocking=_bool(data["blocking"], where),
        span=_decode_span(data["span"], where),
        text=_str(data["text"], where),
        written=written,
    )


_DRIVER_FIELDS: Final = frozenset(
    {
        "target", "edge_type", "process", "clock", "assigns", "span", "unsupported", "caveats",
        "intermediate",
    }  # fmt: skip
)


def _encode_driver(driver: Driver) -> dict[str, object]:
    clock = driver.clock
    return {
        "target": driver.target,
        "edge_type": driver.edge_type.value,
        "process": driver.process.value,
        "clock": None if clock is None else {"signal": clock.signal, "rising": clock.rising},
        "assigns": [_encode_assign(assign) for assign in driver.assigns],
        "span": _encode_span(driver.span),
        "unsupported": driver.unsupported,
        "caveats": list(driver.caveats),
        "intermediate": list(driver.intermediate),
    }


def _decode_driver(value: object) -> Driver:
    where = "driver"
    data = _fields(value, _DRIVER_FIELDS, where)
    clock: ClockSpec | None = None
    if data["clock"] is not None:
        spec = _fields(data["clock"], {"signal", "rising"}, f"{where}.clock")
        clock = ClockSpec(_str(spec["signal"], where), _bool(spec["rising"], where))
    unsupported = data["unsupported"]
    return Driver(
        target=_str(data["target"], where),
        edge_type=_enum(EdgeType, data["edge_type"], where),
        process=_enum(ProcessKind, data["process"], where),
        clock=clock,
        assigns=tuple(
            _decode_assign(item, f"{where}.assigns") for item in _list(data["assigns"], where)
        ),
        span=_decode_span(data["span"], where),
        unsupported=None if unsupported is None else _str(unsupported, where),
        caveats=tuple(_str(item, where) for item in _list(data["caveats"], where)),
        intermediate=tuple(_str(item, where) for item in _list(data["intermediate"], where)),
    )


_SIGNAL_FIELDS: Final = frozenset(
    {"name", "width", "bit_range", "signed", "kind", "span", "is_aggregate"}
)


def _encode_signal(info: SignalInfo) -> dict[str, object]:
    bit_range = info.bit_range
    return {
        "name": info.name,
        "width": info.width,
        "bit_range": None
        if bit_range is None
        else {"left": bit_range.left, "right": bit_range.right},
        "signed": info.signed,
        "kind": info.kind.value,
        "span": _encode_span(info.span),
        "is_aggregate": info.is_aggregate,
    }


def _decode_signal(value: object) -> SignalInfo:
    where = "signal"
    data = _fields(value, _SIGNAL_FIELDS, where)
    bit_range: BitRange | None = None
    if data["bit_range"] is not None:
        bounds = _fields(data["bit_range"], {"left", "right"}, f"{where}.bit_range")
        bit_range = _contract(BitRange, bounds, f"{where}.bit_range")
    width = _int(data["width"], where)
    if width < 1:
        raise GraphFormatError(f"{where}: width must be at least 1")
    return SignalInfo(
        name=_str(data["name"], where),
        width=width,
        bit_range=bit_range,
        signed=_bool(data["signed"], where),
        kind=_enum(SignalKind, data["kind"], where),
        span=_decode_span(data["span"], where),
        is_aggregate=_bool(data["is_aggregate"], where),
    )


def _encode_diagnostic(diagnostic: Diagnostic) -> dict[str, object]:
    return {"code": diagnostic.code, "message": diagnostic.message}


def _decode_diagnostic(value: object) -> Diagnostic:
    return _contract(Diagnostic, _fields(value, {"code", "message"}, "diagnostic"), "diagnostic")


def _decode_source(value: object) -> SourceFile:
    return _contract(SourceFile, _fields(value, {"id", "path", "sha256"}, "source"), "source")


def _decode_span(value: object, where: str) -> SourceSpan | None:
    if value is None:
        return None
    data = _fields(value, {"source_id", "line", "end_line"}, f"{where}.span")
    return _contract(SourceSpan, data, f"{where}.span")


# --------------------------------------------------------------------------
# Field checks
# --------------------------------------------------------------------------


def _fields(value: object, names: Collection[str], where: str) -> dict[str, object]:
    """``value`` as an object with exactly the fields ``names``."""
    if not isinstance(value, dict):
        raise GraphFormatError(f"{where}: expected an object")
    expected, present = set(names), set(value)
    if present - expected:
        raise GraphFormatError(f"{where}: unknown fields {sorted(present - expected)}")
    if expected - present:
        raise GraphFormatError(f"{where}: missing fields {sorted(expected - present)}")
    return value


def _list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise GraphFormatError(f"{where}: expected a list")
    return value


def _int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphFormatError(f"{where}: expected an integer, got {value!r}")
    return value


def _bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise GraphFormatError(f"{where}: expected true or false, got {value!r}")
    return value


def _str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise GraphFormatError(f"{where}: expected a string, got {value!r}")
    return value


def _enum[E: (ir.GuardKind, CaseFlavor, EdgeType, ProcessKind, SignalKind, UnaryOp, BinaryOp,
              ir.SelectMode)](enum: type[E], value: object, where: str) -> E:  # fmt: skip
    try:
        return enum(_str(value, where))
    except ValueError as error:
        raise GraphFormatError(f"{where}: unknown {enum.__name__} {value!r}") from error


def _contract[M: (BitRange, Diagnostic, SourceFile, SourceSpan)](
    model: type[M], data: Mapping[str, object], where: str
) -> M:
    try:
        return model.model_validate(dict(data))
    except ValueError as error:
        raise GraphFormatError(f"{where}: {error}") from error
