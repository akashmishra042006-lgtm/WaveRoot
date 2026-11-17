"""Hierarchy glob patterns for selecting canonical signal paths.

A pattern is matched against a whole canonical path, component by component,
with both split on ``.``:

* ``*`` matches any run of characters (possibly empty) within one component.
* ``?`` matches exactly one character within one component.
* ``**`` as a whole component matches zero or more whole components.
* Everything else, including ``[`` and ``]`` in array indices, is literal, and
  matching is case-sensitive.

Examples: ``tb.dut.*``, ``tb.**.clk``, ``tb.dut.w?tr``, ``tb.gen[2].*``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache

from src.contracts import InvalidQueryError

_DEEP: object = object()


def _component_regex(component: str, pattern: str) -> re.Pattern[str]:
    if not component:
        raise InvalidQueryError(f"pattern {pattern!r} has an empty component")
    if "**" in component:
        raise InvalidQueryError(
            f"pattern {pattern!r}: '**' must be a whole component, not part of {component!r}"
        )
    parts = []
    for char in component:
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts), re.DOTALL)


@lru_cache(maxsize=256)
def _compile(pattern: str) -> tuple[re.Pattern[str] | object, ...]:
    if not isinstance(pattern, str) or not pattern:
        raise InvalidQueryError(f"pattern {pattern!r} must be a non-empty string")
    compiled: list[re.Pattern[str] | object] = []
    for component in pattern.split("."):
        if component == "**":
            if not compiled or compiled[-1] is not _DEEP:  # '**.**' is the same as '**'
                compiled.append(_DEEP)
        else:
            compiled.append(_component_regex(component, pattern))
    return tuple(compiled)


def _matches(compiled: tuple[re.Pattern[str] | object, ...], components: list[str]) -> bool:
    # reachable[j]: the first i pattern elements can match the first j components.
    reachable = [True] + [False] * len(components)
    for element in compiled:
        if element is _DEEP:
            for j in range(1, len(reachable)):
                reachable[j] = reachable[j] or reachable[j - 1]
        else:
            assert isinstance(element, re.Pattern)
            for j in range(len(components), 0, -1):
                reachable[j] = reachable[j - 1] and element.fullmatch(components[j - 1]) is not None
            reachable[0] = False
    return reachable[-1]


def path_matches(path: str, pattern: str) -> bool:
    """Whether canonical ``path`` matches ``pattern``."""
    return _matches(_compile(pattern), path.split("."))


def check_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    """Validate ``patterns`` and return them as a tuple."""
    if isinstance(patterns, str):
        raise InvalidQueryError("patterns must be a sequence of strings, not one string")
    result = tuple(patterns)
    if not result:
        raise InvalidQueryError("at least one pattern is required")
    for pattern in result:
        if not isinstance(pattern, str):
            raise InvalidQueryError(f"pattern {pattern!r} must be a string")
        _compile(pattern)
    return result


def match_paths(paths: Sequence[str], patterns: Sequence[str]) -> tuple[str, ...]:
    """Paths matching any pattern, in input order and without duplicates.

    Raises ``InvalidQueryError`` for a malformed pattern or one that matches
    no path, naming the pattern.
    """
    compiled = [(pattern, _compile(pattern)) for pattern in check_patterns(patterns)]
    used: set[str] = set()
    seen: set[str] = set()
    selected: list[str] = []
    for path in paths:
        components = path.split(".")
        hit = False
        for pattern, elements in compiled:
            if _matches(elements, components):
                used.add(pattern)
                hit = True
        if hit and path not in seen:
            seen.add(path)
            selected.append(path)
    for pattern, _ in compiled:
        if pattern not in used:
            raise InvalidQueryError(f"pattern {pattern!r} matches no signal path")
    return tuple(selected)
