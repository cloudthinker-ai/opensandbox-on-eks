# Getting Started

Deploy OpenSandbox and create your first sandbox, end-to-end.

## Choose your platform

| Platform | Method | Status | Guide |
|----------|--------|--------|-------|
| Amazon EKS | **Terraform (recommended)** | **Available** | [docs/eks/terraform.md](eks/terraform.md) |
| Amazon EKS | Manual (`eksctl`) — new cluster | **Available** | [docs/eks/new-cluster.md](eks/new-cluster.md) |
| Amazon EKS | Manual (`eksctl`) — existing cluster | **Available** | [docs/eks/existing-cluster.md](eks/existing-cluster.md) |
| Azure AKS | — | Coming soon | — |
| Google GKE | — | Coming soon | — |
| Local (Docker) | — | Coming soon | — |

**Recommended:** Use the [Terraform guide](eks/terraform.md) — it automates cluster, VPC, IAM, addons, and ECR setup in a single `terraform apply`, then walks you through the remaining steps (StorageClass, image build, Helm install).

Follow your chosen guide end-to-end, then return here to create your first sandbox.

---

## Create your first sandbox

Once your OpenSandbox deployment is running, you can create sandboxes via the REST API.

### Understanding sandbox images

OpenSandbox has two types of images:

- **Infrastructure images** (server, controller, execd, egress-sidecar) — these run the platform itself. You built and pushed them during the EKS guide.
- **Sandbox images** — these are what your sandboxes actually run. You specify the image in each API call. **Any Docker image works** — `python:3.11-slim`, `ubuntu:24.04`, `node:20`, or your own custom image. OpenSandbox automatically injects the execd daemon via init container.

### Port-forward the server

If you haven't set up a LoadBalancer yet:

```bash
kubectl port-forward svc/opensandbox-server -n opensandbox 8080:8080 &
```

### Create a sandbox

```bash
curl -X POST "http://localhost:8080/v1/sandboxes" \
  -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "image": { "uri": "python:3.11-slim" },
    "entrypoint": ["python", "-m", "http.server", "8000"],
    "timeout": 3600,
    "resourceLimits": { "cpu": "500m", "memory": "512Mi" }
  }' | jq .
```

Response:

```json
{
  "id": "a1b2c3d4-5678-90ab-cdef-1234567890ab",
  "status": {
    "state": "Pending",
    "reason": "CONTAINER_STARTING",
    "message": "Sandbox container is starting."
  },
  "expiresAt": "2024-01-15T11:30:00Z",
  "createdAt": "2024-01-15T10:30:00Z",
  "entrypoint": ["python", "-m", "http.server", "8000"]
}
```

### Check sandbox status

```bash
curl -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  "http://localhost:8080/v1/sandboxes/<sandbox-id>" | jq .status
```

Wait until `state` is `Running`.

### Verify the sandbox pod

```bash
kubectl get pods -n opensandbox -l opensandbox.io/component=sandbox
```

You should see a pod in `Running` state.

### Delete the sandbox

```bash
curl -X DELETE -H "OPEN-SANDBOX-API-KEY: <your-api-key>" \
  "http://localhost:8080/v1/sandboxes/<sandbox-id>"
```

### Using a private registry image

If your sandbox image is in a private registry (e.g., ECR), pass `auth` in the image spec:

```json
{
  "image": {
    "uri": "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/my-image:latest",
    "auth": {
      "username": "AWS",
      "password": "<ecr-token>"
    }
  }
}
```

---

## Use the Python SDK

Install the SDK:

```bash
pip install opensandbox
```

Quick example:

```python
import asyncio
from datetime import timedelta
from opensandbox import Sandbox

async def main():
    sandbox = await Sandbox.create(
        "python:3.11-slim",
        entrypoint=["python", "-c", "import time; time.sleep(300)"],
        timeout=timedelta(minutes=10),
    )

    async with sandbox:
        # Execute a command
        result = await sandbox.commands.run("echo 'Hello from sandbox!'")
        print(result.logs.stdout[0].text)

        # Write and read a file
        from opensandbox.models import WriteEntry
        await sandbox.files.write_files([
            WriteEntry(path="/tmp/hello.txt", data="Hello World", mode=644)
        ])
        content = await sandbox.files.read_file("/tmp/hello.txt")
        print(f"File content: {content}")

    await sandbox.kill()

asyncio.run(main())
```

---

## Optional: Build the code-interpreter image

For a richer sandbox experience with Jupyter and multi-language support (Python, Node.js, Go, Java), build the code-interpreter image:

```bash
# Build the base image first (this takes a while — it installs multiple language runtimes)
docker build -t opensandbox/code-interpreter-base:latest \
  -f sandboxes/code-interpreter/Dockerfile_base sandboxes/code-interpreter/

# Build the code-interpreter image
docker build -t opensandbox/code-interpreter:latest sandboxes/code-interpreter/

# Push to ECR (replace placeholders)
docker tag opensandbox/code-interpreter:latest \
  <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opensandbox/code-interpreter:latest
docker push <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opensandbox/code-interpreter:latest
```

Then use it in API calls:

```json
{
  "image": { "uri": "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/opensandbox/code-interpreter:latest" },
  "entrypoint": ["/opt/opensandbox/code-interpreter.sh"],
  "timeout": 3600,
  "resourceLimits": { "cpu": "1", "memory": "2Gi" }
}
```

---

## Build custom sandbox images

Want to create your own purpose-built sandbox image? See the [Custom Images Guide](custom-images.md) for requirements, examples, and best practices.

---

## Next steps

- [Architecture](architecture/architecture.md) — system design, OverlayFS persistence, lifecycle state machine
- **API docs** — Swagger UI at `/docs` and ReDoc at `/redoc` when the server is running
- [SDKs](../sdks/) — Python, Java/Kotlin, TypeScript/JavaScript, C#/.NET
- [Custom Images](custom-images.md) — build your own sandbox images
