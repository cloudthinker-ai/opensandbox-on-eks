# Python Sandbox SDK

The OpenSandbox Python SDK provides a high-level interface for creating, managing, and interacting with secure sandbox environments. It supports both **async** and **sync** APIs, including shell command execution, file management, streaming output, and resource monitoring.

- **Source**: [`sdks/sandbox/python/`](../../sdks/sandbox/python/)
- **Python**: >= 3.10

> **Note**: The `opensandbox` package on PyPI is published by the upstream [Alibaba OpenSandbox](https://github.com/alibaba/OpenSandbox) project and is deprecated. For this fork, you must **build and install from source**.

## Installation

### Build from source

Requires Python >= 3.10 and [uv](https://docs.astral.sh/uv/).

```bash
# Clone the repo (if you haven't already)
git clone https://github.com/cloudthinker-ai/OpenSandbox-OSS.git
cd OpenSandbox-OSS/sdks/sandbox/python

# Install dependencies and generate API clients
make dev-install

# Build the wheel
make build

# Install the built package into your project
pip install dist/opensandbox-*.whl
```

Or install directly from the source tree in editable mode:

```bash
cd sdks/sandbox/python
uv sync
pip install -e .
```

## Quick Start

### Async API

```python
import asyncio
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxException

async def main():
    config = ConnectionConfig(
        domain="api.opensandbox.io",
        api_key="your-api-key",
    )

    try:
        sandbox = await Sandbox.create("ubuntu", connection_config=config)
        async with sandbox:
            execution = await sandbox.commands.run("echo 'Hello Sandbox!'")
            print(execution.logs.stdout[0].text)

            await sandbox.kill()
    except SandboxException as e:
        print(f"Sandbox Error: [{e.error.code}] {e.error.message}")

asyncio.run(main())
```

### Sync API

Use `SandboxSync` / `SandboxManagerSync` with `ConnectionConfigSync` for synchronous usage:

```python
from datetime import timedelta
import httpx
from opensandbox import SandboxSync
from opensandbox.config import ConnectionConfigSync

config = ConnectionConfigSync(
    domain="api.opensandbox.io",
    api_key="your-api-key",
    request_timeout=timedelta(seconds=30),
    transport=httpx.HTTPTransport(limits=httpx.Limits(max_connections=20)),
)

sandbox = SandboxSync.create("ubuntu", connection_config=config)
with sandbox:
    execution = sandbox.commands.run("echo 'Hello Sandbox!'")
    print(execution.logs.stdout[0].text)
    sandbox.kill()
```

## Usage Examples

### Lifecycle Management

Manage the sandbox lifecycle — renew, pause, resume, and inspect.

```python
from datetime import timedelta

# Renew the sandbox (resets expiration to current time + duration)
await sandbox.renew(timedelta(minutes=30))

# Pause execution (suspends all processes)
await sandbox.pause()

# Resume a paused sandbox
sandbox = await Sandbox.resume(
    sandbox_id=sandbox.id,
    connection_config=config,
)

# Get current status
info = await sandbox.get_info()
print(f"State: {info.status.state}")
```

### Custom Health Check

Override the default ping-based health check with your own logic:

```python
async def custom_health_check(sbx: Sandbox) -> bool:
    try:
        endpoint = await sbx.get_endpoint(80)
        # Perform your check (HTTP request, socket connect, etc.)
        return True
    except Exception:
        return False

sandbox = await Sandbox.create(
    "nginx:latest",
    connection_config=config,
    health_check=custom_health_check,
)
```

### Command Execution & Streaming

Execute commands with real-time streaming output using `ExecutionHandlers`:

```python
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts

async def handle_stdout(msg):
    print(f"STDOUT: {msg.text}")

async def handle_stderr(msg):
    print(f"STDERR: {msg.text}")

async def handle_complete(complete):
    print(f"Finished in {complete.execution_time_in_millis}ms")

handlers = ExecutionHandlers(
    on_stdout=handle_stdout,
    on_stderr=handle_stderr,
    on_execution_complete=handle_complete,
)

result = await sandbox.commands.run(
    "for i in {1..5}; do echo \"Count $i\"; sleep 0.5; done",
    handlers=handlers,
)
```

You can also check the status of background commands:

```python
status = await sandbox.commands.get_command_status(execution_id)
logs = await sandbox.commands.get_background_command_logs(execution_id, cursor=0)
```

### File Operations

Read, write, search, and manage files inside the sandbox.

```python
from opensandbox.models.filesystem import WriteEntry, SearchEntry

# Write multiple files
await sandbox.files.write_files([
    WriteEntry(path="/tmp/hello.txt", data="Hello World", mode=644),
])

# Write a single file (convenience method)
await sandbox.files.write_file("/tmp/config.json", '{"key": "value"}', mode=644)

# Read file as text
content = await sandbox.files.read_file("/tmp/hello.txt")

# Read file as bytes
data = await sandbox.files.read_bytes("/tmp/hello.txt")

# Stream large files
async for chunk in await sandbox.files.read_bytes_stream("/tmp/large.bin"):
    process(chunk)

# Search for files
files = await sandbox.files.search(
    SearchEntry(path="/tmp", pattern="*.txt")
)
for f in files:
    print(f"Found: {f.path}")

# Get file metadata
info = await sandbox.files.get_file_info(["/tmp/hello.txt"])

# Move files
from opensandbox.models.filesystem import MoveEntry
await sandbox.files.move_files([
    MoveEntry(source="/tmp/hello.txt", destination="/tmp/moved.txt"),
])

# Delete files and directories
await sandbox.files.delete_files(["/tmp/moved.txt"])
await sandbox.files.delete_directories(["/tmp/mydir"])

# Create directories
await sandbox.files.create_directories([
    WriteEntry(path="/tmp/newdir", mode=755),
])

# Set permissions
from opensandbox.models.filesystem import SetPermissionEntry
await sandbox.files.set_permissions([
    SetPermissionEntry(path="/tmp/hello.txt", mode=644),
])

# Replace file contents
from opensandbox.models.filesystem import ContentReplaceEntry
await sandbox.files.replace_contents([
    ContentReplaceEntry(path="/tmp/hello.txt", pattern="Hello", replacement="Hi"),
])
```

### Sandbox Management (Admin)

Use `SandboxManager` for administrative tasks across multiple sandboxes:

```python
from opensandbox.manager import SandboxManager
from opensandbox.models.sandboxes import SandboxFilter

async with await SandboxManager.create(connection_config=config) as manager:
    # List running sandboxes
    result = await manager.list_sandbox_infos(
        SandboxFilter(states=["RUNNING"], page_size=10)
    )
    for info in result.sandbox_infos:
        print(f"Sandbox: {info.id}")

    # Admin operations on a specific sandbox
    await manager.renew_sandbox("sandbox-id", timedelta(minutes=30))
    await manager.pause_sandbox("sandbox-id")
    await manager.resume_sandbox("sandbox-id")
    await manager.kill_sandbox("sandbox-id")
```

## Configuration

### ConnectionConfig

| Parameter          | Description                                   | Default                  | Environment Variable     |
| ------------------ | --------------------------------------------- | ------------------------ | ------------------------ |
| `api_key`          | API key for authentication                    | Required                 | `OPEN_SANDBOX_API_KEY`   |
| `domain`           | Endpoint domain of the sandbox service        | `localhost:8080`         | `OPEN_SANDBOX_DOMAIN`    |
| `protocol`         | HTTP protocol (`http` or `https`)             | `http`                   | —                        |
| `request_timeout`  | Timeout for API requests                      | 30 seconds               | —                        |
| `debug`            | Enable debug logging for HTTP requests        | `False`                  | —                        |
| `headers`          | Custom HTTP headers                           | Empty                    | —                        |
| `transport`        | Shared httpx transport (pool/proxy/retry)     | SDK-created per instance | —                        |
| `use_server_proxy` | Route execd/endpoint requests through server  | `False`                  | —                        |

```python
from datetime import timedelta
import httpx

# Basic
config = ConnectionConfig(
    api_key="your-key",
    domain="api.opensandbox.io",
    request_timeout=timedelta(seconds=60),
)

# Advanced: shared transport for many Sandbox instances
config = ConnectionConfig(
    api_key="your-key",
    domain="api.opensandbox.io",
    headers={"X-Custom-Header": "value"},
    transport=httpx.AsyncHTTPTransport(
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=50,
            keepalive_expiry=30.0,
        )
    ),
)
# If you provide a custom transport, you are responsible for closing it:
# await config.transport.aclose()
```

### Sandbox.create() Parameters

| Parameter                      | Description                              | Default                         |
| ------------------------------ | ---------------------------------------- | ------------------------------- |
| `image`                        | Docker image specification               | Required                        |
| `timeout`                      | Automatic termination timeout            | 10 minutes                      |
| `ready_timeout`                | Max time to wait for sandbox readiness   | 30 seconds                      |
| `entrypoint`                   | Container entrypoint command             | `["tail", "-f", "/dev/null"]`   |
| `resource`                     | CPU and memory limits                    | `{"cpu": "1", "memory": "2Gi"}` |
| `env`                          | Environment variables                    | Empty                           |
| `metadata`                     | Custom metadata tags                     | Empty                           |
| `network_policy`               | Outbound network policy (egress)         | —                               |
| `volumes`                      | Volume mounts for the sandbox            | —                               |
| `extensions`                   | Extension configuration                  | Empty                           |
| `health_check`                 | Custom health check function             | Default ping                    |
| `health_check_polling_interval`| Interval between health check polls      | 200 milliseconds                |
| `skip_health_check`            | Skip the health check on creation        | `False`                         |
| `connection_config`            | Connection configuration                 | —                               |

```python
from datetime import timedelta
from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

sandbox = await Sandbox.create(
    "python:3.11",
    connection_config=config,
    timeout=timedelta(minutes=30),
    ready_timeout=timedelta(seconds=60),
    resource={"cpu": "2", "memory": "4Gi"},
    env={"PYTHONPATH": "/app"},
    metadata={"project": "demo"},
    network_policy=NetworkPolicy(
        defaultAction="deny",
        egress=[NetworkRule(action="allow", target="pypi.org")],
    ),
    skip_health_check=False,
)
```

## Error Handling

All SDK exceptions inherit from `SandboxException`:

| Exception                      | Description                                                    |
| ------------------------------ | -------------------------------------------------------------- |
| `SandboxException`             | Base exception for all sandbox errors                          |
| `SandboxApiException`          | API returned an error response (includes `status_code`)        |
| `SandboxInternalException`     | Unexpected internal SDK error                                  |
| `SandboxUnhealthyException`    | Sandbox determined to be unhealthy                             |
| `SandboxReadyTimeoutException` | Timed out waiting for sandbox readiness                        |
| `InvalidArgumentException`     | Invalid argument passed to an SDK method                       |

```python
from opensandbox.exceptions import (
    SandboxException,
    SandboxApiException,
    SandboxReadyTimeoutException,
)

try:
    sandbox = await Sandbox.create("ubuntu", connection_config=config)
except SandboxReadyTimeoutException:
    print("Sandbox took too long to start")
except SandboxApiException as e:
    print(f"API error (HTTP {e.status_code}): {e.error.code} - {e.error.message}")
except SandboxException as e:
    print(f"Sandbox error: {e}")
```

## Building from Source

All commands below assume you are in `sdks/sandbox/python/`.

### Prerequisites

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/) (package manager)

### Install Dependencies

```bash
# Production dependencies only
make install        # runs: uv sync

# With dev dependencies (ruff, pyright, pytest, openapi-python-client)
make dev-install    # runs: uv sync --all-extras (also generates API clients)
```

### Generate API Clients

The SDK uses typed API clients generated from OpenAPI specs. Regenerate them after API spec changes:

```bash
make generate-api   # runs: uv run python scripts/generate_api.py
```

To clean and regenerate:

```bash
make clean-api      # removes src/opensandbox/api/execd/ and src/opensandbox/api/lifecycle/
make generate-api
```

### Build the Package

```bash
make build          # generates API clients, then runs: uv build
```

This outputs a wheel and sdist to `dist/`. The version is derived from git tags matching `python/sandbox/v*` (via `hatch-vcs`), with a fallback of `0.1.0`.

Install the built wheel into another project:

```bash
pip install dist/opensandbox-*.whl
```

### Run Tests

```bash
make test           # runs: uv run pytest

# With coverage report
make test-cov       # runs: uv run pytest --cov=src/opensandbox --cov-report=html
```

### Lint & Type Check

```bash
# Run all checks (format + lint + type-check)
make check

# Individual targets
make format         # Black + isort
make lint           # Ruff
make type-check     # Pyright
```

### Full CI Pipeline Locally

```bash
make ci             # generate-api → dev-install → check → test
```

### Clean Build Artifacts

```bash
make clean          # removes dist/, build/, .pytest_cache/, __pycache__, etc.
```

### Publish

```bash
uv publish          # requires PyPI authentication
```
