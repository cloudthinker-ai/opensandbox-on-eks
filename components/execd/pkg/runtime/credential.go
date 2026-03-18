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

//go:build !windows
// +build !windows

package runtime

import (
	"os/user"
	"strconv"
	"syscall"

	"github.com/alibaba/opensandbox/execd/pkg/log"
)

// runCredential holds the UID/GID resolved at startup. Commands spawned
// by execd will be forced to run under this identity via SysProcAttr.Credential,
// providing defense-in-depth even if the shell-level setpriv fails.
var runCredential *syscall.Credential

func init() {
	u, err := user.Current()
	if err != nil {
		log.Error("credential: failed to resolve current user: %v", err)
		return
	}
	uid, err := strconv.ParseUint(u.Uid, 10, 32)
	if err != nil {
		log.Error("credential: invalid UID %q: %v", u.Uid, err)
		return
	}
	gid, err := strconv.ParseUint(u.Gid, 10, 32)
	if err != nil {
		log.Error("credential: invalid GID %q: %v", u.Gid, err)
		return
	}
	runCredential = &syscall.Credential{
		Uid:         uint32(uid),
		Gid:         uint32(gid),
		NoSetGroups: true,
	}
	log.Info("credential: commands will run as uid=%d gid=%d", uid, gid)
}
