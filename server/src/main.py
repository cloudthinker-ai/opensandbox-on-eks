# Copyright 2025 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
FastAPI application entry point for OpenSandbox Lifecycle API.

This module initializes the FastAPI application with middleware, routes,
and configuration for the sandbox lifecycle management service.
"""

import asyncio
import copy
import logging.config
import threading
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.config import load_config
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG

# Load configuration before initializing routers/middleware
app_config = load_config()

# Unify logging format (including uvicorn access/error logs) with timestamp prefix.
_log_config = copy.deepcopy(UVICORN_LOGGING_CONFIG)
_fmt = "%(levelprefix)s %(asctime)s %(name)s: %(message)s"
_datefmt = "%Y-%m-%d %H:%M:%S%z"

# Enable colors and set format for both default and access loggers
_log_config["formatters"]["default"]["fmt"] = _fmt
_log_config["formatters"]["default"]["datefmt"] = _datefmt
_log_config["formatters"]["default"]["use_colors"] = True

_log_config["formatters"]["access"]["fmt"] = _fmt
_log_config["formatters"]["access"]["datefmt"] = _datefmt
_log_config["formatters"]["access"]["use_colors"] = True

# Ensure project loggers (src.*) emit at configured level using the default handler.
_log_config["loggers"]["src"] = {
    "handlers": ["default"],
    "level": app_config.server.log_level.upper(),
    "propagate": False,
}

logging.config.dictConfig(_log_config)
logging.getLogger().setLevel(
    getattr(logging, app_config.server.log_level.upper(), logging.INFO)
)

from src.api.lifecycle import router  # noqa: E402
from src.middleware.auth import AuthMiddleware  # noqa: E402


logger = logging.getLogger(__name__)

_shutdown_event = threading.Event()
_POLL_INTERVAL_SECONDS = 2


def _wait_for_old_pods_drained(timeout: int) -> None:
    """Block until this pod is the only Ready server pod, or timeout expires."""
    import os
    import time

    from kubernetes import client as k8s_client

    my_pod = os.environ.get("HOSTNAME", "")
    if not my_pod:
        logger.warning("HOSTNAME not set, falling back to %ds delay", timeout)
        _shutdown_event.wait(timeout)
        return

    namespace = app_config.kubernetes.namespace
    core_api = k8s_client.CoreV1Api()

    # Discover the app label from our own pod metadata.
    try:
        own_pod = core_api.read_namespaced_pod(my_pod, namespace)
        app_label = (own_pod.metadata.labels or {}).get("app", "")
    except Exception:
        logger.warning("Could not read own pod labels, falling back to %ds delay", timeout)
        _shutdown_event.wait(timeout)
        return

    if not app_label:
        logger.warning("No 'app' label on own pod, falling back to %ds delay", timeout)
        _shutdown_event.wait(timeout)
        return

    label_selector = f"app={app_label}"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline and not _shutdown_event.is_set():
        try:
            pods = core_api.list_namespaced_pod(namespace, label_selector=label_selector)
        except Exception:
            logger.warning("K8s API error while polling for old pods, will retry")
            _shutdown_event.wait(_POLL_INTERVAL_SECONDS)
            continue
        other_ready = [
            p
            for p in pods.items
            if p.metadata.name != my_pod
            and p.status.phase == "Running"
            and any(cs.ready for cs in (p.status.container_statuses or []))
        ]
        if not other_ready:
            logger.info("Old server pod(s) drained, proceeding with sweep")
            return
        logger.debug("Waiting for %d old pod(s) to drain...", len(other_ready))
        _shutdown_event.wait(_POLL_INTERVAL_SECONDS)

    logger.warning(
        "Timed out waiting for old pods to drain after %ds, sweeping anyway", timeout
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(timeout=180.0)

    # Defer stale-sandbox sweep so it runs after the old pod is drained.
    async def _deferred_sweep():
        try:
            from src.api.lifecycle import sandbox_service

            timeout = app_config.runtime.startup_sweep_timeout
            if timeout > 0 and app_config.runtime.type == "kubernetes":
                await asyncio.to_thread(_wait_for_old_pods_drained, timeout)

            logger.info("Running deferred stale-sandbox sweep")
            await asyncio.to_thread(sandbox_service.startup_stale_sandbox_sweep)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Deferred stale-sandbox sweep failed")

    _shutdown_event.clear()
    sweep_task = asyncio.create_task(_deferred_sweep())

    yield

    _shutdown_event.set()
    sweep_task.cancel()
    with suppress(asyncio.CancelledError):
        await sweep_task
    await app.state.http_client.aclose()


# Initialize FastAPI application
app = FastAPI(
    title="OpenSandbox Lifecycle API",
    version="0.1.0",
    description="The Sandbox Lifecycle API coordinates how untrusted workloads are created, "
                "executed, paused, resumed, and finally disposed.",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 当前环境默认放开所有来源，生产部署需按配置收敛
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Attach global config for runtime access
app.state.config = app_config

# Add authentication middleware
app.add_middleware(AuthMiddleware, config=app_config)

# Include API routes at root and versioned prefix
app.include_router(router)
app.include_router(router, prefix="/v1")

DEFAULT_ERROR_CODE = "GENERAL::UNKNOWN_ERROR"
DEFAULT_ERROR_MESSAGE = "An unexpected error occurred."


def _normalize_error_detail(detail: Any) -> dict[str, str]:
    """
    Ensure HTTP errors always conform to {"code": "...", "message": "..."}.
    """
    if isinstance(detail, dict):
        code = detail.get("code") or DEFAULT_ERROR_CODE
        message = detail.get("message") or DEFAULT_ERROR_MESSAGE
        return {"code": code, "message": message}
    message = str(detail) if detail else DEFAULT_ERROR_MESSAGE
    return {"code": DEFAULT_ERROR_CODE, "message": message}


@app.exception_handler(HTTPException)
async def sandbox_http_exception_handler(request: Request, exc: HTTPException):
    """
    Flatten FastAPI HTTPException payload to the standard error schema.
    """
    content = _normalize_error_detail(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=exc.headers,
    )


@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns:
        dict: Health status
    """
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    # Run the application
    uvicorn.run(
        "src.main:app",
        host=app_config.server.host,
        port=app_config.server.port,
        reload=True,
        log_config=_log_config,
    )
