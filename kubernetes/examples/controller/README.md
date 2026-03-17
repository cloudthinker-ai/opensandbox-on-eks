# Controller Example

This example demonstrates how to use the generated clientset, informer, and lister to operate BatchSandbox and Pool custom resources.

## Features

### 1. Clientset (Client Set)
Used to interact directly with the Kubernetes API Server for CRUD operations:
- **Create**: Create new resources
- **Get**: Retrieve specific resources
- **List**: List all resources
- **Update**: Update existing resources
- **Delete**: Delete resources

### 2. Informer (Informer)
Used to watch resource changes and maintain local cache:
- Automatically watches resource changes from the API Server
- Triggers event handlers (Add/Update/Delete)
- Maintains a local cache of resources to reduce API Server load

### 3. Lister (Lister)
Used to read resources from the Informer's local cache:
- High-performance local cache reads
- Avoids frequent API Server access
- Supports filtering by namespace and labels

## Running the Example

### Prerequisites
1. CRDs are installed in the Kubernetes cluster
2. Have a kubeconfig file to access the cluster

### Install CRDs
```bash
# Run from project root directory
kubectl apply -f config/crd/bases/
```

### Run the Example Program
```bash
# Use default kubeconfig (~/.kube/config)
go run examples/controller/main.go

# Or specify kubeconfig path
go run examples/controller/main.go -kubeconfig=/path/to/kubeconfig
```

## Example Output

The program will perform the following operations:

1. **Create Pool resource**
   ```
   Successfully created Pool: example-pool
   ```

2. **Get Pool resource**
   ```
   Successfully retrieved Pool: example-pool, PoolMin: 2, PoolMax: 10
   ```

3. **List all Pool resources**
   ```
   Found 1 Pool(s):
     - example-pool (PoolMin: 2, PoolMax: 10)
   ```

4. **Update Pool resource**
   ```
   Successfully updated Pool: example-pool, new PoolMax: 20
   ```

5. **Create BatchSandbox resource**
   ```
   Successfully created BatchSandbox: example-batchsandbox, Replicas: 3
   ```

6. **Get and update BatchSandbox**
   ```
   Successfully updated BatchSandbox: example-batchsandbox, new Replicas: 5
   ```

7. **Use Lister to read from cache**
   ```
   Retrieved Pool from cache: example-pool, PoolMax: 20
   Found 1 BatchSandbox(es) from cache
   ```

8. **Cleanup resources**
   ```
   Successfully deleted BatchSandbox: example-batchsandbox
   Successfully deleted Pool: example-pool
   ```

## Code Structure

```
main.go
├── Controller struct          # Controller structure
├── NewController()           # Create controller and register event handlers
├── DemonstrateClientsetUsage() # Demonstrate Clientset CRUD operations
└── DemonstrateListerUsage()   # Demonstrate Lister cache reads
```

## Key Concepts

### Clientset vs Lister

**When to use Clientset:**
- Need to create, update, or delete resources
- Need to get the latest state of resources
- Performing write operations

**When to use Lister:**
- Only need to read resources
- Can tolerate slight data staleness
- Need high-performance batch reads
- Want to reduce API Server load

### Informer Event Handling

Informer triggers corresponding event handlers when resources change:
```go
AddFunc: func(obj interface{}) {
    // Called when resource is created
}
UpdateFunc: func(old, new interface{}) {
    // Called when resource is updated
}
DeleteFunc: func(obj interface{}) {
    // Called when resource is deleted
}
```

## Production Recommendations

1. **Use Lister instead of frequent Clientset.Get() calls**
   - Lister reads from local cache with better performance
   - Reduces pressure on the API Server

2. **Properly handle Informer resync**
   - Set a reasonable resync period (e.g., 30 seconds)
   - Use idempotent operations in event handlers

3. **Use Workqueue to process events**
   - Avoid time-consuming operations in event handlers
   - Use workqueue to implement retry mechanisms

4. **Handle resource version conflicts**
   - Use optimistic locking during Update operations
   - Catch Conflict errors and retry

## Further Reading

- [Kubernetes Client-go Documentation](https://github.com/kubernetes/client-go)
- [Writing Kubernetes Controllers](https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/)
- [Sample Controller](https://github.com/kubernetes/sample-controller)
