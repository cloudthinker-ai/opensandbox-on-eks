# Accessing Services in a Sandbox

Start any service on a port inside a sandbox and access it via a public URL — no port pre-registration needed.

## Overview

When a service listens on a port inside a sandbox, OpenSandbox provides a public endpoint URL to reach it. With wildcard DNS gateway mode, each sandbox port gets its own subdomain:

```
{sandbox-id}-{port}.sandbox.example.com
```

This requires **gateway mode** to be configured in your Helm values:

```yaml
server:
  config:
    ingress:
      mode: "gateway"
      gateway:
        address: "*.sandbox.example.com"   # Your wildcard domain
        route:
          mode: "wildcard"
```

You also need a wildcard DNS record (`*.sandbox.example.com`) pointing to your ALB/NLB, so that the generated URLs are publicly reachable.

The workflow is:

1. Start a service on a port inside the sandbox
2. Call `get_endpoint(port)` to get the public URL
3. Access the service from your browser or app — no API key needed

---

## Quick Start

### Step 1: Start a service inside the sandbox

You can start a service in two ways:

**Option A — Via entrypoint** (service starts when sandbox starts):

```python
sandbox = await Sandbox.create(
    "python:3.11-slim",
    connection_config=config,
    entrypoint=["python", "-m", "http.server", "8080"],
)
```

**Option B — Via `commands.run()`** (start a service in a running sandbox):

```python
await sandbox.commands.run("python -m http.server 8080 &")
```

> Use `&` at the end to run the service in the background so the command returns immediately.

### Step 2: Get the endpoint URL

**Python SDK:**

```python
endpoint = await sandbox.get_endpoint(8080)
url = f"http://{endpoint.endpoint}"
# url = "http://abc123-8080.sandbox.example.com"
```

**JavaScript/TypeScript SDK:**

```typescript
const url = await sandbox.getEndpointUrl(8080);
// "http://abc123-8080.sandbox.example.com"
```

**REST API (curl):**

```bash
curl -s -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  "http://localhost:8080/v1/sandboxes/<sandbox-id>/endpoints/8080" | jq .
```

Response:

```json
{
  "endpoint": "abc123-8080.sandbox.example.com"
}
```

### Step 3: Access the service

```bash
curl http://abc123-8080.sandbox.example.com/
```

Or open the URL in your browser.

---

## Full Example: Python HTTP Server

```python
import asyncio
import httpx
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig

async def main():
    config = ConnectionConfig(
        domain="localhost:8080",
        api_key="your-api-key",
    )

    sandbox = await Sandbox.create(
        "python:3.11-slim",
        connection_config=config,
    )

    async with sandbox:
        # Start an HTTP server on port 8080
        await sandbox.commands.run("python -m http.server 8080 &")

        # Get the public URL
        endpoint = await sandbox.get_endpoint(8080)
        url = f"http://{endpoint.endpoint}"

        # Access the service
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{url}/")
            print(response.text)

        await sandbox.kill()

asyncio.run(main())
```

## Full Example: Node.js Express App

```python
import asyncio
import httpx
from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
from opensandbox.models.filesystem import WriteEntry

async def main():
    config = ConnectionConfig(
        domain="localhost:8080",
        api_key="your-api-key",
    )

    sandbox = await Sandbox.create(
        "node:20",
        connection_config=config,
    )

    async with sandbox:
        # Write an Express app
        await sandbox.files.write_files([
            WriteEntry(
                path="/app/server.js",
                data="""
const http = require('http');
const server = http.createServer((req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ message: 'Hello from sandbox!' }));
});
server.listen(3000, () => console.log('Listening on port 3000'));
""",
                mode=644,
            )
        ])

        # Start the server
        await sandbox.commands.run("node /app/server.js &")

        # Get the public URL
        endpoint = await sandbox.get_endpoint(3000)
        url = f"http://{endpoint.endpoint}"

        # Access the service
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            print(response.json())  # {"message": "Hello from sandbox!"}

        await sandbox.kill()

asyncio.run(main())
```

---

## Multiple Ports

A sandbox can expose multiple ports simultaneously. Call `get_endpoint()` for each port:

```python
# Start two services
await sandbox.commands.run("python -m http.server 8080 &")
await sandbox.commands.run("python -m http.server 9090 &")

# Get separate URLs for each
endpoint_8080 = await sandbox.get_endpoint(8080)
endpoint_9090 = await sandbox.get_endpoint(9090)
# "abc123-8080.sandbox.example.com"
# "abc123-9090.sandbox.example.com"
```

---

## Endpoint Reference

### Python SDK

```python
endpoint = await sandbox.get_endpoint(port)
# endpoint.endpoint  -> str, e.g. "abc123-8080.sandbox.example.com"
# endpoint.headers   -> Optional[dict], None for wildcard DNS mode
```

### JavaScript/TypeScript SDK

```typescript
// Raw endpoint (no scheme)
const ep = await sandbox.getEndpoint(port);
// ep.endpoint  -> string

// Full URL with scheme (convenience)
const url = await sandbox.getEndpointUrl(port);
// "http://abc123-8080.sandbox.example.com"
```

### REST API

```
GET /v1/sandboxes/{sandbox_id}/endpoints/{port}

Response:
{
  "endpoint": "abc123-8080.sandbox.example.com"
}
```

---

## Caveats

- **Port range:** 1-65535
- **No scheme in endpoint:** The `endpoint` string does not include `http://` or `https://`. Prepend the scheme yourself, or use `getEndpointUrl()` in the JavaScript SDK which adds it automatically.
- **Service must be listening:** The endpoint URL is generated from sandbox metadata, but the connection will fail if no service is actually bound to that port.
- **Sandbox lifecycle:** The endpoint becomes unreachable when the sandbox is paused, expired, or killed.
