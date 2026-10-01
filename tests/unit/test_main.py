import pytest

from plough_backer.main import serve


def test_serve_backs_off_and_resets_after_healthy_run() -> None:
    now = [0.0]
    durations = iter([1, 1, 1, 700, 1])  # seconds each run lasts before failing
    sleeps: list[float] = []

    def run_once() -> None:
        try:
            now[0] += next(durations)
        except StopIteration:
            raise KeyboardInterrupt from None
        raise RuntimeError("telegram down")

    def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    with pytest.raises(KeyboardInterrupt):
        serve(run_once, sleep=sleep, clock=lambda: now[0])
    assert sleeps == [5, 10, 20, 5, 10]
