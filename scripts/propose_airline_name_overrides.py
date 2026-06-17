from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from urllib.parse import urlparse

# Reuse the build script as the single source of truth for the Mictronics feed + curated overrides.
from build_dim_airlines import _DESIGNATOR, _FIX, _NAME, _clean, SOURCE_URL

# Dev-only one-shot: cross-reference Wikidata airline labels against Mictronics to PROPOSE clean-name
# overrides for human review, then bake the accepted set into build_dim_airlines.py as _WIKIDATA_NAME.
# Not a runtime/build dependency — run manually when refreshing the airline dim.

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
# rdfs:label form, NOT SERVICE wikibase:label — the label service returned empty bindings here.
_QUERY = """
SELECT ?code ?label WHERE {
  ?airline wdt:P230 ?code .
  ?airline rdfs:label ?label .
  FILTER(LANG(?label) = "en")
}
"""
_UA = "sancha1090-dim-airlines/1.0 (airline-name proposer; contact via repo)"

# Words a prefix-match must NOT silently drop — they change what the carrier does, not just its styling.
_MEANINGFUL = ("cargo", "express")

# Legal-entity noise that a Wikidata brand label legitimately drops; longest-first for greedy strip.
_NOISE_SUFFIXES = sorted(
    {
        "limited", "ltd", "corporation", "corp", "inc", "incorporated", "company", "co",
        "jsc", "gmbh", "plc", "sa", "as", "llc", "pte", "group", "holdings",
        "coltd", "ptyltd", "pteltd", "corporationlimited",
    },
    key=len,
    reverse=True,
)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _strip_noise(remainder: str) -> bool:
    # True iff `remainder` is made up entirely of stacked legal-entity noise suffixes.
    while remainder:
        for suf in _NOISE_SUFFIXES:
            if remainder.endswith(suf):
                remainder = remainder[: -len(suf)]
                break
        else:
            return False
    return True


def fetch_wikidata() -> dict[str, list[str]]:
    parsed = urlparse(WIKIDATA_SPARQL)
    if parsed.scheme != "https" or parsed.netloc != "query.wikidata.org":
        raise ValueError(f"unsupported SPARQL endpoint: {WIKIDATA_SPARQL}")
    data = urllib.parse.urlencode({"query": _QUERY, "format": "json"}).encode()
    req = urllib.request.Request(
        WIKIDATA_SPARQL, data=data,
        headers={"User-Agent": _UA, "Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 — hardcoded https const, validated above
        payload = json.loads(resp.read().decode("utf-8"))
    labels: dict[str, list[str]] = defaultdict(list)
    for b in payload["results"]["bindings"]:
        code = b["code"]["value"].strip().upper()
        label = b["label"]["value"].strip()
        if _DESIGNATOR.fullmatch(code) and label and label not in labels[code]:
            labels[code].append(label)
    return labels


def fetch_mictronics() -> dict[str, str]:
    parsed = urlparse(SOURCE_URL)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
        raise ValueError(f"unsupported source URL: {SOURCE_URL}")
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:  # noqa: S310 — hardcoded https const, validated above
        raw = json.loads(resp.read().decode("utf-8"))
    out = {}
    for icao in raw:
        if _DESIGNATOR.fullmatch(icao):
            out[icao] = _clean(raw[icao].get("n") or "")
    return out


def classify(m: str, label: str) -> tuple[str, str]:
    # Returns (verdict, label) where verdict is accept-* / ambiguous / "" (no relation).
    nm, nl = _norm(m), _norm(label)
    if not nm or not nl:
        return "", label
    if nm == nl:
        return ("casing" if m != label else "", label)
    if not nm.startswith(nl):
        return "", label
    remainder = nm[len(nl):]
    if _strip_noise(remainder):
        return "suffix", label
    if any(w in remainder for w in _MEANINGFUL):  # dropped a load-bearing word (e.g. "cargo") → not safe
        return "ambiguous", label
    if " " not in m:
        # Mashed single token (hub city welded on); guard against absurd truncations.
        return ("mashed" if len(nl) >= 0.5 * len(nm) else "ambiguous", label)
    return "ambiguous", label


def main() -> int:
    mict = fetch_mictronics()
    wiki = fetch_wikidata()

    accepted: dict[str, tuple[str, str, str]] = {}   # icao -> (new_name, reason, mictronics_name)
    review: list[tuple[str, str, str]] = []          # (icao, mictronics_name, wikidata_label)
    exact = 0

    for icao, m in sorted(mict.items()):
        if icao in _NAME or icao in _FIX:            # already hand-curated; leave alone
            continue
        labels = wiki.get(icao)
        if not labels:
            continue
        best_accept = None
        best_review = None
        matched_exact = False
        # Deterministic across re-runs: Wikidata binding order isn't stable, so sort labels and rank
        # accepts by verdict (safest first), alphabetical tiebreak — never let SPARQL order pick.
        priority = {"casing": 0, "suffix": 1, "mashed": 2}
        for label in sorted(labels, key=str.casefold):
            verdict, lab = classify(m, label)
            if verdict in priority:
                cand = (priority[verdict], lab, verdict)
                if best_accept is None or cand < best_accept:
                    best_accept = cand
            elif verdict == "ambiguous":
                if best_review is None or lab.casefold() < best_review.casefold():
                    best_review = lab
            elif verdict == "" and _norm(m) == _norm(lab):
                matched_exact = True
        if best_accept:
            _, lab, verdict = best_accept
            accepted[icao] = (lab, verdict, m)
        elif best_review:
            review.append((icao, m, best_review))
        elif matched_exact:
            exact += 1

    print(f"# Mictronics codes: {len(mict)}  |  in Wikidata: {sum(1 for c in mict if c in wiki)}")
    print(f"# exact (no change): {exact}  |  auto-accept: {len(accepted)}  |  manual review: {len(review)}\n")

    print("_WIKIDATA_NAME = {")
    for icao in sorted(accepted):
        new, reason, old = accepted[icao]
        print(f'    {icao!r}: {new!r},  # {reason}: {old!r}')
    print("}\n")

    print("# ---- NEEDS MANUAL REVIEW (prefix drops a meaningful word) ----")
    for icao, m, lab in sorted(review):
        print(f"#   {icao}: mictronics={m!r}  wikidata={lab!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
