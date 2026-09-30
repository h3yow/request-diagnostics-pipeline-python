"""Sampler boundary and determinism tests."""

import pytest

from diagnostics.sampling import Sampler


def test_rate_zero_never_samples() -> None:
    sampler = Sampler(0.0)
    assert not any(sampler.sample(f"id-{i}") for i in range(500))


def test_rate_one_always_samples() -> None:
    sampler = Sampler(1.0)
    assert all(sampler.sample(f"id-{i}") for i in range(500))


def test_decision_matches_bucket_boundary_exactly() -> None:
    sampler = Sampler(0.25)
    for i in range(500):
        rid = f"boundary-{i}"
        bucket = sampler.bucket(rid)
        assert 0.0 <= bucket < 1.0
        assert sampler.sample(rid) is (bucket < 0.25)


def test_decision_is_deterministic_for_same_id() -> None:
    sampler = Sampler(0.5)
    assert all(sampler.sample("stable") == sampler.sample("stable")
               for _ in range(20))


def test_higher_rate_only_adds_sampled_ids() -> None:
    ids = [f"mono-{i}" for i in range(1000)]
    for low, high in ((0.0, 0.1), (0.1, 0.5), (0.5, 1.0)):
        sampled_low = {i for i in ids if Sampler(low).sample(i)}
        sampled_high = {i for i in ids if Sampler(high).sample(i)}
        assert sampled_low <= sampled_high


def test_empirical_rate_close_to_configured() -> None:
    n = 5000
    decisions = [Sampler(0.3).sample(f"emp-{i}") for i in range(n)]
    ratio = sum(decisions) / n
    assert 0.27 < ratio < 0.33


def test_invalid_rate_rejected() -> None:
    with pytest.raises(ValueError):
        Sampler(-0.1)
    with pytest.raises(ValueError):
        Sampler(1.1)
