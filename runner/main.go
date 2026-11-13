// Command krater-runner runs a Krater Compute job on a rented Vast machine.
//
// Krater's bootstrap script downloads it, checks its sha256 and starts it. It then:
//
//  1. tells Krater it started (POST /runner/v1/start) and learns the command and whether this is a resume;
//  2. restores the job's checkpoints into $KRATER_CHECKPOINT_DIR;
//  3. runs the member's command with `sh -c`;
//  4. sends a heartbeat every minute with the GPU use it samples from nvidia-smi and the container's network use
//     (netcount.go), and stops the command when Krater says so;
//  5. uploads changed checkpoints on a timer, and checkpoints and outputs when the command exits; the command's
//     output (and the runner's own lines) go to the job's log, uploaded every minute, which its team reads in Krater;
//  6. reports the exit code. Krater then destroys the machine.
//
// The runner is not the container's main process: the bootstrap shell is, and it waits forever after the runner
// returns, because Vast restarts a container whose main process exits (and keeps billing). Only Krater ends a machine,
// except that after hours without an answer from Krater the runner stops it (selfstop.go).
//
// It holds one secret, the instance's runner token ($KRATER_JOB_TOKEN), which works for this instance only. Storage
// is reached only through presigned URLs Krater hands out for this job.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

var version = "dev"

func logf(format string, args ...any) { log.Printf("krater-runner: "+format, args...) }

// Config is what the runner reads from its environment.
type Config struct {
	RunnerURL     string
	Token         string
	CheckpointDir string
	OutputDir     string
	InputDir      string
	WorkspaceDir  string
	Home          string
	ProcNet       string
	// Where a box's login shells read the project's secrets from.
	SecretsProfile string
	// Where the job's output log is kept before upload; blank means a krater-logs folder in the temp directory.
	LogDir string
	// The dead-man switch main runs beside the job (selfstop.go); nil in tests that don't need it.
	SelfStop *SelfStop
}

func configFromEnv() (Config, error) {
	cfg := Config{
		RunnerURL:      os.Getenv("KRATER_RUNNER_URL"),
		Token:          os.Getenv("KRATER_JOB_TOKEN"),
		CheckpointDir:  envOr("KRATER_CHECKPOINT_DIR", "/krater/checkpoints"),
		OutputDir:      envOr("KRATER_OUTPUT_DIR", "/krater/outputs"),
		InputDir:       envOr("KRATER_INPUT_DIR", "/krater/inputs"),
		WorkspaceDir:   envOr("KRATER_WORKSPACE_DIR", "/workspace"),
		Home:           envOr("HOME", "/root"),
		ProcNet:        "/proc/net",
		SecretsProfile: "/etc/profile.d/krater-secrets.sh",
	}
	if cfg.RunnerURL == "" || cfg.Token == "" {
		return cfg, errors.New("KRATER_RUNNER_URL and KRATER_JOB_TOKEN must be set")
	}
	return cfg, nil
}

// SetupFailed is the exit code the runner reports when it can't prepare the machine (it couldn't make its
// directories), so Krater ends the job at once instead of waiting for a heartbeat deadline.
const SetupFailed = 125

// usableDir makes `dir` and returns it or, when the image's user may not create it (a non-root USER can't make
// /krater), the same name under `fallbackBase`.
func usableDir(dir, fallbackBase string) (string, error) {
	err := os.MkdirAll(dir, 0o755)
	if err == nil {
		return dir, nil
	}
	if !errors.Is(err, fs.ErrPermission) {
		return "", err
	}
	alt := filepath.Join(fallbackBase, "krater", filepath.Base(dir))
	if err := os.MkdirAll(alt, 0o755); err != nil {
		return "", fmt.Errorf("can't create %s or %s: %w", dir, alt, err)
	}
	logf("this user can't create %s; using %s", dir, alt)
	return alt, nil
}

func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	logf("version %s", version)
	cfg, err := configFromEnv()
	if err != nil {
		logf("%v", err)
		return
	}
	cfg.SelfStop = NewSelfStop(&http.Client{Timeout: 60 * time.Second})
	if dir, err := usableDir(envOr("KRATER_STATE_DIR", "/krater/state"), os.TempDir()); err != nil {
		logf("nowhere to keep the self-stop settings: %v", err)
	} else {
		cfg.SelfStop.StatePath = filepath.Join(dir, "selfstop.json")
		if cfg.SelfStop.Load() {
			// The container restarted on the same machine: until /start answers, stop as Krater last said.
			logf("restarted: using the self-stop settings Krater gave the last run")
		}
	}
	go cfg.SelfStop.Run(context.Background(), time.Minute)
	net := &NetCounter{Path: "/proc/net/dev", NSPath: "/proc/self/ns/net"}
	if cfg.SelfStop.StatePath != "" {
		net.StatePath = filepath.Join(filepath.Dir(cfg.SelfStop.StatePath), "net.json")
		net.Load()
	}
	netSample = net.Sample
	code, err := Run(context.Background(), cfg, &http.Client{Timeout: 60 * time.Second}, storageClient(), nvidiaSMI)
	if err != nil {
		logf("%v", err)
	}
	if cfg.SelfStop.Fired() {
		logf("done; the machine is stopping itself")
	} else {
		logf("done (exit code %d); Krater stops this machine", code)
	}
	// Stay up for the dead-man switch: if Krater never comes to end this machine, it stops itself.
	select {}
}

// Run does the whole job and returns the command's exit code. `api` and `storage` are separate clients: storage
// requests can take far longer than API calls.
// storageClient is the HTTP client for presigned storage URLs: Go's default transport, keeping as many idle
// connections to storage as files go up or down at once (the default keeps 2, so most transfers would open a new
// TLS connection).
func storageClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConnsPerHost = transferWorkers
	return &http.Client{Transport: transport}
}

// whileRestoring runs fn, sending a heartbeat every `every` meanwhile: restoring thousands of files can take longer
// than Krater waits for a heartbeat (heartbeat_timeout_minutes), and it would take the runner for lost.
func whileRestoring(ctx context.Context, api *API, every time.Duration, fn func()) {
	done := make(chan struct{})
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		ticker := time.NewTicker(every)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
				if _, err := api.HeartbeatReply(ctx, map[string]any{"phase": "restoring"}); err != nil {
					logf("heartbeat while restoring failed: %v", err)
				}
			}
		}
	}()
	fn()
	close(done)
	<-stopped
}

func Run(ctx context.Context, cfg Config, apiHTTP, storageHTTP *http.Client, gpu func() (float64, bool)) (int, error) {
	api := &API{BaseURL: cfg.RunnerURL, Token: cfg.Token, HTTP: apiHTTP}
	if cfg.SelfStop != nil {
		api.Contact = cfg.SelfStop.Contact
	}
	var info *StartInfo
	err := retry(ctx, 10*time.Minute, func() error {
		var err error
		info, err = api.Start(ctx)
		return err
	})
	if err != nil {
		return -1, fmt.Errorf("could not start: %w", err)
	}
	logf("job %s, attempt %d, resumed=%v", info.JobID, info.Attempt, info.Resumed)
	cfg.SelfStop.Configure(info)

	for _, dir := range []*string{&cfg.CheckpointDir, &cfg.OutputDir} {
		usable, err := usableDir(*dir, os.TempDir())
		if err != nil {
			_ = api.Exit(ctx, SetupFailed)
			return SetupFailed, err
		}
		*dir = usable
	}
	checkpoints := NewSyncer(api, "checkpoints", cfg.CheckpointDir, storageHTTP)
	outputs := NewSyncer(api, "outputs", cfg.OutputDir, storageHTTP)
	beatEvery := seconds(info.HeartbeatSeconds, 60)
	whileRestoring(ctx, api, beatEvery, func() {
		if n, err := checkpoints.Restore(ctx); err != nil {
			logf("restoring checkpoints failed: %v", err)
		} else if n > 0 {
			logf("restored %d checkpoint files", n)
		}
	})
	if info.Inputs {
		// The job asked for project inputs: without them it would run (and bill) for nothing, so it fails instead.
		var code int
		var err error
		whileRestoring(ctx, api, beatEvery, func() { code, err = restoreInputs(ctx, api, &cfg, storageHTTP) })
		if err != nil {
			return code, err
		}
	}

	if info.Kind == "box" {
		if cfg.SecretsProfile != "" {
			if err := writeSecretsProfile(cfg.SecretsProfile, info.Secrets); err != nil {
				logf("could not write the project's secrets for login shells: %v", err)
			} else if n := len(secretEnv(info.Secrets)); n > 0 {
				logf("%d project secrets for login shells", n)
			}
		}
		if err := writeAuthorizedKeys(cfg.Home, info.SSHKeys); err != nil {
			logf("could not write authorized_keys: %v", err)
		} else {
			logf("installed %d SSH keys", len(info.SSHKeys))
		}
		workspaceDir, err := usableDir(cfg.WorkspaceDir, os.TempDir())
		if err != nil {
			_ = api.Exit(ctx, SetupFailed)
			return SetupFailed, err
		}
		cfg.WorkspaceDir = workspaceDir
		workspace := NewSyncer(api, "workspace", cfg.WorkspaceDir, storageHTTP)
		workspace.Manifest = true
		whileRestoring(ctx, api, beatEvery, func() {
			if n, err := workspace.Restore(ctx); err != nil {
				logf("restoring /workspace failed: %v", err)
			} else if n > 0 {
				logf("restored %d workspace files", n)
			}
		})
		runBox(ctx, api, info, workspace, gpu, cfg.ProcNet, cfg.Home)
		return 0, nil
	}
	if info.Command == "" {
		heartbeatLoop(ctx, api, seconds(info.HeartbeatSeconds, 60), gpu, nil, nil)
		return 0, nil
	}

	cmd := exec.Command("sh", "-c", info.Command)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	logDir := cfg.LogDir
	if logDir == "" {
		logDir = filepath.Join(os.TempDir(), "krater-logs")
	}
	var logs *Syncer
	if out, err := newLogFile(logDir, info.Attempt, LogMaxBytes); err != nil {
		logf("can't save the command's output for Krater: %v", err)
	} else {
		defer out.Close()
		previous := log.Writer()
		log.SetOutput(io.MultiWriter(previous, out))
		defer log.SetOutput(previous)
		cmd.Stdout, cmd.Stderr = io.MultiWriter(os.Stdout, out), io.MultiWriter(os.Stderr, out)
		// With pipes, Wait also waits for the output to close: don't hang on a background process that keeps it.
		cmd.WaitDelay = 10 * time.Second
		logs = NewSyncer(api, "logs", logDir, storageHTTP)
	}
	secrets := secretEnv(info.Secrets)
	if len(secrets) > 0 {
		logf("%d project secrets in the command's environment", len(secrets))
	}
	cmd.Env = append(append(os.Environ(), secrets...),
		"KRATER_RESUMED="+boolEnv(info.Resumed),
		"KRATER_CHECKPOINT_DIR="+cfg.CheckpointDir,
		"KRATER_OUTPUT_DIR="+cfg.OutputDir,
		"KRATER_INPUT_DIR="+cfg.InputDir,
	)
	if info.SmokeSeconds > 0 {
		cmd.Env = append(cmd.Env, "KRATER_SMOKE=1")
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		_ = api.Exit(ctx, 127)
		return 127, fmt.Errorf("could not start the command: %w", err)
	}

	done := make(chan struct{})
	var stopOnce sync.Once
	// Set when the smoke test's timer was what stopped the command: its exit code is then the stop's, not a failure.
	var smokeStopped atomic.Bool
	stop := func(why string) {
		stopOnce.Do(func() {
			if why == smokeStopWhy {
				smokeStopped.Store(true)
			}
			logf("%s", why)
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
			go func() {
				select {
				case <-done:
				case <-time.After(30 * time.Second):
					_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
				}
			}()
		})
	}
	// Stopping itself, the machine first stops the command and gives it a moment to save a last checkpoint.
	cfg.SelfStop.Before(func() {
		stop("stopping the command: this machine is stopping itself")
		select {
		case <-done:
		case <-time.After(45 * time.Second):
		}
	})
	if info.SmokeSeconds > 0 {
		// A smoke test: the command's first minutes on the real machine are the test. The timer starts with the
		// command, after the pull and the restores, so they don't eat into it.
		logf("smoke test: the command is stopped after %d seconds if it hasn't ended by then", info.SmokeSeconds)
		go func() {
			select {
			case <-done:
			case <-time.After(time.Duration(info.SmokeSeconds) * time.Second):
				stop(smokeStopWhy)
			}
		}()
	}
	loops, cancelLoops := context.WithCancel(ctx)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		heartbeatLoop(loops, api, seconds(info.HeartbeatSeconds, 60), gpu,
			func() { stop("Krater asked to stop the job") },
			func() { saveCheckpoints(loops, api, checkpoints) })
	}()
	go func() {
		defer wg.Done()
		syncLoop(loops, checkpoints, seconds(info.CheckpointSyncSeconds, 600))
	}()
	if logs != nil {
		wg.Add(1)
		go func() { defer wg.Done(); syncLoop(loops, logs, seconds(info.LogSyncSeconds, 60)) }()
	}

	code := exitCode(cmd.Wait())
	close(done)
	cancelLoops()
	wg.Wait()
	if cfg.SelfStop.Fired() {
		// Not the job's own end: report nothing, or Krater would finish the job when it's back. The machine stops,
		// and Krater starts it again; the command then resumes from its checkpoints.
		logf("the command stopped (code %d) because this machine is stopping itself", code)
		return code, nil
	}
	if smokeStopped.Load() {
		logf("smoke test passed: the command ran its time without failing (it stopped with code %d)", code)
	} else {
		logf("the command exited with code %d", code)
	}

	// A transient storage error here would lose the job's outputs while it shows as succeeded, so retry for a while.
	// Past the window the machine still reports the exit (it mustn't bill forever), but says loudly what is missing;
	// the log goes last so that line reaches the job's log too. The /exit wire format has no field for it.
	end := time.Now().Add(finalPushWindow)
	var lost []string
	for _, s := range []*Syncer{checkpoints, outputs} {
		n := 0
		err := retry(ctx, time.Until(end), func() error {
			pushed, err := s.Push(ctx)
			n += pushed
			return err
		})
		if err != nil {
			logf("final upload of %s FAILED after retrying: %v", s.Area, err)
			lost = append(lost, s.Area)
		} else if n > 0 {
			logf("uploaded %d %s files", n, s.Area)
		}
	}
	if len(lost) > 0 {
		logf("WARNING: the %s were NOT fully saved; the job will show as finished but those files are missing from Krater",
			strings.Join(lost, " and "))
	}
	if logs != nil {
		if err := retry(ctx, time.Until(end), func() error { _, err := logs.Push(ctx); return err }); err != nil {
			logf("final upload of the output log failed: %v", err)
		}
	}
	report := func() error { return api.Exit(ctx, code) }
	if smokeStopped.Load() {
		report = func() error { return api.ExitSmokeStopped(ctx, code) }
	}
	if err := retry(ctx, 10*time.Minute, report); err != nil {
		return code, fmt.Errorf("could not report the exit code: %w", err)
	}
	return code, nil
}

// smokeStopWhy is the runner's log line when a smoke test's timer stops the command (and how `stop` knows it was).
const smokeStopWhy = "smoke test: the command ran its time without failing; stopping it"

// finalPushWindow bounds the retries of the final upload after the command exits.
var finalPushWindow = 5 * time.Minute

// saveCheckpoints uploads the checkpoints now and confirms it: Krater asks before stopping all compute (a regular
// stop), then destroys the machine. Confirm only a save that happened; on failure Krater keeps asking until its
// deadline, and then stops the machine anyway.
func saveCheckpoints(ctx context.Context, api *API, checkpoints *Syncer) {
	err := retry(ctx, boxSaveWindow, func() error {
		_, err := checkpoints.Push(ctx)
		return err
	})
	if err != nil {
		logf("saving the checkpoints failed, NOT confirming the save to Krater: %v", err)
		return
	}
	if err := api.Synced(ctx); err != nil {
		logf("could not confirm the save: %v", err)
		return
	}
	logf("saved the checkpoints; Krater is stopping this machine")
}

// heartbeatLoop reports the machine's state every `every`. `stop` runs when Krater says the job has ended, and `save`
// when it asks for the checkpoints before stopping the machine; either may be nil.
func heartbeatLoop(ctx context.Context, api *API, every time.Duration, gpu func() (float64, bool), stop, save func()) {
	sampler := newGPUSampler(gpu)
	sampler.take() // so the first heartbeat already carries a sample
	go sampler.run(ctx, 15*time.Second)
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		beat := map[string]any{"phase": "running"}
		if util, ok := sampler.average(); ok {
			beat["gpu_util"] = util
		}
		addNetUse(beat)
		reply, err := api.HeartbeatReply(ctx, beat)
		shouldStop := err == nil && reply.Stop
		var denied *ErrDenied
		switch {
		case errors.As(err, &denied):
			shouldStop = true
		case err != nil && ctx.Err() == nil:
			logf("heartbeat failed: %v", err)
		}
		if err == nil && reply.Sync && !shouldStop && save != nil {
			save()
		}
		if shouldStop {
			if stop != nil {
				stop()
			} else {
				return
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func syncLoop(ctx context.Context, s *Syncer, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			n, err := s.Push(ctx)
			switch {
			case err != nil && ctx.Err() == nil:
				logf("%s upload failed: %v", s.Area, err)
			case n > 0 && s.Area != "logs": // a line about the log would change the log, and upload it again
				logf("uploaded %d %s files", n, s.Area)
			}
		}
	}
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		if status, ok := exitErr.Sys().(syscall.WaitStatus); ok && status.Signaled() {
			return 128 + int(status.Signal())
		}
		return exitErr.ExitCode()
	}
	return -1
}

func boolEnv(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

func seconds(n, fallback int) time.Duration {
	if n <= 0 {
		n = fallback
	}
	return time.Duration(n) * time.Second
}

// restoreInputs copies the project inputs the job asked for into cfg.InputDir. On failure it tells Krater the job
// couldn't start and returns SetupFailed.
func restoreInputs(ctx context.Context, api *API, cfg *Config, storage *http.Client) (int, error) {
	dir, err := usableDir(cfg.InputDir, os.TempDir())
	if err == nil {
		cfg.InputDir = dir
		var n int
		n, err = NewSyncer(api, "inputs", cfg.InputDir, storage).Restore(ctx)
		if err == nil {
			logf("copied %d input files into %s", n, cfg.InputDir)
			return 0, nil
		}
	}
	logf("copying the job's inputs failed: %v", err)
	_ = api.Exit(ctx, SetupFailed)
	return SetupFailed, fmt.Errorf("copying inputs: %w", err)
}
