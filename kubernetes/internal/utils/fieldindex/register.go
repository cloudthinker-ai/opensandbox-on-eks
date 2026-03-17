// Copyright 2025 Alibaba Group Holding Ltd.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package fieldindex

import (
	"context"
	"sync"

	v1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"

	sandboxv1alpha1 "github.com/alibaba/OpenSandbox/sandbox-k8s/apis/sandbox/v1alpha1"
)

const (
	IndexNameForOwnerRefUID = "ownerRefUID"
	IndexNameForPoolRef     = "poolRef"
)

var (
	registerOnce sync.Once
)

var OwnerIndexFunc = func(obj client.Object) []string {
	var owners []string
	for _, ref := range obj.GetOwnerReferences() {
		owners = append(owners, string(ref.UID))
	}
	return owners
}

var PoolRefIndexFunc = func(obj client.Object) []string {
	batchSandbox, ok := obj.(*sandboxv1alpha1.BatchSandbox)
	if ok {
		return []string{batchSandbox.Spec.PoolRef}
	}
	return nil
}

func RegisterFieldIndexes(c cache.Cache) error {
	var err error
	registerOnce.Do(func() {
		// pod ownerReference
		if err = c.IndexField(context.TODO(), &v1.Pod{}, IndexNameForOwnerRefUID, OwnerIndexFunc); err != nil {
			return
		}
		if err = c.IndexField(context.TODO(), &sandboxv1alpha1.BatchSandbox{}, IndexNameForPoolRef, PoolRefIndexFunc); err != nil {
			return
		}
	})
	return err
}
