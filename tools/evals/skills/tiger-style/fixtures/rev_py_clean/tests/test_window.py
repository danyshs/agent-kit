import pytest

from src.window import EVENTS_MAX, Window, WindowFull, rate_per_s, snapshot


def test_old_stamps_expire() -> None:
    window = Window(1000)
    window.add(0)
    window.add(500)
    assert window.count(1200) == 1


def test_clock_going_backwards_is_rejected() -> None:
    window = Window(1000)
    window.add(10)
    with pytest.raises(ValueError):
        window.add(5)


def test_full_window_refuses_instead_of_dropping() -> None:
    window = Window(1000)
    for _ in range(EVENTS_MAX):
        window.add(0)
    with pytest.raises(WindowFull):
        window.add(0)
    assert window.count(0) == EVENTS_MAX


def test_rate_is_per_second() -> None:
    window = Window(2000)
    window.add(0)
    window.add(1)
    assert rate_per_s(window, 1) == 1.0


def test_snapshot_keeps_whole_stamps() -> None:
    window = Window(100_000)
    for stamp_ms in (1234, 5678, 9012):
        window.add(stamp_ms)
    assert snapshot(window, 9) == b"5678,9012"
    assert snapshot(window, 3) == b""
