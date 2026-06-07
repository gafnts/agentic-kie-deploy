"""
Seeded corpus sampling and pre-run sanity check for the load test (ADR-0015).

A fixed sample of the Kleister NDA *train* partition, reused in the same order
across both the burst and sustained scenarios so the arrival pattern is the only
variable that changes between runs (ADR-0015, "Corpus"). The PDFs are not
committed; they are materialized under a git-ignored directory via the pinned
``nda`` package (pyproject ``sources.nda``).

The sanity check parses each sampled PDF *locally*—the same text the extractor
feeds Gemini, with no LLM call—and confirms the realized size/token distribution
sits inside the run envelope: the extractor's 120s timeout and the Gemini Tier-1
4M input-TPM ceiling. A corpus of unusually long NDAs is the one input that could
approach either.
"""

import random
from dataclasses import dataclass
from pathlib import Path

from agentic_kie import PDFLoader

# Train partition, materialized under a git-ignored dir (see module docstring):
# `uv run nda --output_dir tests/load/documents` lays down train/documents/*.pdf.
CORPUS_DIR = Path(__file__).parent / "documents" / "train" / "documents"

SAMPLE_SIZE = 200
# Fixed seed: the identical 200 documents, in the identical order, for both
# scenarios. Changing it re-rolls the sample and breaks cross-run doc pairing.
SAMPLE_SEED = 0xC0FFEE

# Run envelope (ADR-0015).
EXTRACTOR_TIMEOUT_S = 120  # extractor Lambda timeout (ADR-0009)
TIER1_INPUT_TPM = 4_000_000  # Gemini Tier-1 input tokens / minute
STAGING_THROUGHPUT_PER_MIN = 60  # maximum_concurrency 10 / ~10s per doc
CHARS_PER_TOKEN = 4  # rough English heuristic for a pre-run estimate
# A single document above this warrants a look before a run: well past the
# benchmark's working size, and the one input that could near the 120s timeout.
REVIEW_INPUT_TOKENS = 50_000


@dataclass(frozen=True)
class Document:
    """One sampled corpus document, identified by its on-disk path."""

    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


def sample(n: int = SAMPLE_SIZE, seed: int = SAMPLE_SEED) -> list[Document]:
    """
    Return ``n`` distinct train-partition PDFs in a fixed, seeded order.

    Filenames are content hashes, so sorting them yields a stable base order
    independent of the filesystem; the seeded draw over that order is therefore
    reproducible as long as the materialized partition is unchanged.
    """
    pdfs = sorted(CORPUS_DIR.glob("*.pdf"))
    if len(pdfs) < n:
        raise FileNotFoundError(
            f"need {n} PDFs under {CORPUS_DIR} but found {len(pdfs)}. "
            "Materialize the Kleister NDA train partition first "
            "(pinned `nda` package; see ADR-0015)."
        )
    return [Document(p) for p in random.Random(seed).sample(pdfs, n)]


@dataclass(frozen=True)
class DocStat:
    """Per-document profile from a local parse (no LLM call)."""

    name: str
    size_bytes: int
    pages: int
    text_chars: int

    @property
    def est_input_tokens(self) -> int:
        return self.text_chars // CHARS_PER_TOKEN


def profile(docs: list[Document]) -> list[DocStat]:
    """Parse each document's text layer locally and measure its size."""
    loader = PDFLoader()
    stats: list[DocStat] = []
    for doc in docs:
        parsed = loader.load_bytes(doc.path.read_bytes(), name=doc.name)
        stats.append(
            DocStat(
                name=doc.name,
                size_bytes=doc.size_bytes,
                pages=parsed.page_count,
                text_chars=len(parsed.full_text),
            )
        )
    return stats


def _pct(values: list[int], p: float) -> float:
    """Linear-interpolated percentile of an integer sequence."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _row(label: str, values: list[int], scale: float = 1.0) -> str:
    cells = "".join(
        f"{v / scale:>12,.0f}"
        for v in (min(values), _pct(values, 0.5), _pct(values, 0.9), max(values))
    )
    return f"  {label:<18}{cells}"


@dataclass(frozen=True)
class CorpusReport:
    """Distribution + envelope verdict for a sampled corpus."""

    stats: list[DocStat]

    @property
    def peak_input_tpm(self) -> int:
        """Pessimistic input TPM: staging throughput against the largest doc."""
        heaviest = max(s.est_input_tokens for s in self.stats)
        return STAGING_THROUGHPUT_PER_MIN * heaviest

    @property
    def oversized(self) -> list[DocStat]:
        """Documents large enough to warrant a look before a run."""
        return [s for s in self.stats if s.est_input_tokens > REVIEW_INPUT_TOKENS]

    def check(self) -> None:
        """Raise if the sample falls outside the run envelope."""
        if self.peak_input_tpm >= TIER1_INPUT_TPM:
            raise AssertionError(
                f"peak input ~{self.peak_input_tpm:,} TPM exceeds the Tier-1 "
                f"ceiling ({TIER1_INPUT_TPM:,}); the sample is too token-heavy."
            )

    def summary(self) -> str:
        sizes = [s.size_bytes for s in self.stats]
        pages = [s.pages for s in self.stats]
        tokens = [s.est_input_tokens for s in self.stats]
        return "\n".join(
            [
                f"corpus sample: {len(self.stats)} docs "
                f"(seed {hex(SAMPLE_SEED)}, train partition)",
                f"  {'metric':<18}{'min':>12}{'median':>12}{'p90':>12}{'max':>12}",
                _row("size (KB)", sizes, scale=1024),
                _row("pages", pages),
                _row("est input tokens", tokens),
                "",
                f"  peak input ~{self.peak_input_tpm:,} TPM "
                f"vs Tier-1 {TIER1_INPUT_TPM:,} "
                f"({TIER1_INPUT_TPM / self.peak_input_tpm:.0f}x headroom)",
            ]
        )
