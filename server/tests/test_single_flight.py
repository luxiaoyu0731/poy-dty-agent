"""Contract for the shared per-key single-flight helper."""

import threading

import pytest

from app.single_flight import SingleFlight


def test_waiters_share_one_build_result_and_failure():
    sf: SingleFlight[dict] = SingleFlight()
    started = threading.Event()
    release = threading.Event()
    calls = []

    def build():
        calls.append(1)
        started.set()
        assert release.wait(timeout=5)
        return {"ok": True}

    results: list = []
    errors: list = []
    owner = threading.Thread(target=lambda: results.append(sf.run("k", build, join_timeout=5)))
    owner.start()
    assert started.wait(timeout=5)
    joiners = [threading.Thread(target=lambda: _collect(sf, results, errors)) for _ in range(3)]
    for thread in joiners:
        thread.start()
    release.set()
    owner.join(timeout=5)
    for thread in joiners:
        thread.join(timeout=5)
    assert errors == []
    assert len(calls) == 1
    assert all(item[0] == {"ok": True} for item in results)
    assert [item[1] for item in results].count(True) == 1


def _collect(sf: SingleFlight, results: list, errors: list, build=lambda: {"ok": True}) -> None:
    try:
        results.append(sf.run("k", build, join_timeout=5))
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)


def test_failure_propagates_to_waiters_and_key_recovers():
    sf: SingleFlight[str] = SingleFlight()
    started = threading.Event()

    def bad():
        started.set()
        raise ValueError("build_failed")

    errors: list = []
    owner = threading.Thread(target=lambda: _collect(sf, [], errors, build=bad))
    owner.start()
    assert started.wait(timeout=5)
    with pytest.raises(ValueError, match="build_failed"):
        sf.run("k", bad, join_timeout=5)
    owner.join(timeout=5)
    assert errors and isinstance(errors[0], ValueError)
    # A later caller starts a fresh build; the failure is not sticky.
    assert sf.run("k", lambda: "recovered", join_timeout=5) == ("recovered", True)


def test_join_timeout_is_bounded_and_does_not_cancel_owner():
    sf: SingleFlight[str] = SingleFlight()
    started = threading.Event()
    release = threading.Event()
    owner_result: list = []

    def build():
        started.set()
        assert release.wait(timeout=5)
        return "late"

    owner = threading.Thread(target=lambda: owner_result.append(sf.run("k", build, join_timeout=5)))
    owner.start()
    assert started.wait(timeout=5), "owner build must start"
    # A second caller joins with a short deadline while the owner still builds.
    with pytest.raises(TimeoutError, match="single_flight_join_timeout"):
        sf.run("k", lambda: "unused", join_timeout=0.2)
    release.set()
    owner.join(timeout=5)
    assert owner_result == [("late", True)], "owner publishes despite the waiter leaving"
    # The key is free; a later caller builds anew.
    assert sf.run("k", lambda: "next", join_timeout=5) == ("next", True)
