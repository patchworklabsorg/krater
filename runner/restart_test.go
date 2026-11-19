package main

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// A container restarted on the same machine (Vast outbids an interruptible machine, pauses it with its disk, and
// starts the container again) finds the last run's checkpoint directory as it was left.

func checkpointSyncer(f *fakeKrater, dir string) *Syncer {
	api := &API{BaseURL: f.server.URL, Token: f.token, HTTP: http.DefaultClient}
	s := NewSyncer(api, "checkpoints", dir, http.DefaultClient)
	s.StampPath = filepath.Join(dir, stampFileName)
	return s
}

// writeLater writes `data` to dir/name and sets its modification time a second past the previous one, so the change
// shows whatever the file system's time resolution.
func writeLater(t *testing.T, dir, name, data string) {
	t.Helper()
	path := filepath.Join(dir, name)
	before := time.Now()
	if info, err := os.Stat(path); err == nil {
		before = info.ModTime()
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
		t.Fatal(err)
	}
	later := before.Add(time.Second)
	if err := os.Chtimes(path, later, later); err != nil {
		t.Fatal(err)
	}
}

func read(t *testing.T, dir, name string) string {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// firstRun uploads the files as one run of the job does, and returns the directory it left.
func firstRun(t *testing.T, f *fakeKrater, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	s := checkpointSyncer(f, dir)
	if _, err := s.Restore(context.Background()); err != nil {
		t.Fatal(err)
	}
	for name, data := range files {
		writeLater(t, dir, name, data)
	}
	if n, err := s.Push(context.Background()); err != nil || n != len(files) {
		t.Fatalf("first run pushed %d, %v", n, err)
	}
	return dir
}

func TestARestartKeepsALocalFileThatGrewSinceItsUploadAndSendsIt(t *testing.T) {
	f := newFakeKrater(t)
	dir := firstRun(t, f, map[string]string{"metrics.log": "step 1\n"})
	writeLater(t, dir, "metrics.log", "step 1\nstep 2\n") // the run went on after the upload

	s := checkpointSyncer(f, dir)
	if n, err := s.Restore(context.Background()); err != nil || n != 0 {
		t.Fatalf("Restore = %d, %v; want nothing downloaded", n, err)
	}
	if got := read(t, dir, "metrics.log"); got != "step 1\nstep 2\n" {
		t.Fatalf("the restart rolled metrics.log back to %q", got)
	}
	if n, err := s.Push(context.Background()); err != nil || n != 1 {
		t.Fatalf("Push = %d, %v; want the grown log sent", n, err)
	}
	if got := string(f.objects["checkpoints/metrics.log"]); got != "step 1\nstep 2\n" {
		t.Errorf("stored metrics.log = %q", got)
	}
}

func TestARestartSendsACheckpointOverwrittenAtTheSameSize(t *testing.T) {
	// latest.pt is rewritten in place each save, and model weights are always the same size.
	f := newFakeKrater(t)
	dir := firstRun(t, f, map[string]string{"latest.pt": "step-0100"})
	writeLater(t, dir, "latest.pt", "step-0200")

	s := checkpointSyncer(f, dir)
	if _, err := s.Restore(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n, err := s.Push(context.Background()); err != nil || n != 1 {
		t.Fatalf("Push = %d, %v; want the newer latest.pt sent", n, err)
	}
	if got := string(f.objects["checkpoints/latest.pt"]); got != "step-0200" {
		t.Errorf("stored latest.pt = %q", got)
	}
}

func TestARestartStillDownloadsAFileThatChangedOnlyInStorage(t *testing.T) {
	f := newFakeKrater(t)
	dir := firstRun(t, f, map[string]string{"ckpt.txt": "v1"})
	f.objects["checkpoints/ckpt.txt"] = []byte("v2, stored since")

	s := checkpointSyncer(f, dir)
	if n, err := s.Restore(context.Background()); err != nil || n != 1 {
		t.Fatalf("Restore = %d, %v; want the stored copy downloaded", n, err)
	}
	if got := read(t, dir, "ckpt.txt"); got != "v2, stored since" {
		t.Errorf("ckpt.txt = %q", got)
	}
	if n, err := s.Push(context.Background()); err != nil || n != 0 {
		t.Errorf("Push = %d, %v; want nothing to send", n, err)
	}
}

func TestARestartWithNothingNewDownloadsAndSendsNothing(t *testing.T) {
	f := newFakeKrater(t)
	dir := firstRun(t, f, map[string]string{"a.pt": "aaa", "sub/b.pt": "bb"})

	s := checkpointSyncer(f, dir)
	if n, err := s.Restore(context.Background()); err != nil || n != 0 {
		t.Fatalf("Restore = %d, %v", n, err)
	}
	if n, err := s.Push(context.Background()); err != nil || n != 0 {
		t.Errorf("Push = %d, %v; want nothing to send", n, err)
	}
	for key := range f.objects {
		if strings.Contains(key, stampFileName) {
			t.Errorf("the runner's stamp file was uploaded: %s", key)
		}
	}
}

func TestARestartedJobUploadsWhatTheLastRunLeftAtOnce(t *testing.T) {
	// Seen on staging (2026-11-19): Vast outbid the machine three times in 15 minutes. Each run uploaded its first
	// checkpoints only after a full sync interval, which no run lasted, so storage never had any.
	f := newFakeKrater(t)
	f.resumed = true
	f.objects["checkpoints/step-40.pt"] = []byte("weights-40")
	cfg := f.cfg(t)
	if err := os.MkdirAll(cfg.CheckpointDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// What the last run on this machine wrote and never uploaded.
	writeLater(t, cfg.CheckpointDir, "step-50.pt", "weights-50")
	sentinel := filepath.Join(t.TempDir(), "uploaded")
	f.onPut = func(key string) {
		if key == "checkpoints/step-50.pt" {
			_ = os.WriteFile(sentinel, nil, 0o644)
		}
	}
	// The sync interval is the fake's hour: only an upload at the start gets it there while the command runs.
	f.command = `for i in $(seq 100); do test -f "` + sentinel + `" && exit 0; sleep 0.1; done; exit 9`

	code, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, noGPU)

	if err != nil || code != 0 {
		t.Fatalf("Run = %d, %v; want 0 (step-50.pt uploaded while the command ran)", code, err)
	}
	if got := read(t, cfg.CheckpointDir, "step-40.pt"); got != "weights-40" {
		t.Errorf("step-40.pt = %q", got)
	}
}
