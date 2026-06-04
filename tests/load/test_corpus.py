"""
Pre-run corpus sanity check (ADR-0015).

No AWS, no LLM call: samples the seeded 200-document set, parses each PDF
locally, and asserts the realized size/token distribution sits inside the run
envelope before any traffic is generated or any dollar is spent. Marked ``load``
so it runs under ``make load`` alongside the scenarios.
"""

import pytest

from . import corpus

pytestmark = pytest.mark.load


def test_corpus_within_envelope() -> None:
    docs = corpus.sample()
    assert len(docs) == corpus.SAMPLE_SIZE
    assert len({d.name for d in docs}) == corpus.SAMPLE_SIZE, "sample has duplicates"

    report = corpus.CorpusReport(corpus.profile(docs))
    print("\n" + report.summary())
    for s in report.oversized:
        print(f"  review: {s.name} ~{s.est_input_tokens:,} est input tokens")

    report.check()
