// Copyright 2026 Alibaba Group Holding Ltd.
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

package runtime

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestLoadExtraEnvFromFileUnset(t *testing.T) {
	t.Setenv("EXECD_ENVS", "")
	if got := loadExtraEnvFromFile(); got != nil {
		t.Fatalf("expected nil when EXECD_ENVS unset, got %#v", got)
	}
}

func TestLoadExtraEnvFromFileParsesAndExpands(t *testing.T) {
	dir := t.TempDir()
	envFile := filepath.Join(dir, "env")

	t.Setenv("EXECD_ENVS", envFile)
	t.Setenv("BASE_DIR", "/opt/base")

	content := strings.Join([]string{
		"# comment",
		"FOO=bar",
		"PATH=$BASE_DIR/bin",
		"MALFORMED",
		"EMPTY=",
		"",
	}, "\n")

	if err := os.WriteFile(envFile, []byte(content), 0o644); err != nil {
		t.Fatalf("write env file: %v", err)
	}

	got := loadExtraEnvFromFile()
	if len(got) != 3 {
		t.Fatalf("expected 3 entries, got %#v", got)
	}

	if got["FOO"] != "bar" {
		t.Fatalf("FOO mismatch, got %q", got["FOO"])
	}
	if got["PATH"] != "/opt/base/bin" {
		t.Fatalf("PATH expansion mismatch, got %q", got["PATH"])
	}
	if val, ok := got["EMPTY"]; !ok || val != "" {
		t.Fatalf("EMPTY mismatch, got %q (present=%v)", val, ok)
	}
}

func TestLoadExtraEnvFromFileMissingFile(t *testing.T) {
	dir := t.TempDir()
	envFile := filepath.Join(dir, "does-not-exist")
	t.Setenv("EXECD_ENVS", envFile)

	if got := loadExtraEnvFromFile(); got != nil {
		t.Fatalf("expected nil for missing file, got %#v", got)
	}
}

func TestMergeEnvsOverlaysExtra(t *testing.T) {
	base := []string{"A=1", "B=2"}
	extra := map[string]string{"B": "override", "C": "3"}

	merged := mergeEnvs(base, extra)
	got := make(map[string]string)
	for _, kv := range merged {
		parts := strings.SplitN(kv, "=", 2)
		if len(parts) == 2 {
			got[parts[0]] = parts[1]
		}
	}

	if len(got) != 3 {
		t.Fatalf("expected 3 entries, got %#v", got)
	}
	if got["A"] != "1" {
		t.Fatalf("A mismatch, got %q", got["A"])
	}
	if got["B"] != "override" {
		t.Fatalf("B mismatch, got %q", got["B"])
	}
	if got["C"] != "3" {
		t.Fatalf("C mismatch, got %q", got["C"])
	}
}

func TestMergeEnvsMultipleMaps(t *testing.T) {
	base := []string{"A=1", "B=2"}
	extra1 := map[string]string{"B": "override", "C": "3"}
	extra2 := map[string]string{"C": "final", "D": "4"}
	merged := mergeEnvs(base, extra1, extra2)

	m := make(map[string]string)
	for _, kv := range merged {
		pair := strings.SplitN(kv, "=", 2)
		m[pair[0]] = pair[1]
	}

	assert.Equal(t, "1", m["A"])
	assert.Equal(t, "override", m["B"])
	assert.Equal(t, "final", m["C"])
	assert.Equal(t, "4", m["D"])
}

func TestMergeEnvsNilMap(t *testing.T) {
	base := []string{"A=1"}
	merged := mergeEnvs(base, nil, map[string]string{"B": "2"}, nil)

	m := make(map[string]string)
	for _, kv := range merged {
		pair := strings.SplitN(kv, "=", 2)
		m[pair[0]] = pair[1]
	}

	assert.Equal(t, "1", m["A"])
	assert.Equal(t, "2", m["B"])
}
