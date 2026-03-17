# OpenSandbox JavaScript E2E Tests

This folder contains strict E2E tests for the JavaScript/TypeScript SDKs, aligned with `OpenSandbox/tests/python` and `OpenSandbox/tests/java`.

## Prerequisites

- Node.js (via nvm): **>= 20**
- pnpm (via corepack or global install)
- OpenSandbox server running

## Environment variables

These tests follow the same naming as Python tests:

- `OPENSANDBOX_TEST_DOMAIN` (default: `localhost:8080`)
- `OPENSANDBOX_TEST_PROTOCOL` (default: `http`)
- `OPENSANDBOX_TEST_API_KEY` (default: `e2e-test`)
- `OPENSANDBOX_SANDBOX_DEFAULT_IMAGE` (default: code-interpreter image)

## Run

```bash
cd OpenSandbox/tests/javascript

# Node >= 20 is required (SDK engines: node >= 20)
source ~/.nvm/nvm.sh
nvm use 22

# Ensure pnpm is available (repo pins pnpm@9.x)
corepack enable
corepack prepare pnpm@9.15.0 --activate

# Install test dependencies (vitest, typescript)
pnpm install

# Run tests (also builds SDKs)
pnpm test
```
