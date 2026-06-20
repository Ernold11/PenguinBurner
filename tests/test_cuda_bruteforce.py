from __future__ import annotations

from stability import cuda_bruteforce


def test_cuda_stress_seed_varies_each_launch_and_stays_u32() -> None:
    first = cuda_bruteforce._stress_seed(0x13579BDF, 0, 0xA5A5A5A5)
    second = cuda_bruteforce._stress_seed(0x13579BDF, 1, 0xA5A5A5A5)

    assert first != second
    assert 0 <= first <= 0xFFFFFFFF
    assert 0 <= second <= 0xFFFFFFFF


def test_cuda_verification_samples_cover_edges_and_middle() -> None:
    element_count = cuda_bruteforce.DEFAULT_STRESS_ELEMENTS

    indices = cuda_bruteforce._verification_sample_indices(element_count)

    assert indices[:4] == [0, 1, 2, 7]
    assert element_count // 2 in indices
    assert element_count - 1 in indices
    assert len(indices) == len(set(indices))
    assert all(0 <= index < element_count for index in indices)


def test_cuda_cpu_reference_is_deterministic_for_stress_rounds() -> None:
    seed0 = cuda_bruteforce._stress_seed(0x13579BDF, 17, 0xA5A5A5A5)
    seed1 = cuda_bruteforce._stress_seed(0x2468ACE1, 17, 0x5A5A5A5A)

    first = cuda_bruteforce._cpu_reference(
        4095,
        cuda_bruteforce.DEFAULT_STRESS_ROUNDS,
        seed0,
        seed1,
    )
    second = cuda_bruteforce._cpu_reference(
        4095,
        cuda_bruteforce.DEFAULT_STRESS_ROUNDS,
        seed0,
        seed1,
    )

    assert first == second
    assert all(0 <= value <= 0xFFFFFFFF for value in first)


def test_cuda_verify_interval_scales_for_short_runs_and_caps_long_runs() -> None:
    assert cuda_bruteforce._verify_interval_s(3.0) == 1.0
    assert cuda_bruteforce._verify_interval_s(5.0) == 5.0 / 3.0
    assert cuda_bruteforce._verify_interval_s(150.0) == 5.0
