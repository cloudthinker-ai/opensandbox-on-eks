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

package task_executor

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Task represents the internal local task resource (LocalTask)
// It follows the Kubernetes resource model with Metadata, Spec, and Status.
type Task struct {
	Name              string       `json:"name"`
	DeletionTimestamp *metav1.Time `json:"deletionTimestamp,omitempty"`

	Process         *Process                `json:"process,omitempty"`
	PodTemplateSpec *corev1.PodTemplateSpec `json:"podTemplateSpec,omitempty"`

	ProcessStatus *ProcessStatus    `json:"processStatus,omitempty"`
	PodStatus     *corev1.PodStatus `json:"podStatus,omitempty"`
}

type Process struct {
	// Command command
	Command []string `json:"command"`
	// Arguments to the entrypoint.
	Args []string `json:"args,omitempty"`
	// List of environment variables to set in the process.
	Env []corev1.EnvVar `json:"env,omitempty"`
	// WorkingDir process working directory.
	WorkingDir string `json:"workingDir,omitempty"`
	// TimeoutSeconds process timeout seconds.
	TimeoutSeconds *int64 `json:"timeoutSeconds,omitempty"`
}

// ProcessStatus holds a possible state of process.
// Only one of its members may be specified.
// If none of them is specified, the default one is Waiting.
type ProcessStatus struct {
	// Details about a waiting process
	// +optional
	Waiting *Waiting `json:"waiting,omitempty"`
	// Details about a running process
	// +optional
	Running *Running `json:"running,omitempty"`
	// Details about a terminated process
	// +optional
	Terminated *Terminated `json:"terminated,omitempty"`
}

// Waiting is a waiting state of a process.
type Waiting struct {
	// (brief) reason the process is not yet running.
	// +optional
	Reason string `json:"reason,omitempty"`
	// Message regarding why the process is not yet running.
	// +optional
	Message string `json:"message,omitempty"`
}

// Running is a running state of a process.
type Running struct {
	// Time at which the process was last (re-)started
	// +optional
	StartedAt metav1.Time `json:"startedAt"`
}

// Terminated is a terminated state of a process.
type Terminated struct {
	// Exit status from the last termination of the process
	ExitCode int32 `json:"exitCode"`
	// Signal from the last termination of the process
	// +optional
	Signal int32 `json:"signal,omitempty"`
	// (brief) reason from the last termination of the process
	// +optional
	Reason string `json:"reason,omitempty"`
	// Message regarding the last termination of the process
	// +optional
	Message string `json:"message,omitempty"`
	// Time at which previous execution of the process started
	// +optional
	StartedAt metav1.Time `json:"startedAt,omitempty"`
	// Time at which the process last terminated
	// +optional
	FinishedAt metav1.Time `json:"finishedAt,omitempty"`
}
