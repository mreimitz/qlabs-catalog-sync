"""The corpus contract: equivalent values collide, different values never do.

This is the test the idempotency claim rests on. If a re-run over an unchanged source is
to perform zero writes, every representational difference an endpoint can introduce has
to hash the same — and every real difference has to hash differently.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from itertools import combinations
from pathlib import Path

import pytest
from envelope_corpus import CLASSES, INSTANT_CEST

from qlabs_catalog_sync_sdk.envelope import ArrayOrder, canonical_json, compute_checksum

CORPUS_PATH = Path(__file__).parent / "envelope_corpus.py"

# Reading the corpus back out of the module proves the same digest across processes.
_CHILD_PROGRAM = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("envelope_corpus", sys.argv[1])
assert spec is not None and spec.loader is not None
corpus = importlib.util.module_from_spec(spec)
spec.loader.exec_module(corpus)

from qlabs_catalog_sync_sdk.envelope import compute_checksum

print(
    json.dumps(
        {
            label: [compute_checksum(member, order=order) for member in members]
            for label, order, members in corpus.CLASSES
        }
    )
)
"""


def _checksums() -> dict[str, list[str]]:
    return {
        label: [compute_checksum(member, order=order) for member in members]
        for label, order, members in CLASSES
    }


def test_the_corpus_has_no_duplicate_labels() -> None:
    labels = [label for label, _order, _members in CLASSES]
    assert len(labels) == len(set(labels))


@pytest.mark.parametrize(("label", "order", "members"), CLASSES, ids=[c[0] for c in CLASSES])
def test_every_representation_of_one_value_hashes_the_same(
    label: str, order: ArrayOrder, members: list[object]
) -> None:
    digests = {compute_checksum(member, order=order) for member in members}
    rendered = [canonical_json(member, order=order) for member in members]
    assert len(digests) == 1, f"{label} disagrees with itself: {rendered}"


def test_distinct_logical_values_never_collide() -> None:
    """Pairwise across the entire corpus, not a hand-picked pair or two."""
    representatives = {
        label: compute_checksum(members[0], order=order) for label, order, members in CLASSES
    }
    collisions = [
        (left, right)
        for (left, left_digest), (right, right_digest) in combinations(representatives.items(), 2)
        if left_digest == right_digest
    ]
    assert collisions == []


def test_null_empty_string_empty_object_empty_array_and_absent_all_differ() -> None:
    digests = {
        "null": compute_checksum(None),
        "empty string": compute_checksum(""),
        "empty object": compute_checksum({}),
        "empty array": compute_checksum([]),
    }
    assert len(set(digests.values())) == len(digests)
    # A key set to null is not the same as a key that is not there. The engine reads this
    # as "clear the target" versus "the source said nothing about this field".
    assert compute_checksum({"a": None}) != compute_checksum({})
    assert compute_checksum({"a": None}) != compute_checksum({"a": ""})


def test_the_checksum_is_stable_within_a_process() -> None:
    first = _checksums()
    second = _checksums()
    assert first == second


@pytest.mark.parametrize("hash_seed", ["0", "1", "4242"])
def test_the_checksum_is_stable_across_processes_and_hash_seeds(hash_seed: str) -> None:
    """No ``hash()``, no ``id()``, no clock, and no set iteration order leaking in.

    ``PYTHONHASHSEED`` randomizes string hashing, which is what would make a ``set`` or a
    dict built from one iterate differently between runs. Same digests under three seeds
    in three fresh interpreters means nothing process-local reached the checksum.
    """
    environment = dict(os.environ, PYTHONHASHSEED=hash_seed)
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_PROGRAM, str(CORPUS_PATH)],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    assert json.loads(completed.stdout) == _checksums(), completed.stderr


def test_checksums_are_prefixed_sha256_hex() -> None:
    digest = compute_checksum({"name": "Retail Sales"})
    algorithm, _, hexdigest = digest.partition(":")
    assert algorithm == "sha256"
    assert len(hexdigest) == 64
    assert set(hexdigest) <= set("0123456789abcdef")


# Pinned digests. These are persisted state: if one of these changes, every checksum
# already in a state store is wrong, so it has to be a deliberate, migrated decision --
# never a silent side effect of touching the normalizer.
GOLDEN: list[tuple[str, object, ArrayOrder, str]] = [
    (
        "empty string",
        "",
        ArrayOrder.PRESERVE,
        "sha256:12ae32cb1ec02d01eda3581b127c1fee3b0dc53572ed6baf239721a03d82e126",
    ),
    (
        "plain text",
        "Retail Sales",
        ArrayOrder.PRESERVE,
        "sha256:b796ab9f3f94956a147fa160eae6964a434bd1778429e519dc1b2b821f3ce9c7",
    ),
    (
        "object",
        {"a": 1, "b": 2},
        ArrayOrder.PRESERVE,
        "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
    ),
    (
        "null",
        None,
        ArrayOrder.PRESERVE,
        "sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
    ),
    (
        "instant",
        INSTANT_CEST,
        ArrayOrder.PRESERVE,
        "sha256:a81a6a3924d0d088a7ae2464a758cfcd3866e0a5c8b0149eb32f572f2976de8c",
    ),
    (
        "sorted array",
        ["z", "x", "y"],
        ArrayOrder.SORTED,
        "sha256:c23c8115d0239045c86c573e521a3d39d18f96b7508e7e7255ec4da76ad1d514",
    ),
]


@pytest.mark.parametrize(
    ("label", "value", "order", "expected"), GOLDEN, ids=[g[0] for g in GOLDEN]
)
def test_pinned_digests_still_hold(
    label: str, value: object, order: ArrayOrder, expected: str
) -> None:
    assert compute_checksum(value, order=order) == expected
