"""Schedule independent business groups through bounded READ and model stages."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from queue import SimpleQueue
from threading import BoundedSemaphore, Condition


@dataclass
class _ReadJob:
    group: str
    payload: object
    priority: int
    future: Future
    bypasses: int = 0


class _FairReaders:
    """Prefer a pending job's rank, but bound overtaking by newer jobs."""

    def __init__(self, slots, read, group_sizes=None):
        self._read = read
        self._sizes = group_sizes or {}
        self._remaining = dict(self._sizes)
        self._pending = []
        self._condition = Condition()
        self._closed = False
        self._threads = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="group-read")
        for _ in range(slots):
            self._threads.submit(self._drain)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._threads.shutdown()

    def submit(self, group, job, priority):
        future = Future()
        with self._condition:
            self._pending.append(_ReadJob(group, job, priority, future))
            self._condition.notify()
        return future

    def _drain(self):
        def rank(job):
            remaining = self._remaining.get(job.group)
            closing = (
                remaining is not None and 0 < remaining <= 2 and remaining < self._sizes[job.group]
            )
            return (
                (0, job.priority)
                if job.priority <= 0
                else (1, remaining)
                if closing
                else (2, job.priority)
            )

        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if not self._pending:
                    return
                oldest_due = next((i for i, j in enumerate(self._pending) if j.bypasses >= 4), None)
                picked = (
                    min(range(len(self._pending)), key=lambda i: rank(self._pending[i]))
                    if oldest_due is None
                    else oldest_due
                )
                for waiting in self._pending[:picked]:
                    waiting.bypasses += 1
                job = self._pending.pop(picked)
            try:
                try:
                    value = self._read(job.group, job.payload)
                finally:
                    with self._condition:
                        if job.group in self._remaining:
                            self._remaining[job.group] -= 1
            except Exception as exc:
                job.future.set_exception(exc)
            else:
                job.future.set_result(value)


def run_group_pipeline(
    groups,
    *,
    intake,
    read,
    plan,
    model,
    finish,
    read_workers=6,
    read_queue=6,
    model_workers=4,
    plan_workers=2,
    finish_workers=2,
    read_priority=None,
    read_admitted=None,
    read_group_sizes=None,
    context_followup=None,
):
    """Run the group stages with no cohort-wide stage barrier.

    `intake(group, submit)` submits each material as soon as it is prepared and
    returns when group membership is sealed. `plan(group, rows)` returns local
    state and the scoped requests. `finish(group, local, calls)` receives one
    outcome per request, including timeouts, and can retain the local state.
    The optional context_followup(group, validated_result) runs in the bounded
    model pool. A timeout retains the validated first pass.
    Model callbacks must impose a deadline on their I/O. Smaller read ranks run
    first; a pending material may be overtaken by at most four newer ones.
    With verified group sizes, prefer a group's last two unfinished materials
    once another read in that group completed. Rank zero remains reserved
    for the highest static priority (such as packing-list tables).
    """
    groups = tuple(groups)
    if len(set(groups)) != len(groups):
        raise ValueError("Business group IDs must be unique")
    if min(read_workers, model_workers, plan_workers, finish_workers) < 1 or read_queue < 0:
        raise ValueError("Invalid pipeline capacity")
    if read_group_sizes is not None:
        read_group_sizes = dict(read_group_sizes)
        if set(read_group_sizes) != set(groups) or any(
            type(n) is not int or n < 0 for n in read_group_sizes.values()
        ):
            raise ValueError("Expected one nonnegative material count per group")
    permits = BoundedSemaphore(read_workers + read_queue)
    events = SimpleQueue()
    state = {g: {"rows": {}, "expected": None, "calls": []} for g in groups}
    results, errors = {}, {}

    def enqueue(executor, kind, group, key, action, *args):
        future = executor.submit(action, *args)
        future.add_done_callback(lambda done: events.put((kind, group, key, done)))

    def produce():
        for group in groups:
            count = 0

            def submit(job, group=group):
                nonlocal count
                if read_group_sizes is not None and count >= read_group_sizes[group]:
                    raise ValueError("Intake exceeded its planned material count")
                permits.acquire()
                n = count
                try:
                    if read_priority is None and read_group_sizes is None:
                        if read_admitted is not None:
                            read_admitted(group, job)
                        future = reads.submit(read, group, job)
                    else:
                        rank = read_priority(group, job) if read_priority is not None else 1
                        if read_admitted is not None:
                            read_admitted(group, job)
                        future = reads.submit(group, job, rank)
                except Exception:
                    permits.release()
                    raise
                count += 1

                def completed(done):
                    permits.release()
                    events.put(("read", group, n, done))

                future.add_done_callback(completed)

            try:
                intake(group, submit)
                if read_group_sizes is not None and count != read_group_sizes[group]:
                    raise ValueError("Intake did not reach its planned material count")
            except Exception as exc:
                events.put(("intake_error", group, None, exc))
            else:
                events.put(("sealed", group, count, None))

    def next_request(group, current):
        request_index = len(current["calls"])
        if request_index == len(current["requests"]):
            enqueue(
                finishes, "finished", group, None, finish, group, current["local"], current["calls"]
            )
        else:
            req = current["requests"][request_index]
            # One in-flight request per group. A long group cannot take all
            # remote slots or reserve a run of future slots in the model queue.
            enqueue(models, "model", group, req, model, group, req)

    readers = (
        _FairReaders(read_workers, read, read_group_sizes)
        if read_priority is not None or read_group_sizes is not None
        else ThreadPoolExecutor(max_workers=read_workers, thread_name_prefix="group-read")
    )
    with (
        readers as reads,
        ThreadPoolExecutor(max_workers=plan_workers, thread_name_prefix="group-plan") as plans,
        ThreadPoolExecutor(max_workers=model_workers, thread_name_prefix="group-model") as models,
        ThreadPoolExecutor(
            max_workers=finish_workers, thread_name_prefix="group-finish"
        ) as finishes,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="group-intake") as intake_pool,
    ):
        producer = intake_pool.submit(produce)
        while len(results) + len(errors) < len(groups):
            kind, group, key, value = events.get()
            if group in errors:
                continue
            current = state[group]
            try:
                if kind == "intake_error":
                    raise value
                if kind == "sealed":
                    current["expected"] = key
                elif kind == "read":
                    current["rows"][key] = value.result()
                elif kind == "planned":
                    current["local"], requests = value.result()
                    current["requests"] = tuple(requests)
                    next_request(group, current)
                elif kind == "model":
                    try:
                        outcome = (key, value.result(), None)
                    except TimeoutError:
                        outcome = (key, None, "timeout")
                    current["calls"].append(outcome)
                    next_request(group, current)
                elif kind == "finished":
                    base = value.result()
                    if context_followup is None:
                        results[group] = base
                    else:
                        current["base_result"] = base
                        enqueue(
                            models, "context_finished", group, None, context_followup, group, base
                        )
                elif kind == "context_finished":
                    try:
                        results[group] = value.result()
                    except TimeoutError:
                        results[group] = current["base_result"]
                if kind in {"read", "sealed"} and len(current["rows"]) == current["expected"]:
                    ordered = [current["rows"][i] for i in range(current["expected"])]
                    enqueue(plans, "planned", group, None, plan, group, ordered)
            except Exception as exc:
                errors[group] = exc
        producer.result()
    if errors:
        group, cause = next(iter(errors.items()))
        raise RuntimeError(
            f"Group pipeline failed for {group!r}; {len(errors)} group(s) failed"
        ) from cause
    return {group: results[group] for group in groups}
