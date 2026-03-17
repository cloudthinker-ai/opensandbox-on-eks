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

package utils

import (
	"slices"
	"testing"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestWithPodIndexSorter(t *testing.T) {
	tests := []struct {
		name     string
		podIndex map[string]int
		podA     *v1.Pod
		podB     *v1.Pod
		want     int
	}{
		{
			name: "a index < b index",
			podIndex: map[string]int{
				"pod-a": 1,
				"pod-b": 2,
			},
			podA: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want: -1,
		},
		{
			name: "a index > b index",
			podIndex: map[string]int{
				"pod-a": 5,
				"pod-b": 3,
			},
			podA: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want: 1,
		},
		{
			name: "a index == b index",
			podIndex: map[string]int{
				"pod-a": 2,
				"pod-b": 2,
			},
			podA: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want: 0,
		},
		{
			name: "a has no index, b has index - a should be last",
			podIndex: map[string]int{
				"pod-b": 1,
			},
			podA: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want: 1,
		},
		{
			name: "a has index, b has no index - b should be last",
			podIndex: map[string]int{
				"pod-a": 1,
			},
			podA: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want: -1,
		},
		{
			name:     "both have no index",
			podIndex: map[string]int{},
			podA:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want:     0,
		},
		{
			name: "index 0 vs index 1",
			podIndex: map[string]int{
				"pod-a": 0,
				"pod-b": 1,
			},
			podA: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB: &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want: -1,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			sorter := WithPodIndexSorter(tt.podIndex)
			got := sorter(tt.podA, tt.podB)
			if got != tt.want {
				t.Errorf("WithPodIndexSorter() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestMultiPodSorter(t *testing.T) {
	tests := []struct {
		name     string
		sorters  MultiPodSorter
		podA     *v1.Pod
		podB     *v1.Pod
		want     int
		wantDesc string
	}{
		{
			name: "first sorter decides - a < b",
			sorters: MultiPodSorter{
				func(a, b *v1.Pod) int {
					if a.Name < b.Name {
						return -1
					} else if a.Name > b.Name {
						return 1
					}
					return 0
				},
			},
			podA:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want:     -1,
			wantDesc: "pod-a should come before pod-b",
		},
		{
			name: "first sorter equal, second sorter decides",
			sorters: MultiPodSorter{
				func(a, b *v1.Pod) int {
					return 0
				},
				func(a, b *v1.Pod) int {
					if a.Name < b.Name {
						return -1
					} else if a.Name > b.Name {
						return 1
					}
					return 0
				},
			},
			podA:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want:     -1,
			wantDesc: "first sorter returns 0, second sorter decides",
		},
		{
			name: "all sorters return equal",
			sorters: MultiPodSorter{
				func(a, b *v1.Pod) int { return 0 },
				func(a, b *v1.Pod) int { return 0 },
				func(a, b *v1.Pod) int { return 0 },
			},
			podA:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want:     0,
			wantDesc: "all sorters return 0",
		},
		{
			name: "index sorter then name sorter - decided by index",
			sorters: MultiPodSorter{
				WithPodIndexSorter(map[string]int{
					"pod-b": 0,
					"pod-a": 1,
				}),
				PodNameSorter,
			},
			podA:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want:     1,
			wantDesc: "pod-b has lower index (0) than pod-a (1), so pod-a > pod-b",
		},
		{
			name: "index sorter then name sorter - decided by name",
			sorters: MultiPodSorter{
				WithPodIndexSorter(map[string]int{
					"pod-a": 1,
					"pod-b": 1,
				}),
				PodNameSorter,
			},
			podA:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want:     -1,
			wantDesc: "same index, fallback to name comparison",
		},
		{
			name:     "empty sorters list",
			sorters:  MultiPodSorter{},
			podA:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
			podB:     &v1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
			want:     0,
			wantDesc: "no sorters, should return 0",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := tt.sorters.Sort(tt.podA, tt.podB)
			if got != tt.want {
				t.Errorf("MultiPodSorter.Sort() = %v, want %v (%s)", got, tt.want, tt.wantDesc)
			}
		})
	}
}

func TestMultiPodSorter_Integration(t *testing.T) {
	pods := []*v1.Pod{
		{ObjectMeta: metav1.ObjectMeta{Name: "pod-c"}},
		{ObjectMeta: metav1.ObjectMeta{Name: "pod-a"}},
		{ObjectMeta: metav1.ObjectMeta{Name: "pod-b"}},
		{ObjectMeta: metav1.ObjectMeta{Name: "pod-d"}},
	}

	podIndex := map[string]int{
		"pod-a": 2,
		"pod-b": 0,
		"pod-c": 1,
	}

	sorter := MultiPodSorter{
		WithPodIndexSorter(podIndex),
		PodNameSorter,
	}

	slices.SortStableFunc(pods, sorter.Sort)

	expectedOrder := []string{"pod-b", "pod-c", "pod-a", "pod-d"}

	for i, pod := range pods {
		if pod.Name != expectedOrder[i] {
			t.Errorf("pod at index %d: got %s, want %s", i, pod.Name, expectedOrder[i])
		}
	}
}
