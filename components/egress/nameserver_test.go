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

package main

import (
	"net/netip"
	"os"
	"path/filepath"
	"testing"

	"github.com/alibaba/opensandbox/egress/pkg/constants"
	"github.com/alibaba/opensandbox/egress/pkg/policy"
)

func TestAllowIPsForNft_EmptyResolv(t *testing.T) {
	dir := t.TempDir()
	resolv := filepath.Join(dir, "resolv.conf")
	if err := os.WriteFile(resolv, []byte("# empty\n"), 0644); err != nil {
		t.Fatal(err)
	}
	ips := AllowIPsForNft(resolv, nil)
	if len(ips) != 1 {
		t.Fatalf("expected 1 IP (127.0.0.1), got %d", len(ips))
	}
	if ips[0] != netip.MustParseAddr("127.0.0.1") {
		t.Fatalf("expected 127.0.0.1, got %s", ips[0])
	}
}

func TestAllowIPsForNft_ValidNameservers(t *testing.T) {
	dir := t.TempDir()
	resolv := filepath.Join(dir, "resolv.conf")
	// Standard resolv.conf with two nameservers
	content := "nameserver 192.168.65.7\nnameserver 10.0.0.1\n"
	if err := os.WriteFile(resolv, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
	ips := AllowIPsForNft(resolv, nil)
	if len(ips) != 3 {
		t.Fatalf("expected 3 IPs (127.0.0.1 + 2 nameservers), got %d", len(ips))
	}
	if ips[0] != netip.MustParseAddr("127.0.0.1") {
		t.Fatalf("expected first 127.0.0.1, got %s", ips[0])
	}
	if ips[1] != netip.MustParseAddr("192.168.65.7") {
		t.Fatalf("expected 192.168.65.7, got %s", ips[1])
	}
	if ips[2] != netip.MustParseAddr("10.0.0.1") {
		t.Fatalf("expected 10.0.0.1, got %s", ips[2])
	}
}

func TestAllowIPsForNft_FiltersInvalid(t *testing.T) {
	dir := t.TempDir()
	resolv := filepath.Join(dir, "resolv.conf")
	// 0.0.0.0 and 127.0.0.11 should be filtered; 192.168.1.1 kept
	content := "nameserver 0.0.0.0\nnameserver 192.168.1.1\nnameserver 127.0.0.11\n"
	if err := os.WriteFile(resolv, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
	ips := AllowIPsForNft(resolv, nil)
	if len(ips) != 2 {
		t.Fatalf("expected 2 IPs (127.0.0.1 + 192.168.1.1), got %d: %v", len(ips), ips)
	}
	if ips[0] != netip.MustParseAddr("127.0.0.1") {
		t.Fatalf("expected first 127.0.0.1, got %s", ips[0])
	}
	if ips[1] != netip.MustParseAddr("192.168.1.1") {
		t.Fatalf("expected 192.168.1.1, got %s", ips[1])
	}
}

func TestAllowIPsForNft_Cap(t *testing.T) {
	dir := t.TempDir()
	resolv := filepath.Join(dir, "resolv.conf")
	content := "nameserver 10.0.0.1\nnameserver 10.0.0.2\nnameserver 10.0.0.3\nnameserver 10.0.0.4\n"
	if err := os.WriteFile(resolv, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
	old := os.Getenv(constants.EnvMaxNameservers)
	defer os.Setenv(constants.EnvMaxNameservers, old)
	os.Setenv(constants.EnvMaxNameservers, "2")

	ips := AllowIPsForNft(resolv, nil)
	// 127.0.0.1 + 2 nameservers (cap)
	if len(ips) != 3 {
		t.Fatalf("expected 3 IPs (127.0.0.1 + 2 capped), got %d: %v", len(ips), ips)
	}
	if ips[1] != netip.MustParseAddr("10.0.0.1") || ips[2] != netip.MustParseAddr("10.0.0.2") {
		t.Fatalf("expected first two nameservers, got %v", ips[1:])
	}
}

func TestIsValidNameserverIP(t *testing.T) {
	tests := []struct {
		ip   string
		want bool
	}{
		{"0.0.0.0", false},
		{"::", false},
		{"127.0.0.1", false},
		{"127.0.0.11", false},
		{"::1", false},
		{"192.168.65.7", true},
		{"10.0.0.1", true},
		{"8.8.8.8", true},
	}
	for _, tt := range tests {
		ip := netip.MustParseAddr(tt.ip)
		got := isValidNameserverIP(ip)
		if got != tt.want {
			t.Errorf("isValidNameserverIP(%s) = %v, want %v", tt.ip, got, tt.want)
		}
	}
}

func TestMaxNameserversFromEnv(t *testing.T) {
	old := os.Getenv(constants.EnvMaxNameservers)
	defer os.Setenv(constants.EnvMaxNameservers, old)

	for _, s := range []string{"", "x", "-1"} {
		os.Setenv(constants.EnvMaxNameservers, s)
		if got := maxNameserversFromEnv(); got != constants.DefaultMaxNameservers {
			t.Errorf("maxNameserversFromEnv(%q) = %d, want default %d", s, got, constants.DefaultMaxNameservers)
		}
	}
	os.Setenv(constants.EnvMaxNameservers, "0")
	if got := maxNameserversFromEnv(); got != 0 {
		t.Errorf("maxNameserversFromEnv(0) = %d, want 0", got)
	}
	os.Setenv(constants.EnvMaxNameservers, "5")
	if got := maxNameserversFromEnv(); got != 5 {
		t.Errorf("maxNameserversFromEnv(5) = %d, want 5", got)
	}
	os.Setenv(constants.EnvMaxNameservers, "99")
	if got := maxNameserversFromEnv(); got != 10 {
		t.Errorf("maxNameserversFromEnv(99) = %d, want 10 (capped)", got)
	}
}

func TestMergeUpstreamIP_NewIP(t *testing.T) {
	base := []netip.Addr{netip.MustParseAddr("127.0.0.1")}
	got := mergeUpstreamIP(base, "172.20.0.10")
	if len(got) != 2 {
		t.Fatalf("expected 2 IPs, got %d: %v", len(got), got)
	}
	if got[1] != netip.MustParseAddr("172.20.0.10") {
		t.Fatalf("expected 172.20.0.10, got %s", got[1])
	}
}

func TestMergeUpstreamIP_Duplicate(t *testing.T) {
	base := []netip.Addr{netip.MustParseAddr("127.0.0.1"), netip.MustParseAddr("172.20.0.10")}
	got := mergeUpstreamIP(base, "172.20.0.10")
	if len(got) != 2 {
		t.Fatalf("expected 2 IPs (dedup), got %d: %v", len(got), got)
	}
}

func TestMergeUpstreamIP_Empty(t *testing.T) {
	base := []netip.Addr{netip.MustParseAddr("127.0.0.1")}
	got := mergeUpstreamIP(base, "")
	if len(got) != 1 {
		t.Fatalf("expected 1 IP (no-op), got %d: %v", len(got), got)
	}
}

func TestAllowIPsForNft_FiltersDeniedIPs(t *testing.T) {
	dir := t.TempDir()
	resolv := filepath.Join(dir, "resolv.conf")
	content := "nameserver 10.0.0.1\nnameserver 192.168.1.1\n"
	if err := os.WriteFile(resolv, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
	pol, _ := policy.ParsePolicy(`{"defaultAction":"allow","egress":[{"action":"deny","target":"10.0.0.0/8"}]}`)
	ips := AllowIPsForNft(resolv, pol)
	// 127.0.0.1 + 192.168.1.1 only; 10.0.0.1 matches deny CIDR
	if len(ips) != 2 {
		t.Fatalf("expected 2 IPs (127.0.0.1 + 192.168.1.1), got %d: %v", len(ips), ips)
	}
	if ips[0] != netip.MustParseAddr("127.0.0.1") {
		t.Fatalf("expected first 127.0.0.1, got %s", ips[0])
	}
	if ips[1] != netip.MustParseAddr("192.168.1.1") {
		t.Fatalf("expected 192.168.1.1, got %s", ips[1])
	}
}
