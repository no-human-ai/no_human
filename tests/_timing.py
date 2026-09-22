"""Test-only vocabulary for expressing timing-sensitive claims WITHOUT
asserting a measured wall-clock duration against an absolute literal.

Motivation: `tests/test_no_wallclock_assertions.py` forbids
`assert <clock-derived expr> < <literal>` anywhere in `tests/*.py` (outside
the two files owned by ticket d41812aa), because that shape reddens on a
contended CI box even when the code under test is correct — the box is
slow, not the code. Every timing claim a test wants to make decomposes into
one of a small number of load-INSENSITIVE claims, and each has a helper
here:

  * "the call returned before the slow thing finished" -> `BlockingGate`
    (a happens-before proof, not a measurement).
  * "a wedge must become a named failure, not a silent hang" ->
    `must_not_hang` / `must_not_hang_async` (a *requested* watchdog bound,
    deliberately huge, never a claim about speed).
  * "a fact about a background thread/task is not true YET, but will become
    true shortly" -> `wait_until` (a watchdog poll, same non-measurement
    semantics as `must_not_hang`).
  * "operation X costs no more than N times operation Y, on THIS box, in
    THIS run" -> `calibrated_budget` (load inflates both sides together).
  * "the code called the slow thing exactly N times" -> `count_calls`.

None of these helpers, and no assertion built from them, should ever be
flagged by `tests/_clock_assertion_guard.py` — that guard explicitly
recognises calls to `calibrated_budget` and `must_not_hang*` by name and
does not taint their return values as measurements-against-literals.
"""
from __future__ import annotations

import asyncio
import functools
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class BlockingGate:
    """A fake for a slow dependency that proves, via happens-before facts
    (not a clock), whether the caller waited for it to finish.

    Usage: pass an instance (or a bound method) wherever the code under
    test would call the slow thing. `entered` flips the moment the call is
    made; `finished` flips only after `released` is set (or after
    `hang_budget` seconds, so a bug can never wedge the test suite itself
    — this is the one exempted wall-clock number in this module, a
    *requested* watchdog an order of magnitude above any plausible
    contended runtime, not a claim about speed).
    """

    value: Any = None
    hang_budget: float = 30.0
    entered: bool = field(default=False, init=False)
    finished: bool = field(default=False, init=False)
    released: threading.Event = field(default_factory=threading.Event, init=False)
    call_count: int = field(default=0, init=False)

    def release(self) -> None:
        self.released.set()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        self.entered = True
        self.released.wait(self.hang_budget)
        self.finished = True
        return self.value

    def snapshot_at_return(self) -> tuple[bool, bool]:
        """`(entered, finished)` read once, right after the production call
        under test returns. Callers assert `entered and not finished` to
        prove "the call was attempted but the caller did not wait for it."
        """
        return self.entered, self.finished

    def assert_caller_did_not_wait(self) -> None:
        entered, finished = self.snapshot_at_return()
        assert entered, "the gate was never called — the test proves nothing"
        assert not finished, (
            "the caller waited for the gated call to finish instead of "
            "returning immediately"
        )


def must_not_hang(fn: Callable[..., Any], *args: Any, budget: float = 60.0,
                   what: str = "", **kwargs: Any) -> Any:
    """Run `fn(*args, **kwargs)` in a helper thread and convert a wedge into
    a NAMED AssertionError instead of a silent CI timeout.

    `budget` is a watchdog, not a performance bound: it is set deliberately
    far above any plausible contended runtime so contention can never flip
    the verdict it produces (hang vs. no-hang). Do not tighten it to make a
    test "faster" — that reintroduces exactly the flake this module exists
    to remove.
    """
    result: list[Any] = []
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result.append(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join(budget)
    if thread.is_alive():
        raise AssertionError(
            f"{what or getattr(fn, '__name__', repr(fn))} did not return "
            f"within the {budget}s watchdog budget — it hung"
        )
    if error:
        raise error[0]
    return result[0] if result else None


async def must_not_hang_async(coro: Any, *, budget: float = 60.0,
                               what: str = "") -> Any:
    """`asyncio` counterpart of `must_not_hang`: awaits `coro` under
    `asyncio.wait_for`, converting a wedge into a named AssertionError.
    Same watchdog semantics — `budget` is a requested ceiling, not a
    performance claim.
    """
    try:
        return await asyncio.wait_for(coro, timeout=budget)
    except asyncio.TimeoutError as exc:
        raise AssertionError(
            f"{what or 'the awaited coroutine'} did not finish within the "
            f"{budget}s watchdog budget — it hung"
        ) from exc


def wait_until(predicate: Callable[[], bool], *, budget: float = 5.0,
               poll: float = 0.005, what: str = "") -> None:
    """Poll `predicate()` until it is true, raising a NAMED AssertionError if
    it never becomes true within `budget` seconds.

    For proving a happens-before FACT ("the background thread reached the
    gate") when the fact is not yet true at the instant control returns to
    the caller — e.g. `threading.Thread(...).start()` returns before the new
    thread necessarily runs. `budget` is a watchdog, exactly like
    `must_not_hang`'s: deliberately generous, never tightened to make a test
    "faster", and never itself the subject of a `<`/`<=` assertion against a
    measured duration — only the boolean `predicate()` is asserted.
    """
    deadline = time.monotonic() + budget
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"{what or 'the awaited condition'} did not become true "
                f"within the {budget}s watchdog budget"
            )
        time.sleep(poll)


def calibrated_budget(reference: Callable[[], Any], *, multiple: float,
                       reps: int = 5, floor: float = 0.0) -> float:
    """Time `reference()` `reps` times, IN THIS PROCESS, IMMEDIATELY BEFORE
    the measurement it will be compared against, and return
    `max(floor, multiple * median(reference_durations))`.

    This makes the resulting bound load-RELATIVE rather than load-ABSOLUTE:
    contention inflates the reference measurement and the thing being
    measured together, roughly proportionally, so the ratio between them
    stays stable even when the box is busy. Contrast with
    `assert elapsed < 100`, which only ever measured today's idle box.

    `floor` guards against a reference call so fast (e.g. a few
    microseconds) that measurement noise alone could make `multiple *
    median` unreasonably tight; it should be set well below any sane
    real-world value, never used to paper over a bound that is actually
    load-sensitive.
    """
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter()
        reference()
        samples.append(time.perf_counter() - t0)
    budget = max(floor, multiple * statistics.median(samples))
    return budget


def count_calls(monkeypatch: Any, target: Any, attr: str) -> list[int]:
    """Monkeypatch `target.attr` to count invocations without changing its
    behavior, returning a single-element list `[count]` (mutable, so the
    caller can read the live count via closure or `counter[0]` after the
    patched callable is used).
    """
    original = getattr(target, attr)
    counter = [0]

    @functools.wraps(original)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        counter[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(target, attr, _wrapped)
    return counter
