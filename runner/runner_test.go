package main

import (
	"bytes"
	"context"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeKrater serves the runner API and a presigned-URL "storage" from one test server.
type fakeKrater struct {
	t         *testing.T
	server    *httptest.Server
	token     string
	command   string
	resumed   bool
	partBytes int64
	stopAfter int // heartbeats before answering stop (0 = never)
	kind      string
	sshKeys   []string
	syncAt    int // answer sync on this heartbeat (0 = never)
	secrets   map[string]string
	refuse    string
	synced    int
	// Storage answers 503 to this many PUTs before working, to stand in for a transient storage outage.
	storageFails int

	mu         sync.Mutex
	objects    map[string][]byte // "area/name" -> content
	multipart  map[string]map[int][]byte
	heartbeats []map[string]any
	exitCodes  []int
	starts     int
}

func newFakeKrater(t *testing.T) *fakeKrater {
	f := &fakeKrater{t: t, token: "tok", partBytes: 64 << 20, objects: map[string][]byte{}, multipart: map[string]map[int][]byte{}}
	f.server = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.server.Close)
	return f
}

func (f *fakeKrater) cfg(t *testing.T) Config {
	dir := t.TempDir()
	return Config{
		RunnerURL:     f.server.URL,
		Token:         f.token,
		CheckpointDir: filepath.Join(dir, "checkpoints"),
		OutputDir:     filepath.Join(dir, "outputs"),
		LogDir:        filepath.Join(dir, "logs"),
	}
}

func (f *fakeKrater) writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func (f *fakeKrater) handle(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	path := r.URL.Path
	if strings.HasPrefix(path, "/storage/") {
		f.storage(w, r, strings.TrimPrefix(path, "/storage/"))
		return
	}
	if r.Header.Get("Authorization") != "Bearer "+f.token {
		w.WriteHeader(http.StatusForbidden)
		f.writeJSON(w, map[string]string{"error": "bad token"})
		return
	}
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	switch {
	case path == "/runner/v1/start":
		f.starts++
		kind := f.kind
		if kind == "" {
			kind = "job"
		}
		f.writeJSON(w, map[string]any{"job_id": "j1", "attempt": 1, "resumed": f.resumed, "command": f.command,
			"kind": kind, "ssh_keys": f.sshKeys, "heartbeat_seconds": 1, "checkpoint_sync_seconds": 3600,
			"secrets": f.secrets})
	case path == "/runner/v1/heartbeat":
		f.heartbeats = append(f.heartbeats, body)
		f.writeJSON(w, map[string]any{
			"stop": f.stopAfter > 0 && len(f.heartbeats) >= f.stopAfter,
			"sync": f.syncAt > 0 && len(f.heartbeats) == f.syncAt,
		})
	case path == "/runner/v1/synced":
		f.synced++
		f.writeJSON(w, map[string]any{"ok": true})
	case path == "/runner/v1/exit":
		f.exitCodes = append(f.exitCodes, int(body["exit_code"].(float64)))
		f.writeJSON(w, map[string]any{"state": "done"})
	case strings.HasSuffix(path, "/list"):
		area := strings.Split(path, "/")[4]
		files := []map[string]any{}
		for key, data := range f.objects {
			if strings.HasPrefix(key, area+"/") {
				name := strings.TrimPrefix(key, area+"/")
				files = append(files, map[string]any{"name": name, "size": len(data), "url": f.server.URL + "/storage/" + key})
			}
		}
		f.writeJSON(w, map[string]any{"files": files})
	case strings.HasSuffix(path, "/upload"):
		area := strings.Split(path, "/")[4]
		name := body["name"].(string)
		if name == f.refuse {
			w.WriteHeader(http.StatusUnprocessableEntity)
			f.writeJSON(w, map[string]any{"error": "quota"})
			return
		}
		size := int64(body["size"].(float64))
		key := area + "/" + name
		if size <= f.partBytes {
			f.writeJSON(w, map[string]any{"name": name, "url": f.server.URL + "/storage/" + key})
			return
		}
		uploadID := fmt.Sprintf("u%d", len(f.multipart)+1)
		f.multipart[uploadID] = map[int][]byte{}
		parts := []string{}
		for n := int64(1); (n-1)*f.partBytes < size; n++ {
			parts = append(parts, fmt.Sprintf("%s/storage/part/%s/%d", f.server.URL, uploadID, n))
		}
		f.writeJSON(w, map[string]any{"name": name, "upload_id": uploadID, "part_bytes": f.partBytes, "parts": parts})
	case strings.HasSuffix(path, "/complete"):
		area := strings.Split(path, "/")[4]
		uploadID := body["upload_id"].(string)
		parts := f.multipart[uploadID]
		var numbers []int
		for n := range parts {
			numbers = append(numbers, n)
		}
		sort.Ints(numbers)
		etags := body["etags"].([]any)
		var data []byte
		for i, n := range numbers {
			sum := md5.Sum(parts[n])
			if etags[i].(string) != `"`+hex.EncodeToString(sum[:])+`"` {
				f.t.Errorf("part %d ETag mismatch", n)
			}
			data = append(data, parts[n]...)
		}
		f.objects[area+"/"+body["name"].(string)] = data
		delete(f.multipart, uploadID)
		f.writeJSON(w, map[string]any{"ok": true})
	default:
		http.NotFound(w, r)
	}
}

func (f *fakeKrater) storage(w http.ResponseWriter, r *http.Request, key string) {
	if r.Header.Get("Authorization") != "" {
		f.t.Errorf("storage request carried an Authorization header")
	}
	switch r.Method {
	case http.MethodGet:
		data, ok := f.objects[key]
		if !ok {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write(data)
	case http.MethodPut:
		data, _ := io.ReadAll(r.Body)
		if f.storageFails > 0 {
			f.storageFails--
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		if strings.HasPrefix(key, "part/") {
			bits := strings.Split(key, "/")
			var n int
			fmt.Sscan(bits[2], &n)
			f.multipart[bits[1]][n] = data
		} else {
			f.objects[key] = data
		}
		sum := md5.Sum(data)
		w.Header().Set("ETag", `"`+hex.EncodeToString(sum[:])+`"`)
	}
}

func noGPU() (float64, bool) { return 0, false }

func TestRunReportsTheExitCodeAndUploadsCheckpointsAndOutputs(t *testing.T) {
	f := newFakeKrater(t)
	f.command = `echo step > "$KRATER_CHECKPOINT_DIR/ckpt.txt"; mkdir -p "$KRATER_OUTPUT_DIR/eval"; ` +
		`echo 0.93 > "$KRATER_OUTPUT_DIR/eval/score.txt"; test "$KRATER_RESUMED" = 0; exit 3`
	cfg := f.cfg(t)

	code, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, noGPU)

	if err != nil || code != 3 {
		t.Fatalf("Run = %d, %v; want 3, nil", code, err)
	}
	if fmt.Sprint(f.exitCodes) != "[3]" {
		t.Errorf("exit codes reported: %v", f.exitCodes)
	}
	if string(f.objects["checkpoints/ckpt.txt"]) != "step\n" || string(f.objects["outputs/eval/score.txt"]) != "0.93\n" {
		t.Errorf("objects: %v", keys(f.objects))
	}
}

func TestRunRestoresCheckpointsAndSaysItResumed(t *testing.T) {
	f := newFakeKrater(t)
	f.resumed = true
	f.objects["checkpoints/model/step-100.pt"] = []byte("weights")
	f.command = `test "$KRATER_RESUMED" = 1 && test "$(cat "$KRATER_CHECKPOINT_DIR/model/step-100.pt")" = weights`
	cfg := f.cfg(t)

	code, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, noGPU)

	if err != nil || code != 0 {
		t.Fatalf("Run = %d, %v; want 0, nil", code, err)
	}
}

func TestBigFilesGoUpInParts(t *testing.T) {
	f := newFakeKrater(t)
	f.partBytes = 10
	cfg := f.cfg(t)
	os.MkdirAll(cfg.CheckpointDir, 0o755)
	content := strings.Repeat("0123456789", 3) + "xyz" // 33 bytes: 4 parts
	os.WriteFile(filepath.Join(cfg.CheckpointDir, "big.bin"), []byte(content), 0o644)
	os.WriteFile(filepath.Join(cfg.CheckpointDir, "half-written.pt.tmp"), []byte("x"), 0o644)
	api := &API{BaseURL: f.server.URL, Token: f.token, HTTP: http.DefaultClient}
	s := NewSyncer(api, "checkpoints", cfg.CheckpointDir, http.DefaultClient)

	n, err := s.Push(context.Background())

	if err != nil || n != 1 {
		t.Fatalf("Push = %d, %v; want 1, nil", n, err)
	}
	if string(f.objects["checkpoints/big.bin"]) != content {
		t.Errorf("stored %q", f.objects["checkpoints/big.bin"])
	}
	// Unchanged files aren't uploaded again.
	if n, _ := s.Push(context.Background()); n != 0 {
		t.Errorf("second push uploaded %d files", n)
	}
}

func TestKraterCanStopTheJob(t *testing.T) {
	f := newFakeKrater(t)
	f.stopAfter = 2
	f.command = "sleep 60"
	cfg := f.cfg(t)
	start := time.Now()

	code, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, func() (float64, bool) { return 87, true })

	if err != nil {
		t.Fatal(err)
	}
	if code != 128+15 {
		t.Errorf("exit code %d, want SIGTERM's 143", code)
	}
	if time.Since(start) > 20*time.Second {
		t.Errorf("stopping took %s", time.Since(start))
	}
	if f.heartbeats[0]["gpu_util"] != 87.0 {
		t.Errorf("heartbeat: %v", f.heartbeats[0])
	}
}

func TestADeniedTokenGivesUpAtOnce(t *testing.T) {
	f := newFakeKrater(t)
	cfg := f.cfg(t)
	cfg.Token = "wrong"

	_, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, noGPU)

	if err == nil || !strings.Contains(err.Error(), "refused") {
		t.Fatalf("err = %v", err)
	}
}

func TestStoredNamesCannotEscapeTheDirectory(t *testing.T) {
	s := NewSyncer(nil, "checkpoints", "/krater/checkpoints", nil)
	for _, name := range []string{"../x", "a/../../x", "/etc/passwd", ".."} {
		if _, err := s.localPath(name); err == nil {
			t.Errorf("%q was allowed", name)
		}
	}
	if p, err := s.localPath("a/b.pt"); err != nil || p != filepath.Join("/krater/checkpoints", "a", "b.pt") {
		t.Errorf("a/b.pt -> %q, %v", p, err)
	}
}

func TestMeanOfNvidiaSMILines(t *testing.T) {
	if v, ok := meanOfLines("90\n70\n"); !ok || v != 80 {
		t.Errorf("got %v %v", v, ok)
	}
	if _, ok := meanOfLines("[N/A]\n"); ok {
		t.Error("N/A parsed")
	}
}

func keys(m map[string][]byte) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func TestABoxInstallsKeysCountsSessionsAndSavesItsWorkspaceWhenAsked(t *testing.T) {
	f := newFakeKrater(t)
	f.kind = "box"
	f.sshKeys = []string{"ssh-ed25519 AAAAC3Nza", "evil\nssh-rsa injected", "ssh-ed25519 AAAA with-comment"}
	f.syncAt = 2
	f.objects["workspace/notes.txt"] = []byte("old notes")
	cfg := f.cfg(t)
	cfg.Home = t.TempDir()
	cfg.WorkspaceDir = t.TempDir()
	cfg.ProcNet = t.TempDir()
	// One established SSH session (local port 22 = 0x0016, state 01) and one listening socket.
	os.WriteFile(filepath.Join(cfg.ProcNet, "tcp"), []byte(
		"  sl  local_address rem_address   st\n"+
			"   0: 00000000:0016 00000000:0000 0A\n"+
			"   1: 0100007F:0016 0200007F:D431 01\n"), 0o644)
	ctx, cancel := context.WithTimeout(context.Background(), 3500*time.Millisecond)
	defer cancel()
	go func() {
		// The member edits a file while the box runs.
		time.Sleep(500 * time.Millisecond)
		os.WriteFile(filepath.Join(cfg.WorkspaceDir, "notes.txt"), []byte("new notes"), 0o644)
	}()

	code, err := Run(ctx, cfg, http.DefaultClient, http.DefaultClient, noGPU)

	if err != nil || code != 0 {
		t.Fatalf("Run = %d, %v", code, err)
	}
	keys, _ := os.ReadFile(filepath.Join(cfg.Home, ".ssh", "authorized_keys"))
	if string(keys) != "ssh-ed25519 AAAAC3Nza\n" {
		t.Errorf("authorized_keys = %q", keys)
	}
	info, _ := os.Stat(filepath.Join(cfg.Home, ".ssh", "authorized_keys"))
	if info.Mode().Perm() != 0o600 {
		t.Errorf("authorized_keys mode %v", info.Mode().Perm())
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.heartbeats[0]["sessions"] != 1.0 {
		t.Errorf("first heartbeat %v", f.heartbeats[0])
	}
	if f.synced != 1 || string(f.objects["workspace/notes.txt"]) != "new notes" {
		t.Errorf("synced %d, workspace %q", f.synced, f.objects["workspace/notes.txt"])
	}
}

func TestOneRefusedFileDoesNotStopTheOthers(t *testing.T) {
	f := newFakeKrater(t)
	cfg := f.cfg(t)
	os.MkdirAll(cfg.CheckpointDir, 0o755)
	os.WriteFile(filepath.Join(cfg.CheckpointDir, "a.pt"), []byte("a"), 0o644)
	os.WriteFile(filepath.Join(cfg.CheckpointDir, "b.pt"), []byte("b"), 0o644)
	api := &API{BaseURL: f.server.URL, Token: f.token, HTTP: http.DefaultClient}
	s := NewSyncer(api, "checkpoints", cfg.CheckpointDir, http.DefaultClient)
	f.refuse = "a.pt"

	n, err := s.Push(context.Background())

	if n != 1 || err == nil || string(f.objects["checkpoints/b.pt"]) != "b" {
		t.Errorf("Push = %d, %v; objects %v", n, err, keys(f.objects))
	}
}

func TestAJobGetsTheProjectSecretsInItsEnvironmentAndTheLogNeverShowsThem(t *testing.T) {
	f := newFakeKrater(t)
	f.secrets = map[string]string{"HF_TOKEN": "hf_s3cret", "KRATER_JOB_TOKEN": "spoofed", "bad-name": "x"}
	f.command = `test "$HF_TOKEN" = hf_s3cret && test "$KRATER_JOB_TOKEN" != spoofed && test -z "$(env | grep bad-name)"`
	var logged strings.Builder
	log.SetOutput(&logged)
	defer log.SetOutput(os.Stderr)

	code, err := Run(context.Background(), f.cfg(t), http.DefaultClient, http.DefaultClient, noGPU)

	if err != nil || code != 0 {
		t.Fatalf("Run = %d, %v; want 0: the command didn't see the secret as expected", code, err)
	}
	if strings.Contains(logged.String(), "hf_s3cret") {
		t.Errorf("a secret value reached the runner's log")
	}
	if !strings.Contains(logged.String(), "1 project secrets") {
		t.Errorf("log: %s", logged.String())
	}
}

func TestABoxGetsAShellProfileWithItsSecrets(t *testing.T) {
	path := filepath.Join(t.TempDir(), "profile.d", "krater-secrets.sh")
	value := `it's "quoted" $HOME`

	if err := writeSecretsProfile(path, map[string]string{"HF_TOKEN": value}); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("profile: %v, mode %v", err, info.Mode())
	}
	out, err := exec.Command("sh", "-c", `. "$1"; printf %s "$HF_TOKEN"`, "sh", path).Output()
	if err != nil || string(out) != value {
		t.Errorf("sourced value = %q, %v; want %q", out, err, value)
	}
	// No secrets left (one was deleted): the file goes.
	if err := writeSecretsProfile(path, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("the profile is still there: %v", err)
	}
}

func TestANonRootImageGetsItsDirectoriesUnderTmp(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can create anything; run as a non-root user to check the fallback")
	}
	locked := filepath.Join(t.TempDir(), "locked")
	os.Mkdir(locked, 0o500)
	base := t.TempDir()

	dir, err := usableDir(filepath.Join(locked, "checkpoints"), base)

	if err != nil || dir != filepath.Join(base, "krater", "checkpoints") {
		t.Fatalf("usableDir = %q, %v", dir, err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Errorf("not created: %v", err)
	}
}

func TestAJobsOutputIsSavedForItsTeam(t *testing.T) {
	f := newFakeKrater(t)
	f.command = `echo to stdout; echo to stderr >&2; exit 4`

	code, err := Run(context.Background(), f.cfg(t), http.DefaultClient, http.DefaultClient, noGPU)

	if err != nil || code != 4 {
		t.Fatalf("Run = %d, %v; want 4", code, err)
	}
	saved := string(f.objects["logs/attempt-1.log"])
	for _, want := range []string{"to stdout\n", "to stderr\n", "the command exited with code 4"} {
		if !strings.Contains(saved, want) {
			t.Errorf("the saved log lacks %q:\n%s", want, saved)
		}
	}
}

func TestTheSavedOutputStopsAtItsCap(t *testing.T) {
	dir := t.TempDir()
	out, err := newLogFile(dir, 2, 10)
	if err != nil {
		t.Fatal(err)
	}
	for _, chunk := range []string{"12345", "67890abc", "more"} {
		if n, err := out.Write([]byte(chunk)); n != len(chunk) || err != nil {
			t.Fatalf("Write(%q) = %d, %v: a full log must still accept writes", chunk, n, err)
		}
	}
	_ = out.Close()
	saved, _ := os.ReadFile(filepath.Join(dir, "attempt-2.log"))
	if !strings.HasPrefix(string(saved), "1234567890\n[krater: the output passed") || strings.Contains(string(saved), "more") {
		t.Errorf("saved %q", saved)
	}
}

// quickRetries makes the runner's retry windows short enough for a test.
func quickRetries(t *testing.T, window time.Duration) {
	oldFirst, oldBox, oldFinal := retryFirstWait, boxSaveWindow, finalPushWindow
	retryFirstWait, boxSaveWindow, finalPushWindow = 5*time.Millisecond, window, window
	t.Cleanup(func() { retryFirstWait, boxSaveWindow, finalPushWindow = oldFirst, oldBox, oldFinal })
}

// runSyncingBox runs a box that is asked to sync on its second heartbeat, with `fails` storage outages first.
func runSyncingBox(t *testing.T, f *fakeKrater, fails int) {
	f.kind = "box"
	f.syncAt = 2
	f.storageFails = fails
	cfg := f.cfg(t)
	cfg.Home, cfg.WorkspaceDir, cfg.ProcNet = t.TempDir(), t.TempDir(), t.TempDir()
	os.WriteFile(filepath.Join(cfg.WorkspaceDir, "notes.txt"), []byte("notes"), 0o644)
	ctx, cancel := context.WithTimeout(context.Background(), 3500*time.Millisecond)
	defer cancel()
	if _, err := Run(ctx, cfg, http.DefaultClient, http.DefaultClient, noGPU); err != nil {
		t.Fatal(err)
	}
}

func TestABoxDoesNotConfirmASaveThatFailed(t *testing.T) {
	quickRetries(t, 50*time.Millisecond)
	f := newFakeKrater(t)

	runSyncingBox(t, f, 1000)

	f.mu.Lock()
	defer f.mu.Unlock()
	if f.synced != 0 || len(f.objects) != 0 {
		t.Errorf("synced %d, objects %v: Krater must not be told /workspace was saved", f.synced, keys(f.objects))
	}
}

func TestABoxRetriesATransientSaveFailureBeforeConfirming(t *testing.T) {
	quickRetries(t, 5*time.Second)
	f := newFakeKrater(t)

	runSyncingBox(t, f, 2)

	f.mu.Lock()
	defer f.mu.Unlock()
	if f.synced != 1 || string(f.objects["workspace/notes.txt"]) != "notes" {
		t.Errorf("synced %d, objects %v", f.synced, keys(f.objects))
	}
}

func runFinishingJob(t *testing.T, f *fakeKrater, fails int) (int, error, string) {
	f.command = `echo 0.93 > "$KRATER_OUTPUT_DIR/score.txt"`
	f.storageFails = fails
	var logged bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&logged)
	defer log.SetOutput(previous)
	code, err := Run(context.Background(), f.cfg(t), http.DefaultClient, http.DefaultClient, noGPU)
	return code, err, logged.String()
}

func TestTheFinalUploadRetriesATransientStorageError(t *testing.T) {
	quickRetries(t, 5*time.Second)
	f := newFakeKrater(t)

	code, err, logged := runFinishingJob(t, f, 2)

	if err != nil || code != 0 || fmt.Sprint(f.exitCodes) != "[0]" {
		t.Fatalf("Run = %d, %v; exits %v", code, err, f.exitCodes)
	}
	if string(f.objects["outputs/score.txt"]) != "0.93\n" {
		t.Errorf("objects: %v", keys(f.objects))
	}
	if strings.Contains(logged, "NOT fully saved") {
		t.Errorf("warned although the upload worked:\n%s", logged)
	}
}

func TestAFinalUploadThatKeepsFailingStillReportsTheExitButSaysSo(t *testing.T) {
	quickRetries(t, 100*time.Millisecond)
	f := newFakeKrater(t)

	code, err, logged := runFinishingJob(t, f, 100000)

	if err != nil || code != 0 || fmt.Sprint(f.exitCodes) != "[0]" {
		t.Fatalf("Run = %d, %v; exits %v: the exit must still be reported", code, err, f.exitCodes)
	}
	if !strings.Contains(logged, "outputs FAILED") || !strings.Contains(logged, "NOT fully saved") {
		t.Errorf("the lost outputs weren't logged:\n%s", logged)
	}
}

// A proxy in front of Krater (a WAF challenge, Funnel) can answer with a status Krater uses itself.
func TestOnlyKratersOwnAnswersCountAsContactOrRefusal(t *testing.T) {
	const html = "<html><body>Just a moment...</body></html>"
	cases := []struct {
		name    string
		status  int
		body    string
		touched bool
		denied  bool
		ok      bool
	}{
		{"success", 200, `{"ok": true}`, true, false, true},
		{"refusal", 403, `{"error": "gone"}`, true, true, false},
		{"JSON 4xx without a refusal", 422, `{"error": "size must be an integer"}`, true, false, false},
		{"403 challenge page", 403, html, false, false, false},
		{"429 challenge page", 429, html, false, false, false},
		{"403 with a JSON array", 403, `["no"]`, false, false, false},
		{"403 with JSON but no error", 403, `{"detail": "x"}`, true, false, false},
		{"gateway error", 502, html, false, false, false},
		{"JSON 5xx", 503, `{"error": "down"}`, false, false, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(c.status)
				io.WriteString(w, c.body)
			}))
			defer server.Close()
			start := time.Now().Add(-time.Hour)
			api := &API{BaseURL: server.URL, Token: "t", HTTP: http.DefaultClient, Contact: NewContact(start)}

			err := api.post(context.Background(), "/synced", nil, nil)

			var denied *ErrDenied
			if got := errors.As(err, &denied); got != c.denied {
				t.Errorf("denied = %v, want %v (err %v)", got, c.denied, err)
			}
			if (err == nil) != c.ok {
				t.Errorf("err = %v", err)
			}
			if touched := api.Contact.Last().After(start); touched != c.touched {
				t.Errorf("touched = %v, want %v", touched, c.touched)
			}
		})
	}
}
