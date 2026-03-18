<div align="center">
    <img src="docs/assets/logo.png" alt="OpenSandbox logo" width="150" />

  <h1>OpenSandbox</h1>

  <p>Sandbox platform for AI coding agents — isolated, persistent development environments with pre-installed tools on Kubernetes.</p>

  <p><em>This is a fork of <a href="https://github.com/alibaba/OpenSandbox">alibaba/OpenSandbox</a>.</em></p>

  <hr />
</div>

## Features

- **Sandbox isolation** — each agent gets its own Kubernetes pod with a full Linux userspace
- **OverlayFS persistence** — all filesystem changes (packages, configs, user files) survive pause/resume via PVC-backed OverlayFS
- **Code execution** — execd daemon + Jupyter server for interactive code interpretation
- **Pause / resume** — instant pause with PVC retention, VolumeSnapshot for long-term archival
- **Pre-warmed pools** — sub-second sandbox provisioning via warm standby pods
- **Security hardened** — no sudo, SUID/SGID stripped, capability dropping after bootstrap
- **Multi-arch builds** — amd64 and arm64 support

## Quick Start

Requirements:

- A running OpenSandbox server
- Python 3.10+

### Install the SDK and Create a Sandbox

```bash
uv pip install opensandbox-code-interpreter
```

```python
import asyncio
from datetime import timedelta

from code_interpreter import CodeInterpreter, SupportedLanguage
from opensandbox import Sandbox
from opensandbox.models import WriteEntry

async def main() -> None:
    sandbox = await Sandbox.create(
        "opensandbox:latest",
        entrypoint=["/opt/opensandbox/code-interpreter.sh"],
        env={"PYTHON_VERSION": "3.12"},
        timeout=timedelta(minutes=10),
    )

    async with sandbox:

        # Execute a shell command
        execution = await sandbox.commands.run("echo 'Hello OpenSandbox!'")
        print(execution.logs.stdout[0].text)

        # Write a file
        await sandbox.files.write_files([
            WriteEntry(path="/tmp/hello.txt", data="Hello World", mode=644)
        ])

        # Read a file
        content = await sandbox.files.read_file("/tmp/hello.txt")
        print(f"Content: {content}")  # Content: Hello World

        # Create a code interpreter
        interpreter = await CodeInterpreter.create(sandbox)

        # Execute Python code
        result = await interpreter.codes.run(
              """
                  import sys
                  print(sys.version)
                  result = 2 + 2
                  result
              """,
              language=SupportedLanguage.PYTHON,
        )

        print(result.result[0].text)       # 4
        print(result.logs.stdout[0].text)  # 3.12.x

    await sandbox.kill()

if __name__ == "__main__":
    asyncio.run(main())
```

## Project Structure

| Directory                                             | Description                                                               |
| ----------------------------------------------------- | ------------------------------------------------------------------------- |
| [`sdks/`](sdks/)                                      | Multi-language SDKs (Python, Java/Kotlin, TypeScript/JavaScript, C#/.NET) |
| [`specs/`](specs/README.md)                           | OpenAPI specs and lifecycle specifications                                |
| [`server/`](server/README.md)                         | Python FastAPI sandbox lifecycle server                                   |
| [`kubernetes/`](kubernetes/README.md)                 | Kubernetes deployment and examples                                        |
| [`components/execd/`](components/execd/README.md)     | Sandbox execution daemon (commands and file operations)                   |
| [`components/ingress/`](components/ingress/README.md) | Sandbox traffic ingress proxy                                             |
| [`components/egress/`](components/egress/README.md)   | Sandbox network egress control                                            |
| [`sandboxes/`](sandboxes/)                            | Runtime sandbox implementations                                           |
| [`charts/`](charts/)                                  | Helm charts for deployment                                                |
| [`docs/`](docs/)                                      | Architecture and design documentation                                     |

## Documentation

For a step-by-step walkthrough — deploy and create your first sandbox — see the **[Getting Started](docs/getting-started.md)** guide. To build your own sandbox images, see **[Custom Images](docs/custom-images.md)**.

For the full architecture — system components, OverlayFS persistence, code execution flow, lifecycle state machine, storage, networking, pool system, and configuration — see [docs/architecture.md](docs/architecture/architecture.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Acknowledgments

This project is built upon [OpenSandbox](https://github.com/alibaba/OpenSandbox) by Alibaba, a general-purpose sandbox platform for AI applications. We are grateful to the Alibaba team and all contributors for open-sourcing the original project under the Apache 2.0 License. Our modifications and extensions are contributed back upstream where applicable.

## License

This project is open source under the [Apache 2.0 License](LICENSE).
