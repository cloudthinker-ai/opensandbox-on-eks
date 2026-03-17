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

package policy

import (
	"net/netip"
	"testing"
)

func TestParsePolicy_EmptyOrNullDefaultsDeny(t *testing.T) {
	cases := []string{
		"",
		"   ",
		"null",
		"{}\n",
	}
	for _, raw := range cases {
		p, err := ParsePolicy(raw)
		if err != nil {
			t.Fatalf("raw %q returned error: %v", raw, err)
		}
		if p == nil {
			t.Fatalf("raw %q expected default deny policy, got nil", raw)
		}
		if p.DefaultAction != ActionDeny {
			t.Fatalf("raw %q expected defaultAction deny, got %+v", raw, p)
		}
		if got := p.Evaluate("example.com."); got != ActionDeny {
			t.Fatalf("raw %q expected deny evaluation, got %s", raw, got)
		}
	}
}

func TestParsePolicy_DefaultActionFallback(t *testing.T) {
	p, err := ParsePolicy(`{"egress":[{"action":"allow","target":"example.com"}]}`)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p == nil {
		t.Fatalf("expected policy object, got nil")
	}
	if p.DefaultAction != ActionDeny {
		t.Fatalf("expected defaultAction fallback to deny, got %+v", p)
	}
}

func TestParsePolicy_EmptyEgressDefaultsDeny(t *testing.T) {
	p, err := ParsePolicy(`{"defaultAction":""}`)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if p.DefaultAction != ActionDeny {
		t.Fatalf("expected default deny when defaultAction missing, got %+v", p)
	}
	if got := p.Evaluate("anything.com."); got != ActionDeny {
		t.Fatalf("expected evaluation deny for empty egress, got %s", got)
	}
}

func TestParsePolicy_IPAndCIDRSupported(t *testing.T) {
	raw := `{
		"defaultAction":"deny",
		"egress":[
			{"action":"allow","target":"1.1.1.1"},
			{"action":"allow","target":"2.2.0.0/16"},
			{"action":"deny","target":"2001:db8::/32"},
			{"action":"deny","target":"2001:db8::1"}
		]
	}`
	p, err := ParsePolicy(raw)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	allowV4, allowV6, denyV4, denyV6 := p.StaticIPSets()
	if len(allowV4) != 2 || allowV4[0] != "1.1.1.1" || allowV4[1] != "2.2.0.0/16" {
		t.Fatalf("allowV4 unexpected: %+v", allowV4)
	}
	if len(denyV6) != 2 {
		t.Fatalf("expected 2 denyV6 entries, got %+v", denyV6)
	}
	if len(allowV6) != 0 || len(denyV4) != 0 {
		t.Fatalf("allowV6/denyV4 should be empty, got %v / %v", allowV6, denyV4)
	}
}

func TestParsePolicy_InvalidAction(t *testing.T) {
	if _, err := ParsePolicy(`{"egress":[{"action":"foo","target":"example.com"}]}`); err == nil {
		t.Fatalf("expected error for invalid action")
	}
}

func TestParsePolicy_EmptyTargetError(t *testing.T) {
	if _, err := ParsePolicy(`{"egress":[{"action":"allow","target":""}]}`); err == nil {
		t.Fatalf("expected error for empty target")
	}
}

func TestWithExtraAllowIPs(t *testing.T) {
	p, _ := ParsePolicy(`{"defaultAction":"deny","egress":[{"action":"allow","target":"example.com"}]}`)
	allowV4, allowV6, _, _ := p.StaticIPSets()
	if len(allowV4) != 0 || len(allowV6) != 0 {
		t.Fatalf("domain-only policy should have no static allow IPs, got allowV4=%v allowV6=%v", allowV4, allowV6)
	}

	ips := []netip.Addr{
		netip.MustParseAddr("192.168.65.7"),
		netip.MustParseAddr("2001:db8::1"),
	}
	merged := p.WithExtraAllowIPs(ips)
	if merged == p {
		t.Fatalf("expected new policy instance")
	}
	allowV4, allowV6, _, _ = merged.StaticIPSets()
	if len(allowV4) != 1 || allowV4[0] != "192.168.65.7" {
		t.Fatalf("allowV4 expected [192.168.65.7], got %v", allowV4)
	}
	if len(allowV6) != 1 || allowV6[0] != "2001:db8::1" {
		t.Fatalf("allowV6 expected [2001:db8::1], got %v", allowV6)
	}

	// nil/empty ips returns same policy
	if got := p.WithExtraAllowIPs(nil); got != p {
		t.Fatalf("WithExtraAllowIPs(nil) should return same policy")
	}
	if got := p.WithExtraAllowIPs([]netip.Addr{}); got != p {
		t.Fatalf("WithExtraAllowIPs([]) should return same policy")
	}
}

func TestIsDeniedIP(t *testing.T) {
	p, err := ParsePolicy(`{
		"defaultAction":"allow",
		"egress":[
			{"action":"deny","target":"10.0.0.0/8"},
			{"action":"deny","target":"192.168.1.1"},
			{"action":"allow","target":"1.1.1.1"}
		]
	}`)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}

	tests := []struct {
		ip   string
		want bool
	}{
		{"10.0.0.1", true},       // matches deny CIDR
		{"10.255.255.255", true}, // matches deny CIDR
		{"192.168.1.1", true},    // matches deny IP
		{"192.168.1.2", false},   // no match
		{"1.1.1.1", false},       // allow rule, not deny
		{"8.8.8.8", false},       // no match
	}
	for _, tt := range tests {
		addr := netip.MustParseAddr(tt.ip)
		got := p.IsDeniedIP(addr)
		if got != tt.want {
			t.Errorf("IsDeniedIP(%s) = %v, want %v", tt.ip, got, tt.want)
		}
	}
}

func TestIsDeniedIP_NilPolicy(t *testing.T) {
	var p *NetworkPolicy
	if p.IsDeniedIP(netip.MustParseAddr("10.0.0.1")) {
		t.Fatal("nil policy should return false")
	}
}

func TestIsExplicitlyAllowedDomain(t *testing.T) {
	p, err := ParsePolicy(`{
		"defaultAction":"allow",
		"egress":[
			{"action":"allow","target":"rds.amazonaws.com"},
			{"action":"deny","target":"evil.com"},
			{"action":"allow","target":"*.internal.example.com"},
			{"action":"deny","target":"10.0.0.0/8"}
		]
	}`)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}

	tests := []struct {
		domain string
		want   bool
	}{
		{"rds.amazonaws.com", true},          // exact match on explicit allow
		{"rds.amazonaws.com.", true},          // trailing dot
		{"RDS.amazonaws.COM", true},           // case insensitive
		{"db.internal.example.com", true},     // wildcard match
		{"evil.com", false},                   // explicit deny rule
		{"other.com", false},                  // no matching rule (falls to defaultAction)
		{"internal.example.com", false},       // wildcard *.x doesn't match x itself
	}
	for _, tt := range tests {
		got := p.IsExplicitlyAllowedDomain(tt.domain)
		if got != tt.want {
			t.Errorf("IsExplicitlyAllowedDomain(%q) = %v, want %v", tt.domain, got, tt.want)
		}
	}
}

func TestIsExplicitlyAllowedDomain_NilPolicy(t *testing.T) {
	var p *NetworkPolicy
	if p.IsExplicitlyAllowedDomain("example.com") {
		t.Fatal("nil policy should return false")
	}
}

func TestWithExtraAllowIPs_SkipsCoveredByCIDR(t *testing.T) {
	p, err := ParsePolicy(`{"defaultAction":"allow","egress":[{"action":"allow","target":"172.16.0.0/12"}]}`)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	extra := []netip.Addr{netip.MustParseAddr("172.20.0.10"), netip.MustParseAddr("8.8.8.8")}
	got := p.WithExtraAllowIPs(extra)
	allowV4, _, _, _ := got.StaticIPSets()
	// 172.20.0.10 should be skipped (covered by 172.16.0.0/12), 8.8.8.8 should be kept
	if len(allowV4) != 2 {
		t.Fatalf("expected 2 allowV4 entries (CIDR + 8.8.8.8), got %v", allowV4)
	}
	if allowV4[0] != "172.16.0.0/12" {
		t.Fatalf("expected first entry to be CIDR 172.16.0.0/12, got %s", allowV4[0])
	}
	if allowV4[1] != "8.8.8.8" {
		t.Fatalf("expected second entry to be 8.8.8.8, got %s", allowV4[1])
	}
}

func TestWithExtraAllowIPs_SkipsCoveredByIPv6CIDR(t *testing.T) {
	p, err := ParsePolicy(`{"defaultAction":"allow","egress":[{"action":"allow","target":"2001:db8::/32"}]}`)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	extra := []netip.Addr{
		netip.MustParseAddr("2001:db8::53"),       // covered by CIDR, should be skipped
		netip.MustParseAddr("2001:4860:4860::8888"), // not covered, should be added
	}
	got := p.WithExtraAllowIPs(extra)
	_, allowV6, _, _ := got.StaticIPSets()
	if len(allowV6) != 2 {
		t.Fatalf("expected 2 allowV6 entries (CIDR + 2001:4860:4860::8888), got %v", allowV6)
	}
	if allowV6[0] != "2001:db8::/32" {
		t.Fatalf("expected first entry to be CIDR 2001:db8::/32, got %s", allowV6[0])
	}
	if allowV6[1] != "2001:4860:4860::8888" {
		t.Fatalf("expected second entry to be 2001:4860:4860::8888, got %s", allowV6[1])
	}
}

func TestWithExtraAllowIPs_DenyCIDRDoesNotSkip(t *testing.T) {
	p, err := ParsePolicy(`{"defaultAction":"deny","egress":[{"action":"deny","target":"10.0.0.0/8"}]}`)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	extra := []netip.Addr{netip.MustParseAddr("10.0.0.53")}
	got := p.WithExtraAllowIPs(extra)
	allowV4, _, _, _ := got.StaticIPSets()
	// 10.0.0.53 is inside a deny CIDR but must still be added as an allow rule
	if len(allowV4) != 1 {
		t.Fatalf("expected 1 allowV4 entry (10.0.0.53), got %v", allowV4)
	}
	if allowV4[0] != "10.0.0.53" {
		t.Fatalf("expected 10.0.0.53, got %s", allowV4[0])
	}
}
