"""Fork-safety of the worker's DB engine (see _reset_db_pool_after_fork).

The real failure mode (two prefork processes sharing one inherited TLS
connection) cannot be reproduced in-process, so these tests pin the two
load-bearing facts instead: the handler is connected to worker_process_init,
and it disposes the engine pool with close=False (forgetting inherited
connections without closing the parent's sockets).
"""
from celery.signals import worker_process_init

from app.workers import tasks as worker_tasks


def test_reset_db_pool_handler_is_connected_to_worker_process_init():
    receivers = [ref() for _, ref in worker_process_init.receivers]
    assert worker_tasks._reset_db_pool_after_fork in receivers


def test_reset_db_pool_disposes_engine_without_closing_parent_sockets(monkeypatch):
    calls = []
    monkeypatch.setattr(
        worker_tasks.engine, 'dispose', lambda close=True: calls.append(close)
    )
    worker_tasks._reset_db_pool_after_fork(sender=None)
    assert calls == [False]
