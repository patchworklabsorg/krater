package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// The dead-man switch. Krater ends every machine it rents, but only while it is up: on staging (2026-10-14) Krater
// went down with a box running, and the box billed for four hours with nobody watching. So a runner that hears
// nothing from Krater for long enough stops its own machine through Vast's API, with the key Vast gives every
// container for its own instance (CONTAINER_API_KEY: it can start, stop or destroy that one instance and nothing
// else). It stops rather than destroys: GPU billing ends, and the disk keeps whatever the job or the member did while
// Krater was away, since nothing reaches storage without Krater. Back up, Krater starts a job's machine again (the job
// carries on from the checkpoints on its disk) and shows a box as stopped, for its member to resume.

// DefaultSelfStopAfter is the silence that stops the machine until Krater's /start says otherwise (its
// `runner_self_stop_minutes` policy). It also covers a runner whose /start never got through.
const DefaultSelfStopAfter = 3 * time.Hour

// DefaultVastURL is where Vast's API is, unless /start names another.
const DefaultVastURL = "https://console.vast.ai"

// Methods of stopping a machine that Krater's /start can name (`self_stop.method`).
const (
	// MethodVastAPI stops the instance through Vast's API with the container's own key.
	MethodVastAPI = "vast_api"
	// MethodShutdown has the machine shut itself down, for a cloud VM whose shutdown behaviour is stop.
	MethodShutdown = "shutdown"
)

// SelfStopDescriptor is how Krater says this machine stops itself. The instance id is a string because providers'
// ids aren't all integers (Vast's is).
type SelfStopDescriptor struct {
	Method     string `json:"method"`
	URL        string `json:"url"`
	InstanceID string `json:"instance_id"`
}

// shutdownCommands are tried in turn: the first that runs ends the machine.
var shutdownCommands = [][]string{{"shutdown", "-h", "now"}, {"poweroff"}, {"halt"}}

// Contact is when Krater last answered this runner, whatever it said: a refusal is an answer too. A gateway error
// (Krater down behind its proxy) or no connection at all is not.
type Contact struct {
	mu   sync.Mutex
	last time.Time
}

// NewContact starts the clock at `now`, so a runner that never reaches Krater still stops its machine in time.
func NewContact(now time.Time) *Contact { return &Contact{last: now} }

// Touch records an answer.
func (c *Contact) Touch(at time.Time) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if at.After(c.last) {
		c.last = at
	}
}

// Last is when Krater last answered.
func (c *Contact) Last() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.last
}

// SelfStop stops this machine after `after` without an answer from Krater.
type SelfStop struct {
	Contact *Contact
	HTTP    *http.Client
	// Now is the clock; tests replace it.
	Now func() time.Time
	// Env reads a variable of the container's; tests replace it. Vast sets CONTAINER_API_KEY and CONTAINER_ID in the
	// container's environment, which a process started from `onstart` may not inherit, so the default also reads
	// /proc/1/environ.
	Env func(name string) string
	// Exec runs a command to completion; tests replace it so nothing shuts down.
	Exec func(name string, args ...string) error
	// StatePath is where Configure keeps what /start said; blank keeps nothing.
	StatePath string

	mu         sync.Mutex
	after      time.Duration
	instanceID int64
	vastURL    string
	method     string // "" is Vast's API, as before /start named a method
	disabled   string // why the stop call is off (an unknown method, an unusable id): logged once, when it fires
	before     func()
	fired      atomic.Bool
	stopped    atomic.Bool
	warnedKey  bool
}

// NewSelfStop is a switch with the defaults; Configure fills in what /start says.
func NewSelfStop(httpClient *http.Client) *SelfStop {
	return &SelfStop{
		Contact: NewContact(time.Now()),
		HTTP:    httpClient,
		Now:     time.Now,
		Env:     containerEnv,
		Exec:    runCommand,
		after:   DefaultSelfStopAfter,
		vastURL: DefaultVastURL,
	}
}

// Configure takes the silence, the instance and Vast's address from Krater's answer to /start. A negative silence
// turns the switch off.
func (s *SelfStop) Configure(info *StartInfo) {
	if s == nil || info == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	defer s.save()
	switch {
	case info.SelfStopAfterSeconds > 0:
		s.after = time.Duration(info.SelfStopAfterSeconds) * time.Second
	case info.SelfStopAfterSeconds < 0:
		// Krater's provider can't stop a machine from inside it: the provider ends abandoned work itself.
		s.after = 0
	}
	if d := info.SelfStop; d != nil {
		// The descriptor wins over the legacy Vast fields: a Krater that sends it names the method itself.
		s.configureMethod(d)
		return
	}
	// An older Krater: only Vast's two fields.
	if info.VastInstanceID > 0 {
		s.instanceID = info.VastInstanceID
	}
	if info.VastAPIURL != "" {
		s.vastURL = info.VastAPIURL
	}
}

// selfStopState is what /start configured, on disk. Vast restarts a container on the same machine with its disk as
// it was, and a runner restarted while Krater is down can't ask /start again: without this it would wait the 3-hour
// default rather than the project's silence, and stop the machine the default way. The clock is not kept: a restarted
// runner counts from its own start, as a new one does.
type selfStopState struct {
	AfterSeconds int64  `json:"after_seconds"` // 0 is off
	InstanceID   int64  `json:"instance_id"`
	VastURL      string `json:"vast_url"`
	Method       string `json:"method"`
	Disabled     string `json:"disabled"`
}

// save writes the configuration to StatePath; s.mu is held. A failure only costs the restart case, so it is logged.
func (s *SelfStop) save() {
	if s.StatePath == "" {
		return
	}
	data, _ := json.Marshal(selfStopState{
		AfterSeconds: int64(s.after / time.Second),
		InstanceID:   s.instanceID,
		VastURL:      s.vastURL,
		Method:       s.method,
		Disabled:     s.disabled,
	})
	tmp := s.StatePath + ".tmp"
	err := os.MkdirAll(filepath.Dir(s.StatePath), 0o700)
	if err == nil {
		err = os.WriteFile(tmp, data, 0o600)
	}
	if err == nil {
		err = os.Rename(tmp, s.StatePath)
	}
	if err != nil {
		logf("could not keep the self-stop settings for a restart: %v", err)
	}
}

// Load takes the configuration an earlier run of this runner kept at StatePath, and reports whether there was one.
// /start, when it gets through, configures the switch again.
func (s *SelfStop) Load() bool {
	if s == nil || s.StatePath == "" {
		return false
	}
	data, err := os.ReadFile(s.StatePath)
	if err != nil {
		if !os.IsNotExist(err) {
			logf("could not read the kept self-stop settings: %v", err)
		}
		return false
	}
	var st selfStopState
	if err := json.Unmarshal(data, &st); err != nil || st.AfterSeconds < 0 {
		logf("ignoring kept self-stop settings this runner can't read")
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.after = time.Duration(st.AfterSeconds) * time.Second
	s.instanceID, s.method, s.disabled = st.InstanceID, st.Method, st.Disabled
	if st.VastURL != "" {
		s.vastURL = st.VastURL
	}
	return true
}

// configureMethod applies a descriptor; s.mu is held.
func (s *SelfStop) configureMethod(d *SelfStopDescriptor) {
	s.method, s.disabled = d.Method, ""
	switch d.Method {
	case MethodVastAPI:
		if d.URL != "" {
			s.vastURL = d.URL
		}
		s.instanceID = 0
		if d.InstanceID == "" {
			return // the container's own id is used, as for an old Krater that sent none
		}
		// Digits only: ParseInt would take "+5" or "-5", and an id Krater got wrong must not stop another instance.
		id, err := strconv.ParseInt(d.InstanceID, 10, 64)
		if err != nil || id <= 0 || strings.Trim(d.InstanceID, "0123456789") != "" {
			s.disabled = fmt.Sprintf("Krater sent a Vast instance id this runner can't use: %q", d.InstanceID)
			return
		}
		s.instanceID = id
	case MethodShutdown:
	default:
		s.disabled = fmt.Sprintf("Krater named a self-stop method this runner doesn't know: %s", d.Method)
	}
}

// Before sets what to do before the machine stops: a job stops its command, so it can save a last checkpoint.
func (s *SelfStop) Before(f func()) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.before = f
}

// Fired reports whether the machine is stopping itself: the runner must then not report the command's end to Krater,
// which would take the stop for the job's own exit.
func (s *SelfStop) Fired() bool { return s != nil && s.fired.Load() }

// Run checks every `every` until ctx ends.
func (s *SelfStop) Run(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.Check(ctx)
		}
	}
}

// Check stops the machine if Krater has been silent too long. Once it has fired it keeps asking Vast until the stop
// goes through (the machine, and this process with it, then stops).
func (s *SelfStop) Check(ctx context.Context) {
	if s.stopped.Load() {
		return
	}
	s.mu.Lock()
	after, before, method, disabled := s.after, s.before, s.method, s.disabled
	s.mu.Unlock()
	if after <= 0 {
		return
	}
	silent := s.Now().Sub(s.Contact.Last())
	if !s.fired.Load() {
		if silent < after {
			return
		}
		s.fired.Store(true)
		logf("no answer from Krater for %s: stopping this machine so it doesn't bill with nobody watching; "+
			"its disk keeps the work, and Krater picks it up when it's back", silent.Round(time.Minute))
		if before != nil {
			before()
		}
		if disabled != "" {
			// The command is stopped, which is all this runner can do; asking again every minute would only repeat this.
			logf("%s; the command is stopped and the machine is left running", disabled)
			s.stopped.Store(true)
			return
		}
	}
	if method == MethodShutdown {
		if err := s.shutDown(); err != nil {
			logf("could not shut this machine down: %v; trying again in a minute", err)
			return
		}
		s.stopped.Store(true)
		logf("shut this machine down")
		return
	}
	if err := s.stopMachine(ctx); err != nil {
		logf("could not stop this machine: %v; trying again in a minute", err)
		return
	}
	s.stopped.Store(true)
	logf("asked Vast to stop this machine")
}

// shutDown ends the machine from inside, with the first of shutdownCommands that works.
func (s *SelfStop) shutDown() error {
	var failures []string
	for _, argv := range shutdownCommands {
		err := s.Exec(argv[0], argv[1:]...)
		if err == nil {
			return nil
		}
		failures = append(failures, fmt.Sprintf("%s: %v", argv[0], err))
	}
	return fmt.Errorf("%s", strings.Join(failures, "; "))
}

func runCommand(name string, args ...string) error {
	return exec.Command(name, args...).Run()
}

func (s *SelfStop) stopMachine(ctx context.Context) error {
	key := s.Env("CONTAINER_API_KEY")
	if key == "" {
		s.mu.Lock()
		warned := s.warnedKey
		s.warnedKey = true
		s.mu.Unlock()
		if !warned {
			return fmt.Errorf("Vast gave this container no CONTAINER_API_KEY")
		}
		return fmt.Errorf("still no CONTAINER_API_KEY")
	}
	s.mu.Lock()
	id, base := s.instanceID, s.vastURL
	s.mu.Unlock()
	if id <= 0 {
		parsed, err := strconv.ParseInt(s.Env("CONTAINER_ID"), 10, 64)
		if err != nil || parsed <= 0 {
			return fmt.Errorf("this machine's Vast instance id is unknown")
		}
		id = parsed
	}
	// Every Vast path ends with a slash: without it Vast answers 308 and does nothing.
	url := fmt.Sprintf("%s/api/v0/instances/%d/", strings.TrimRight(base, "/"), id)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, bytes.NewReader([]byte(`{"state": "stopped"}`)))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode/100 != 2 || bytes.Contains(data, []byte(`"success": false`)) ||
		bytes.Contains(data, []byte(`"success":false`)) {
		return fmt.Errorf("Vast answered %d: %s", resp.StatusCode, truncate(string(data), 200))
	}
	return nil
}

// containerEnv is the variable from this process's environment or, failing that, the container's main process's.
func containerEnv(name string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	data, err := os.ReadFile("/proc/1/environ")
	if err != nil {
		return ""
	}
	for _, entry := range bytes.Split(data, []byte{0}) {
		if k, v, ok := strings.Cut(string(entry), "="); ok && k == name {
			return v
		}
	}
	return ""
}
