from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from doccase_preprocessing.group_pipeline import run_group_pipeline


@pytest.mark.parametrize("blocked_stage", ["read", "model"])
@pytest.mark.parametrize("tail_sizes", [None, {"slow": 1, "fast": 1}])
def test_other_group_finishes_before_blocked_group_releases(blocked_stage, tail_sizes):
    # The gate also requires the first group's own timeout to fall back.
    blocked, release, other_done = Event(), Event(), Event()
    outcomes = {}

    def intake(group, submit):
        if group == "fast":
            assert blocked.wait(4)
        submit(group)

    def read(group, job):
        if group == "slow" and blocked_stage == "read":
            blocked.set()
            assert release.wait(4)
        return "local:" + job

    def plan(group, rows):
        return rows[0], [group]

    def model(group, request):
        if group == "slow" and blocked_stage == "model":
            blocked.set()
            assert release.wait(4)
            raise TimeoutError
        return "remote:" + request

    def finish(group, local, calls):
        outcomes[group] = (local, calls)
        if group == "fast":
            other_done.set()
        return outcomes[group]

    with ThreadPoolExecutor(max_workers=1) as background:
        future = background.submit(
            run_group_pipeline,
            ["slow", "fast"],
            intake=intake,
            read=read,
            plan=plan,
            model=model,
            finish=finish,
            read_workers=2,
            read_queue=1,
            model_workers=2,
            read_group_sizes=tail_sizes,
        )
        try:
            assert other_done.wait(4)
            assert "slow" not in outcomes
        finally:
            release.set()
        result = future.result(timeout=4)
    assert result["fast"] == ("local:fast", [("fast", "remote:fast", None)])
    assert result["slow"] == (
        "local:slow",
        [("slow", None, "timeout")]
        if blocked_stage == "model"
        else [("slow", "remote:slow", None)],
    )


def test_read_queue_applies_backpressure_without_waiting_for_intake_to_finish():
    reading, trying_third, third_accepted, release = Event(), Event(), Event(), Event()

    def intake(group, submit):
        submit(1)
        assert reading.wait(4)
        submit(2)
        trying_third.set()
        submit(3)
        third_accepted.set()

    def read(group, job):
        if job == 1:
            reading.set()
            assert release.wait(4)
        return job

    with ThreadPoolExecutor(max_workers=1) as background:
        future = background.submit(
            run_group_pipeline,
            ["g"],
            intake=intake,
            read=read,
            plan=lambda g, rows: (rows, []),
            model=lambda g, req: None,
            finish=lambda g, local, calls: local,
            read_workers=1,
            read_queue=1,
        )
        try:
            assert trying_third.wait(4)
            assert not third_accepted.is_set()
        finally:
            release.set()
        assert future.result(timeout=4) == {"g": [1, 2, 3]}


def test_priority_cannot_bypass_an_older_material_more_than_four_times():
    admitted, release, holding = Event(), Event(), Event()
    order = []

    def intake(group, submit):
        for job in ["hold", "ordinary"] if group == "first" else ["p1", "p2", "p3", "p4", "p5"]:
            submit(job)
            if job == "hold":
                assert holding.wait(4)
        if group == "priority":
            admitted.set()

    def read(group, job):
        order.append(job)
        if job == "hold":
            holding.set()
            assert release.wait(4)
        return job

    with ThreadPoolExecutor(max_workers=1) as background:
        future = background.submit(
            run_group_pipeline,
            ["first", "priority"],
            intake=intake,
            read=read,
            plan=lambda g, rows: (rows, []),
            model=lambda g, req: None,
            finish=lambda g, local, calls: local,
            read_workers=1,
            read_queue=6,
            read_priority=lambda g, job: 0 if job.startswith("p") else 2,
        )
        try:
            assert admitted.wait(4)
        finally:
            release.set()
        assert future.result(timeout=4)["first"] == ["hold", "ordinary"]
    assert order == ["hold", "p1", "p2", "p3", "p4", "ordinary", "p5"]


@pytest.mark.parametrize("tail", [False, True])
def test_read_priority_changes_when_a_group_has_one_material_left(tail):
    started, release, queued = Event(), Event(), Event()
    order = []
    work = {"closing": ["a1", "a2"], "ordinary": ["b1", "b2", "b3"], "packing": ["p"]}

    def intake(group, submit):
        for job in work[group]:
            submit(job)
            if job == "a1":
                assert started.wait(4)
        if group == "packing":
            queued.set()

    def read(group, job):
        order.append(job)
        if job == "a1":
            started.set()
            assert release.wait(4)
        return job

    with ThreadPoolExecutor(max_workers=1) as background:
        future = background.submit(
            run_group_pipeline,
            list(work),
            intake=intake,
            read=read,
            plan=lambda g, rows: (rows, []),
            model=lambda g, req: None,
            finish=lambda g, local, calls: local,
            read_workers=1,
            read_queue=5,
            read_priority=lambda g, job: {"p": 0, "a2": 2}.get(job, 1),
            read_group_sizes={g: len(jobs) for g, jobs in work.items()} if tail else None,
        )
        try:
            assert queued.wait(4)
        finally:
            release.set()
        assert future.result(timeout=4) == work
    assert order[:3] == (["a1", "p", "a2"] if tail else ["a1", "p", "b1"])


def test_tail_policy_rejects_incomplete_material_inventory():
    with pytest.raises(RuntimeError, match="Group pipeline failed") as error:
        run_group_pipeline(
            ["g"],
            intake=lambda g, submit: submit(1),
            read=lambda g, job: job,
            plan=lambda g, rows: (rows, []),
            model=lambda g, req: None,
            finish=lambda g, local, calls: local,
            read_group_sizes={"g": 2},
        )
    assert str(error.value.__cause__) == "Intake did not reach its planned material count"
