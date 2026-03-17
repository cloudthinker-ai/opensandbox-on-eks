# Controller 示例

这个示例演示了如何使用生成的 clientset、informer 和 lister 来操作 BatchSandbox 和 Pool 自定义资源。

## 功能介绍

### 1. Clientset (客户端集)
用于直接与 Kubernetes API Server 交互,执行 CRUD 操作:
- **Create**: 创建新的资源
- **Get**: 获取特定资源
- **List**: 列出所有资源
- **Update**: 更新现有资源
- **Delete**: 删除资源

### 2. Informer (通知器)
用于监听资源变化并维护本地缓存:
- 自动监听 API Server 的资源变化
- 触发事件处理器 (Add/Update/Delete)
- 维护资源的本地缓存,减少 API Server 压力

### 3. Lister (列表器)
用于从 Informer 的本地缓存中读取资源:
- 高性能的本地缓存读取
- 避免频繁访问 API Server
- 支持按命名空间和标签过滤

## 运行示例

### 前提条件
1. 已安装 CRD 定义到 Kubernetes 集群
2. 有访问集群的 kubeconfig 文件

### 安装 CRD
```bash
# 从项目根目录运行
kubectl apply -f config/crd/bases/
```

### 运行示例程序
```bash
# 使用默认 kubeconfig (~/.kube/config)
go run examples/controller/main.go

# 或指定 kubeconfig 路径
go run examples/controller/main.go -kubeconfig=/path/to/kubeconfig
```

## 示例输出

程序将执行以下操作:

1. **创建 Pool 资源**
   ```
   Successfully created Pool: example-pool
   ```

2. **获取 Pool 资源**
   ```
   Successfully retrieved Pool: example-pool, PoolMin: 2, PoolMax: 10
   ```

3. **列出所有 Pool 资源**
   ```
   Found 1 Pool(s):
     - example-pool (PoolMin: 2, PoolMax: 10)
   ```

4. **更新 Pool 资源**
   ```
   Successfully updated Pool: example-pool, new PoolMax: 20
   ```

5. **创建 BatchSandbox 资源**
   ```
   Successfully created BatchSandbox: example-batchsandbox, Replicas: 3
   ```

6. **获取和更新 BatchSandbox**
   ```
   Successfully updated BatchSandbox: example-batchsandbox, new Replicas: 5
   ```

7. **使用 Lister 从缓存读取**
   ```
   Retrieved Pool from cache: example-pool, PoolMax: 20
   Found 1 BatchSandbox(es) from cache
   ```

8. **清理资源**
   ```
   Successfully deleted BatchSandbox: example-batchsandbox
   Successfully deleted Pool: example-pool
   ```

## 代码结构

```
main.go
├── Controller struct          # 控制器结构
├── NewController()           # 创建控制器并注册事件处理器
├── DemonstrateClientsetUsage() # 演示 Clientset CRUD 操作
└── DemonstrateListerUsage()   # 演示 Lister 缓存读取
```

## 关键概念

### Clientset vs Lister

**何时使用 Clientset:**
- 需要创建、更新或删除资源
- 需要获取资源的最新状态
- 执行写操作

**何时使用 Lister:**
- 只需要读取资源
- 可以接受轻微的数据延迟
- 需要高性能的批量读取
- 减少 API Server 负载

### Informer 事件处理

Informer 会在资源变化时触发相应的事件处理器:
```go
AddFunc: func(obj interface{}) {
    // 资源被创建时调用
}
UpdateFunc: func(old, new interface{}) {
    // 资源被更新时调用
}
DeleteFunc: func(obj interface{}) {
    // 资源被删除时调用
}
```

## 生产环境建议

1. **使用 Lister 而不是频繁调用 Clientset.Get()**
   - Lister 从本地缓存读取,性能更好
   - 减少对 API Server 的压力

2. **正确处理 Informer 重新同步**
   - 设置合理的 resync 周期 (如 30 秒)
   - 在事件处理器中使用幂等操作

3. **使用 Workqueue 处理事件**
   - 避免在事件处理器中执行耗时操作
   - 使用 workqueue 实现重试机制

4. **处理资源版本冲突**
   - Update 操作时使用 optimistic locking
   - 捕获 Conflict 错误并重试

## 扩展阅读

- [Kubernetes Client-go 文档](https://github.com/kubernetes/client-go)
- [编写 Kubernetes 控制器](https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/)
- [Sample Controller](https://github.com/kubernetes/sample-controller)
