"""Zero-result "Did you mean" candidates (spec §6.1) — stdlib only.

No third-party dependency (the RESEARCH Package Legitimacy Audit rejected
both Levenshtein packages on the registry). Candidates come ONLY from the
stored term table (no summarisation, no generated content): each returned
surface is a term the index already knows.

Ranking (spec §6.1): (1) exact word overlap with ``q`` first, then
(2) Damerau-Levenshtein distance <= 2 on ANY word of ``q`` vs any word of the
term, then (3) shared 3-gram count. Ties break on doc_count desc, then
surface asc, so the ordering is deterministic.

The distance is a full-table optimal-string-alignment O(n*m) DP — terms and
query words are short (< 30 chars) and the candidate band is pre-filtered
(T-27.1-18), so the cost is bounded. (A rolling-row OSA needs to read
``d[i-2][j-2]`` while row i-2 is still live — a two-row ring buffer
incorrectly overwrites it, which is why the full table is used.)
"""

from __future__ import annotations

from dataclasses import dataclass

from hansard_gateway.index.extract import STOPWORDS, norm


@dataclass(frozen=True)
class TermRow:
    """One stored term: display surface + normalised form + doc count."""

    surface: str
    norm: str
    doc_count: int


def osa_distance(a: str, b: str, *, max_dist: int = 2) -> int:
    """Optimal-string-alignment edit distance with adjacent transpositions.

    Returns the true distance when it is <= ``max_dist`` and ``max_dist + 1``
    otherwise (early exit — callers only need the <= 2 band)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_dist:
        return max_dist + 1
    if a == b:
        return 0
    table = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        table[i][0] = i
    for j in range(lb + 1):
        table[0][j] = j
    for i in range(1, la + 1):
        row = table[i]
        up = table[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            best = up[j] + 1
            if row[j - 1] + 1 < best:
                best = row[j - 1] + 1
            if up[j - 1] + cost < best:
                best = up[j - 1] + cost
            if (
                i > 1 and j > 1
                and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]
            ):
                if table[i - 2][j - 2] + 1 < best:
                    best = table[i - 2][j - 2] + 1
            row[j] = best
        if min(row) > max_dist:
            return max_dist + 1
    result = table[la][lb]
    return result if result <= max_dist else max_dist + 1


def _words(text: str) -> list[str]:
    """Normalised, non-stopword words of ``text``."""
    return [w for w in norm(text).split(" ") if w and w not in STOPWORDS]


def _three_grams(words: list[str]) -> frozenset[str]:
    """Character 3-grams over the space-joined words (tier 3 ranking)."""
    text = " ".join(words)
    return frozenset(text[i : i + 3] for i in range(len(text) - 2))


def _in_band(row_norm: str, query_words: list[str]) -> bool:
    """Pre-filter (T-27.1-18): some query word within length 2 of SOME term
    word. Length is compared word-to-word (not query word vs whole norm —
    that wrongly excluded multi-word terms). Keeps the candidate band cheap
    against the few-thousand-term table."""
    if not row_norm:
        return False
    term_words = row_norm.split(" ")
    return any(
        abs(len(qw) - len(tw)) <= 2
        for qw in query_words
        for tw in term_words
    )


def _rank_tier(row_norm: str, query_words: list[str]) -> int:
    """0 = exact word overlap, 1 = DL distance <= 2 on any word pair."""
    term_words = [w for w in row_norm.split(" ") if w]
    if set(term_words) & set(query_words):
        return 0
    dl = min(
        (
            osa_distance(qw, tw)
            for qw in query_words
            for tw in term_words
        ),
        default=3,
    )
    return 1 if dl <= 2 else 2


def suggest_terms(*, query: str, index, limit: int) -> list[tuple[str, int]]:
    """Up to ``limit`` candidate term surfaces for a zero-result query.

    ``index`` is the wave-1 :class:`IndexService`; only its stored term table
    (``terms_by_norm``) is read. Returns (surface, doc_count) rows ranked per
    the spec §6.1 tiers. A query whose words are all stopwords, or an empty
    index, yields []."""
    rows: list[tuple[str, str, int]] = index.terms_by_norm()
    query_words = _words(query)
    query_set = set(query_words)
    query_grams = _three_grams(query_words)
    query_norm = norm(query)
    if not query_set or not rows:
        return []

    scored: list[tuple[tuple, str, int]] = []
    for surface, tnorm, doc_count in rows:
        if tnorm == query_norm:
            continue  # already searched with zero hits — not a suggestion
        if not _in_band(tnorm, query_words):
            continue
        shared = len(query_grams & _three_grams(
            [w for w in tnorm.split(" ") if w]
        ))
        key = (_rank_tier(tnorm, query_words), -shared, -doc_count, surface)
        scored.append((key, surface, doc_count))

    scored.sort(key=lambda t: t[0])
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for _key, surface, doc_count in scored:
        if surface in seen:
            continue
        seen.add(surface)
        out.append((surface, doc_count))
        if len(out) >= limit:
            break
    return out
