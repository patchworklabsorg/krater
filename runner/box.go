package main

import (
	"bufio"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// SSHPort is the port whose established connections count as open sessions.
const SSHPort = 22

// writeAuthorizedKeys replaces ~/.ssh/authorized_keys with exactly the box owner's keys. Krater stores keys as
// `type base64` only, so a line can't carry options; the runner still drops anything that isn't one line.
func writeAuthorizedKeys(home string, keys []string) error {
	dir := filepath.Join(home, ".ssh")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		return err
	}
	var lines []string
	for _, key := range keys {
		key = strings.TrimSpace(key)
		if key == "" || strings.ContainsAny(key, "\r\n") || len(strings.Fields(key)) != 2 {
			continue
		}
		lines = append(lines, key)
	}
	path := filepath.Join(dir, "authorized_keys")
	tmp := path + ".krater-partial"
	if err := os.WriteFile(tmp, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// countSessions counts established TCP connections to local `port`, from /proc/net/tcp and tcp6.
func countSessions(procNet string, port int) int {
	n := 0
	for _, name := range []string{"tcp", "tcp6"} {
		file, err := os.Open(filepath.Join(procNet, name))
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(file)
		scanner.Scan() // header
		for scanner.Scan() {
			fields := strings.Fields(scanner.Text())
			if len(fields) < 4 || fields[3] != "01" { // 01 = ESTABLISHED
				continue
			}
			local := fields[1]
			colon := strings.LastIndexByte(local, ':')
			if colon < 0 {
				continue
			}
			if hexPort(local[colon+1:]) == port {
				n++
			}
		}
		file.Close()
	}
	return n
}

func hexPort(s string) int {
	v := 0
	for _, c := range s {
		switch {
		case c >= '0' && c <= '9':
			v = v*16 + int(c-'0')
		case c >= 'A' && c <= 'F':
			v = v*16 + int(c-'A'+10)
		case c >= 'a' && c <= 'f':
			v = v*16 + int(c-'a'+10)
		default:
			return -1
		}
	}
	return v
}

// boxSaveWindow bounds one round of retries at saving /workspace. It must stay well under Krater's box sync
// deadline (minutes), and heartbeats pause while it runs.
var boxSaveWindow = 60 * time.Second

// saveWorkspace pushes /workspace, retrying a transient storage error for a bounded time.
func saveWorkspace(ctx context.Context, workspace *Syncer) error {
	return retry(ctx, boxSaveWindow, func() error {
		_, err := workspace.Push(ctx)
		return err
	})
}

// runBox keeps a box going: it reports sessions and GPU use, saves /workspace on a timer and whenever Krater is
// about to stop the machine, and never returns on its own (Krater stops or destroys the machine).
func runBox(ctx context.Context, api *API, info *StartInfo, workspace *Syncer, gpu func() (float64, bool), procNet string) {
	sampler := newGPUSampler(gpu)
	sampler.take()
	go sampler.run(ctx, 15*time.Second)
	beatEvery := seconds(info.HeartbeatSeconds, 60)
	syncEvery := seconds(info.CheckpointSyncSeconds, 600)
	lastSync := time.Now()
	ticker := time.NewTicker(beatEvery)
	defer ticker.Stop()
	for {
		beat := map[string]any{"phase": "box", "sessions": countSessions(procNet, SSHPort)}
		if util, ok := sampler.average(); ok {
			beat["gpu_util"] = util
		}
		reply, err := api.HeartbeatReply(ctx, beat)
		var denied *ErrDenied
		switch {
		case errors.As(err, &denied):
			logf("Krater no longer accepts this box: %v", err)
			return
		case err != nil:
			logf("heartbeat failed: %v", err)
		case reply.Sync:
			// Confirm only a save that happened: Krater takes the confirmation for "/workspace is safe" and stops
			// the box. On failure say nothing: Krater keeps asking on every heartbeat until its sync deadline,
			// when it records the box as not saved.
			if err := saveWorkspace(ctx, workspace); err != nil {
				logf("saving /workspace failed, NOT confirming the save to Krater: %v", err)
			} else if err := api.Synced(ctx); err != nil {
				logf("could not confirm the save: %v", err)
			} else {
				logf("saved /workspace; Krater stops the box next")
			}
			lastSync = time.Now()
		case time.Since(lastSync) >= syncEvery:
			if n, err := workspace.Push(ctx); err != nil {
				logf("saving /workspace failed: %v", err)
			} else if n > 0 {
				logf("saved %d workspace files", n)
			}
			lastSync = time.Now()
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
