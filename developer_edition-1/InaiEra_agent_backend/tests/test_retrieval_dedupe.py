"""
Retrieval collapses chunks whose content is identical before filling
top_k.

The reported symptom: the Sources list under a chat answer showed the
same FAQ entry twice, then the next one twice, then a third once.

The cause is a double-ingested knowledge base. The same source file
ingested twice leaves two chunk rows with different ids, identical text
and identical scores, and both are returned. Confirmed live on
`acme_support_faq`: a top_k of 5 returned chunk ids 24 and 44 with
byte-identical text and a score of 1.0 each, then 33 and 13 likewise —
three distinct facts spread over five slots. Measured across five KBs,
three were affected; `scen_general_hq` returned two distinct chunks out
of eight.

Deduping here is not a substitute for cleaning the KB — the wasted
storage and ingest cost are real — but it stops a duplicated corpus
from halving the context an agent gets, which raising top_k cannot fix
because the duplicates scale with it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.retrieval import _dedupe_by_content


def rows(*pairs):
    return [{"id": i, "content": c, "score": s} for i, c, s in pairs]


def test_identical_content_collapses_to_one():
    """The exact shape seen live: two ids, same text, same score."""
    out = _dedupe_by_content(rows(
        (24, "Q: How do I dispute an ACME charge? A: Open the invoice.", 1.0),
        (44, "Q: How do I dispute an ACME charge? A: Open the invoice.", 1.0),
    ))
    assert [r["id"] for r in out] == [24]


def test_distinct_content_is_untouched():
    out = _dedupe_by_content(rows(
        (1, "first fact", 0.9), (2, "second fact", 0.8), (3, "third fact", 0.7)))
    assert [r["id"] for r in out] == [1, 2, 3]


def test_whitespace_only_differences_still_collapse():
    """Two ingests of the same file can differ in trailing newlines or
    indentation without being different facts."""
    out = _dedupe_by_content(rows(
        (1, "same   text\nhere", 0.9),
        (2, "same text here", 0.9),
        (3, "  same text here  \n", 0.9)))
    assert [r["id"] for r in out] == [1]


def test_the_first_occurrence_wins_so_order_is_stable():
    """Rows arrive already sorted by score, so the first of an identical
    pair is the best-scoring one. Keeping the first also keeps ordering
    stable across queries for tied scores."""
    out = _dedupe_by_content(rows(
        (10, "dup", 0.9), (11, "dup", 0.9), (12, "unique", 0.5)))
    assert [r["id"] for r in out] == [10, 12]


def test_empty_content_rows_are_not_collapsed_into_one():
    """Otherwise every contentless row would fold together, which loses
    real results rather than duplicates."""
    out = _dedupe_by_content(rows((1, "", 0.5), (2, "", 0.4), (3, "real", 0.3)))
    assert [r["id"] for r in out] == [1, 2, 3]


def test_it_reads_text_when_content_is_absent():
    """Parent-child chunking returns `content`; some paths carry `text`."""
    out = _dedupe_by_content([
        {"id": 1, "text": "same", "score": 0.9},
        {"id": 2, "text": "same", "score": 0.9},
    ])
    assert [r["id"] for r in out] == [1]


def test_a_fully_duplicated_page_yields_the_distinct_half():
    """A KB ingested twice should still fill top_k with real variety
    rather than pairs — the whole point of the fix."""
    doubled = []
    for i in range(5):
        doubled.append({"id": i, "content": f"fact {i}", "score": 1.0 - i * 0.1})
        doubled.append({"id": 100 + i, "content": f"fact {i}", "score": 1.0 - i * 0.1})
    doubled.sort(key=lambda r: r["score"], reverse=True)
    out = _dedupe_by_content(doubled)
    assert len(out) == 5
    assert [r["content"] for r in out] == [f"fact {i}" for i in range(5)]


def test_an_empty_result_set_is_handled():
    assert _dedupe_by_content([]) == []
