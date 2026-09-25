"""Strict FIFO across users. Checkpoint-swap reordering survives, but only
inside the head owner's own run -- it must never overtake anyone else."""

from __future__ import annotations

from alchemyui.config import Settings
from alchemyui.queue import Job, QueueManager


def _qm(resident: str | None) -> QueueManager:
    qm = QueueManager(db=None, comfy=None, registry=None, settings=Settings())
    qm._resident_checkpoint = resident
    return qm


def _job(user: int | None, ckpt: str) -> Job:
    return Job(workflow_id="w", params={"checkpoint": ckpt}, user_id=user)


def _drain(qm: QueueManager) -> list[Job]:
    order = []
    while qm._pending:
        order.append(qm._pick_next())
    return order


def test_another_users_job_is_never_overtaken():
    qm = _qm("fast")
    a1, b1, a2 = _job(1, "slow"), _job(2, "fast"), _job(1, "fast")
    qm._pending = [a1, b1, a2]
    # v1 would have run a2 first (its model is loaded), jumping bob's b1
    assert _drain(qm) == [a1, b1, a2]


def test_one_users_own_jobs_are_still_grouped_by_checkpoint():
    qm = _qm("fast")
    a1, a2, b1 = _job(1, "slow"), _job(1, "fast"), _job(2, "slow")
    qm._pending = [a1, a2, b1]
    assert _drain(qm) == [a2, a1, b1]


def test_a_starved_job_cannot_jump_another_user():
    qm = _qm("fast")
    a1, b1 = _job(1, "fast"), _job(2, "slow")
    b1.skips = 99
    qm._pending = [a1, b1]
    assert _drain(qm) == [a1, b1]


def test_without_owners_it_behaves_exactly_as_v1():
    qm = _qm("fast")
    slow, fast = _job(None, "slow"), _job(None, "fast")
    qm._pending = [slow, fast]
    assert _drain(qm) == [fast, slow]
