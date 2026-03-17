# Task Executor 使用指南

## 简介

`task-executor` 是一个轻量级组件，旨在 Kubernetes Pod 环境中运行和管理短期任务（进程或容器）。它充当本地代理，从 Kubernetes 控制器（例如 `BatchSandboxController`）接收任务规范，并在其运行的节点上执行这些任务。它暴露了一个简单的 HTTP API 用于任务创建、状态查询和管理。

## 运行 Task Executor

可以使用 `cmd/task-executor/main.go` 入口点启动 `task-executor`。它支持各种命令行标志和环境变量进行配置。

**基本启动：**

```bash
/path/to/cmd/task-executor/main --data-dir=/var/lib/sandbox/tasks --listen-addr=0.0.0.0:5758
```

**关键配置参数：**

| 标志 / 环境变量 | 描述 | 默认值 |
| :--- | :--- | :--- |
| `--data-dir` (DATA_DIR) | 用于持久化任务状态和日志的目录。 | `/var/lib/sandbox/tasks` |
| `--listen-addr` (LISTEN_ADDR) | HTTP API 服务器的地址和端口。 | `0.0.0.0:5758` |
| `--enable-sidecar-mode` (ENABLE_SIDECAR_MODE) | 如果为 `true`，则启用 sidecar 模式执行，任务将在指定主容器的 PID 命名空间内运行。需要 `nsenter` 和适当的权限。 | `false` |
| `--main-container-name` (MAIN_CONTAINER_NAME) | 当 `enable-sidecar-mode` 为 `true` 时，指定应使用其 PID 命名空间的主容器的名称。 | `main` |
| `--enable-container-mode` (ENABLE_CONTAINER_MODE) | 如果为 `true`，则启用使用 CRI 运行时的容器模式执行。（注意：当前实现可能只是占位符）。 | `false` |
| `--cri-socket` (CRI_SOCKET) | 当 `enable-container-mode` 为 `true` 时，CRI 套接字的路径（例如 `containerd.sock`）。 | `/var/run/containerd/containerd.sock` |
| `--reconcile-interval` | 内部任务管理器协调任务状态的间隔。 | `500ms` |

## HTTP API 端点

`task-executor` 暴露了一个 RESTful HTTP API。所有 API 调用都期望 JSON 请求体（如适用）并返回 JSON 响应。

### 1. `POST /tasks` - 创建新任务

创建并启动单个任务。

*   **方法：** `POST`
*   **路径：** `/tasks`
*   **请求体 (application/json)：** 代表所需任务的对象。

    ```json
    {
      "name": "my-first-task",
      "spec": {
        "process": {
          "command": ["sh", "-c"],
          "args": ["echo 'Hello from my task!' && sleep 5 && echo 'Task finished.'"]
        }
      }
    }
    ```

*   **响应体 (application/json)：** 创建的任务对象及其初始状态。

    ```json
    {
      "name": "my-first-task",
      "spec": {
        "process": {
          "command": ["sh", "-c"],
          "args": ["echo 'Hello from my task!' && sleep 5 && echo 'Task finished.'"]
        }
      },
      "status": {
        "state": {
          "waiting": {
            "reason": "Initialized"
          }
        }
      }
    }
    ```

**示例 (使用 `curl`)：**

```bash
curl -X POST -H "Content-Type: application/json" -d
'{
  "name": "my-first-task",
  "spec": {
    "process": {
      "command": ["sh", "-c"],
      "args": ["echo \"Hello from my task!\" && sleep 5 && echo \"Task finished.\""]
    }
  }
}' http://localhost:5758/tasks
```

### 2. `GET /tasks/{id}` - 获取任务状态

通过名称检索特定任务的当前状态。

*   **方法：** `GET`
*   **路径：** `/tasks/{taskName}`
*   **响应体 (application/json)：** 任务对象，包括其当前状态。

    ```json
    {
      "name": "my-first-task",
      "spec": {
        "process": {
          "command": ["sh", "-c"],
          "args": ["echo 'Hello from my task!' && sleep 5 && echo 'Task finished.'"]
        }
      },
      "status": {
        "state": {
          "running": {
            "startedAt": "2025-12-17T10:00:00Z"
          }
        }
      }
    }
    ```

**示例 (使用 `curl`)：**

```bash
curl http://localhost:5758/tasks/my-first-task
```

### 3. `DELETE /tasks/{id}` - 删除任务

标记要删除的任务。`task-executor` 将尝试优雅地停止任务，然后删除其状态。

*   **方法：** `DELETE`
*   **路径：** `/tasks/{taskName}`
*   **响应：** 成功标记删除时返回 `204 No Content`。

**示例 (使用 `curl`)：**

```bash
curl -X DELETE http://localhost:5758/tasks/my-first-task
```

### 4. `POST /setTasks` - 同步任务

此端点通常由控制器用于同步所需的任务集。不在所需列表中的任务将被标记为删除；新任务将被创建。

*   **方法：** `POST`
*   **路径：** `/setTasks`
*   **请求体 (application/json)：** 代表所需状态的任务对象数组。

    ```json
    [
      {
        "name": "task-alpha",
        "spec": {
          "process": {
            "command": ["sleep", "10"]
          }
        }
      },
      {
        "name": "task-beta",
        "spec": {
          "process": {
            "command": ["ls", "-l", "/tmp"]
          }
        }
      }
    ]
    ```

*   **响应体 (application/json)：** 同步后执行器管理的当前任务列表。

    ```json
    [
      {
        "name": "task-alpha",
        "spec": {
          "process": {
            "command": ["sleep", "10"]
          }
        },
        "status": {
          "state": {
            "waiting": {
              "reason": "Initialized"
            }
          }
        }
      },
      {
        "name": "task-beta",
        "spec": {
          "process": {
            "command": ["ls", "-l", "/tmp"]
          }
        },
        "status": {
          "state": {
            "waiting": {
              "reason": "Initialized"
            }
          }
        }
      }
    ]
    ```

**示例 (使用 `curl`)：**

```bash
curl -X POST -H "Content-Type: application/json" -d \
'[
  {
    "name": "task-alpha",
    "spec": { "process": { "command": ["sleep", "10"] } }
  },
  {
    "name": "task-beta",
    "spec": { "process": { "command": ["ls", "-l", "/tmp"] } }
  }
]' http://localhost:5758/setTasks
```

### 5. `GET /getTasks` - 列出所有任务

检索 `task-executor` 当前管理的所有任务的列表。

*   **方法：** `GET`
*   **路径：** `/getTasks`
*   **响应体 (application/json)：** 任务对象数组。

    ```json
    [
      {
        "name": "task-alpha",
        "spec": {
          "process": {
            "command": ["sleep", "10"]
          }
        },
        "status": {
          "state": {
            "running": {
              "startedAt": "2025-12-17T10:05:00Z"
            }
          }
        }
      },
      {
        "name": "task-beta",
        "spec": {
          "process": {
            "command": ["ls", "-l", "/tmp"]
          }
        },
        "status": {
          "state": {
            "terminated": {
              "exitCode": 0,
              "reason": "Succeeded",
              "startedAt": "2025-12-17T10:06:00Z",
              "finishedAt": "2025-12-17T10:06:01Z"
            }
          }
        }
      }
    ]
    ```

**示例 (使用 `curl`)：**

```bash
curl http://localhost:5758/getTasks
```

### 6. `GET /health` - 健康检查

返回 `task-executor` 的健康状态。

*   **方法：** `GET`
*   **路径：** `/health`
*   **响应体 (application/json)：**

    ```json
    {
      "status": "healthy"
    }
    ```

**示例 (使用 `curl`)：**

```bash
curl http://localhost:5758/health
```

## 任务规范 (`TaskSpec`) 结构

任务对象中的 `spec` 字段 (`api/v1alpha1.TaskSpec`) 定义了应如何执行任务。它目前支持 `process` 和 `container` 执行模式。

### 进程任务示例

此模式直接作为进程执行命令。

```json
{
  "name": "my-process-task",
  "spec": {
    "process": {
      "command": ["python3", "my_script.py"],
      "args": ["--config", "/etc/app/config.yaml"],
      "env": [
        { "name": "DEBUG_MODE", "value": "true" }
      ],
      "workingDir": "/app"
    }
  }
}
```

### 容器任务示例（占位符/未来特性）

此模式旨在执行由 CRI 运行时管理的容器中的任务。请注意，根据 `internal/task-executor/runtime/container.go`，此模式可能仍是一个占位符。

```json
{
  "name": "my-container-task",
  "spec": {
    "container": {
      "image": "ubuntu:latest",
      "command": ["/bin/bash", "-c"],
      "args": ["apt update && apt install -y curl"],
      "env": [
        { "name": "http_proxy", "value": "http://myproxy.com:5758" }
      ],
      "volumeMounts": [
        {
          "name": "data-volume",
          "mountPath": "/data"
        }
      ]
    }
  }
}
```

## 任务状态 (`TaskStatus`) 结构

任务对象中的 `status` 字段 (`internal/task-executor/types/Status` 映射到 `api/v1alpha1.TaskStatus` 用于外部 API) 提供了有关任务当前执行状态的详细信息。

```json
{
  "name": "my-task",
  "spec": { ... },
  "status": {
    "state": {
      "waiting": {
        "reason": "Initialized"
      }
    },
    // 或者
    "state": {
      "running": {
        "startedAt": "2025-12-17T10:00:00Z"
      }
    },
    // 或者
    "state": {
      "terminated": {
        "exitCode": 0,
        "reason": "Succeeded",
        "message": "Task completed successfully",
        "startedAt": "2025-12-17T10:00:00Z",
        "finishedAt": "2025-12-17T10:00:05Z"
      }
    }
  }
}
```

**状态类型：**

*   `waiting`：任务正在等待执行。
*   `running`：任务当前正在执行。
*   `terminated`：任务已完成（成功或失败）。

## 示例场景：运行 Sidecar 任务

如果 `task-executor` 配置了 `--enable-sidecar-mode=true` 和 `--main-container-name=my-main-app`，它可以在 `my-main-app` 的 PID 命名空间内执行任务。

```bash
# 假设 task-executor 在 sidecar 模式下运行在一个包含 'my-main-app' 的 pod 上
# 此任务将从主容器的命名空间内执行 'ls /proc/self/ns'
curl -X POST -H "Content-Type: application/json" -d
'{
  "name": "sidecar-namespace-check",
  "spec": {
    "process": {
      "command": ["ls", "/proc/self/ns"]
    }
  }
}' http://localhost:5758/tasks
```
