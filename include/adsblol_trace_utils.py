from __future__ import annotations

from typing import Any, Optional


def num(value: Any) -> Optional[float]:
    # readsb traces carry "ground" and null in numeric slots; only a real number is a value.
    return float(value) if isinstance(value, (int, float)) else None


def trace_preamble(trace_doc: dict[str, Any]) -> Optional[tuple[list[Any], str, float]]:
    points = trace_doc.get("trace") or []
    if not points:
        return None
    icao = (trace_doc.get("icao") or "").lower()
    base = trace_doc.get("timestamp")
    # '~'-prefixed addresses are TIS-B/non-ICAO synthetics — not real airframes.
    if not icao or icao.startswith("~") or base is None:
        return None
    return points, icao, base
