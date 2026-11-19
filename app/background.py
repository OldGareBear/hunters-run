"""Run background work after a request.

Locally (and in containers) the work runs in-process after the response, using
FastAPI's BackgroundTasks. On AWS Lambda the web function can't keep working after
it answers, so when TASKS_FUNCTION_NAME is set the work is handed to the tasks
function with an asynchronous invoke (see app/lambda_handler.py).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import lru_cache

from fastapi import BackgroundTasks

from app.config import get_settings

log = logging.getLogger(__name__)


@lru_cache
def _lambda_client():
    import boto3

    return boto3.client("lambda")


def invoke_task(task: str, **payload) -> bool:
    """Start a task on the tasks function without waiting. False if it couldn't start."""
    name = get_settings().tasks_function_name
    try:
        _lambda_client().invoke(
            FunctionName=name,
            InvocationType="Event",
            Payload=json.dumps({"task": task, **payload}).encode(),
        )
    except Exception:  # the scheduled worker picks up anything left behind
        log.exception("couldn't start background task %s", task)
        return False
    return True


def run_later(
    background: BackgroundTasks, task: str, local: Callable[..., None], **payload
) -> None:
    """Run `task` on the tasks function if there is one, else `local(**payload)` in-process."""
    if get_settings().tasks_function_name:
        background.add_task(invoke_task, task, **payload)
    else:
        background.add_task(local, **payload)
