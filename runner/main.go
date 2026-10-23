// Command krater-runner runs a Krater Compute job on a rented Vast machine.
//
// Krater's bootstrap script downloads it, checks its sha256 and starts it. It then:
//
//  1. tells Krater it started (POST /runner/v1/start) and learns the command and whether this is a resume;
//  2. restores the job's checkpoints into $KRATER_CHECKPOINT_DIR;
//  3. runs the member's command with `sh -c`;
//  4. sends a heartbeat every minute with the GPU use it samples from nvidia-smi, and stops the command when Krater
//     says so;
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
	code, err := Run(context.Background(), cfg, &http.Client{Timeout: 60 * time.Second}, &http.Client{}, nvidiaSMI)
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
	if n, err := checkpoints.Restore(ctx); err != nil {
		logf("restoring checkpoints failed: %v", err)
	} else if n > 0 {
		logf("restored %d checkpoint files", n)
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
		if n, err := workspace.Restore(ctx); err != nil {
			logf("restoring /workspace failed: %v", err)
		} else if n > 0 {
			logf("restored %d workspace files", n)
		}
		runBox(ctx, api, info, workspace, gpu, cfg.ProcNet)
		return 0, nil
	}
	if info.Command == "" {
		heartbeatLoop(ctx, api, seconds(info.HeartbeatSeconds, 60), gpu, nil)
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
	)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		_ = api.Exit(ctx, 127)
		return 127, fmt.Errorf("could not start the command: %w", err)
	}

	done := make(chan struct{})
	var stopOnce sync.Once
	stop := func(why string) {
		stopOnce.Do(func() {
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
	loops, cancelLoops := context.WithCancel(ctx)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		heartbeatLoop(loops, api, seconds(info.HeartbeatSeconds, 60), gpu, func() { stop("Krater asked to stop the job") })
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
	logf("the command exited with code %d", code)

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
	if err := retry(ctx, 10*time.Minute, func() error { return api.Exit(ctx, code) }); err != nil {
		return code, fmt.Errorf("could not report the exit code: %w", err)
	}
	return code, nil
}

// finalPushWindow bounds the retries of the final upload after the command exits.
var finalPushWindow = 5 * time.Minute

func heartbeatLoop(ctx context.Context, api *API, every time.Duration, gpu func() (float64, bool), stop func()) {
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
		shouldStop, err := api.Heartbeat(ctx, beat)
		var denied *ErrDenied
		switch {
		case errors.As(err, &denied):
			shouldStop = true
		case err != nil && ctx.Err() == nil:
			logf("heartbeat failed: %v", err)
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
