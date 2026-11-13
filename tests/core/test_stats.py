"""Statistics: estimators checked against brute force or hand-computed values."""

from __future__ import annotations

from itertools import combinations

import pytest
from parallax.stats import cluster_bootstrap, mean_of_means, pass_hat_k


@pytest.mark.parametrize("n", range(1, 6))
def test_pass_hat_k_matches_enumerating_every_k_subset(n: int) -> None:
    for c in range(n + 1):
        outcomes = [True] * c + [False] * (n - c)
        for k in range(1, n + 1):
            subsets = list(combinations(outcomes, k))
            want = sum(all(s) for s in subsets) / len(subsets)
            assert pass_hat_k(n, c, k) == pytest.approx(want)


def test_pass_hat_k_is_undefined_with_fewer_repeats_than_k() -> None:
    assert pass_hat_k(2, 2, 3) is None


def test_cluster_bootstrap_is_deterministic_under_a_seed_and_brackets_the_estimate() -> None:
    per_task = {"a": [1.0, 1.0, 0.0], "b": [0.0, 0.0, 0.0], "c": [1.0, 0.0, 1.0], "d": [1.0, 1.0, 1.0]}
    first = cluster_bootstrap(per_task, mean_of_means, b=2_000, seed=0)
    assert first == cluster_bootstrap(per_task, mean_of_means, b=2_000, seed=0)
    assert first[0] <= mean_of_means(list(per_task.values())) <= first[1]
