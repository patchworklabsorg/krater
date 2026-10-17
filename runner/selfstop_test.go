package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeVast records what the runner asks of Vast's API with its container key.
type fakeVast struct {
	server   *httptest.Server
	mu       sync.Mutex
	requests []string // "METHOD path auth body"
	failures int      // answer 500 to this many requests first
}

func newFakeVast(t *testing.T) *fakeVast {
	v := &fakeVast{}
	v.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		v.mu.Lock()
		defer v.mu.Unlock()
		v.requests = append(v.requests, fmt.Sprintf("%s %s %s %s", r.Method, r.URL.Path, r.Header.Get("Authorization"), body))
		if v.failures > 0 {
			v.failures--
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		_, _ = w.Write([]byte(`{"success": true}`))
	}))
	t.Cleanup(v.server.Close)
	return v
}

func (v *fakeVast) calls() []string {
	v.mu.Lock()
	defer v.mu.Unlock()
	return append([]string(nil), v.requests...)
}

// testSelfStop is a switch against `vast` whose clock the test moves.
func testSelfStop(vast *fakeVast, now *time.Time, env map[string]string) *SelfStop {
	s := NewSelfStop(http.DefaultClient)
	s.Contact = NewContact(*now)
	s.Now = func() time.Time { return *now }
	s.Env = func(name string) string { return env[name] }
	s.Configure(&StartInfo{SelfStopAfterSeconds: 3600, VastInstanceID: 55916494, VastAPIURL: vast.server.URL})
	return s
}

func TestAMachineStopsItselfAfterAnHourWithoutKrater(t *testing.T) {
	vast := newFakeVast(t)
	now := time.Date(2026, 10, 14, 11, 29, 0, 0, time.UTC)
	s := testSelfStop(vast, &now, map[string]string{"CONTAINER_API_KEY": "instance-key"})
	before := 0
	s.Before(func() { before++ })

	now = now.Add(59 * time.Minute)
	s.Check(context.Background())
	if len(vast.calls()) != 0 || s.Fired() {
		t.Fatalf("stopped after 59 minutes: %v", vast.calls())
	}
	now = now.Add(time.Minute)
	s.Check(context.Background())

	want := `PUT /api/v0/instances/55916494/ Bearer instance-key {"state": "stopped"}`
	if calls := vast.calls(); len(calls) != 1 || calls[0] != want {
		t.Fatalf("Vast got %v; want [%s]", calls, want)
	}
	if !s.Fired() || before != 1 {
		t.Errorf("fired=%v, before ran %d times", s.Fired(), before)
	}
	s.Check(context.Background()) // stopped: nothing more to do
	if len(vast.calls()) != 1 {
		t.Errorf("asked again after the stop went through: %v", vast.calls())
	}
}

func TestAnyAnswerFromKraterKeepsTheMachineGoing(t *testing.T) {
	vast := newFakeVast(t)
	now := time.Now()
	s := testSelfStop(vast, &now, map[string]string{"CONTAINER_API_KEY": "k"})
	for _, status := range []int{http.StatusOK, http.StatusForbidden, http.StatusBadGateway} {
		krater := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{}`))
		}))
		api := &API{BaseURL: krater.URL, Token: "t", HTTP: http.DefaultClient, Contact: s.Contact}
		before := s.Contact.Last()
		time.Sleep(2 * time.Millisecond)
		_, _ = api.Heartbeat(context.Background(), map[string]any{})
		krater.Close()
		touched := s.Contact.Last().After(before)
		if touched != (status < 500) {
			t.Errorf("answer %d: contact touched=%v", status, touched)
		}
	}
}

func TestAFailedStopIsTriedAgainWithoutStoppingTheCommandTwice(t *testing.T) {
	vast := newFakeVast(t)
	vast.failures = 1
	now := time.Now()
	s := testSelfStop(vast, &now, map[string]string{"CONTAINER_API_KEY": "k"})
	before := 0
	s.Before(func() { before++ })
	now = now.Add(2 * time.Hour)

	s.Check(context.Background())
	s.Check(context.Background())

	if len(vast.calls()) != 2 || before != 1 {
		t.Errorf("Vast calls %d (want 2), before ran %d times (want 1)", len(vast.calls()), before)
	}
}

func TestWithoutStartInfoTheContainersOwnIdIsUsed(t *testing.T) {
	vast := newFakeVast(t)
	now := time.Now()
	s := NewSelfStop(http.DefaultClient)
	s.Contact = NewContact(now)
	s.Now = func() time.Time { return now }
	s.Env = func(name string) string {
		return map[string]string{"CONTAINER_API_KEY": "k", "CONTAINER_ID": "123"}[name]
	}
	s.Configure(&StartInfo{VastAPIURL: vast.server.URL}) // an old Krater: no instance id, no silence
	now = now.Add(DefaultSelfStopAfter)

	s.Check(context.Background())

	if calls := vast.calls(); len(calls) != 1 || !strings.HasPrefix(calls[0], "PUT /api/v0/instances/123/ ") {
		t.Errorf("Vast got %v", calls)
	}
}

func TestKraterCanTurnTheSwitchOff(t *testing.T) {
	vast := newFakeVast(t)
	now := time.Now()
	s := testSelfStop(vast, &now, map[string]string{"CONTAINER_API_KEY": "k"})
	s.Configure(&StartInfo{SelfStopAfterSeconds: -1})
	now = now.Add(48 * time.Hour)

	s.Check(context.Background())

	if len(vast.calls()) != 0 || s.Fired() {
		t.Errorf("calls %v, fired %v", vast.calls(), s.Fired())
	}
}

func TestWithoutAContainerKeyNothingIsCalled(t *testing.T) {
	vast := newFakeVast(t)
	now := time.Now()
	s := testSelfStop(vast, &now, map[string]string{})
	now = now.Add(2 * time.Hour)

	s.Check(context.Background())

	if len(vast.calls()) != 0 || !s.Fired() {
		t.Errorf("calls %v, fired %v", vast.calls(), s.Fired())
	}
}

func TestAJobStoppedByItsOwnMachineIsNotReportedAsFinished(t *testing.T) {
	f := newFakeKrater(t)
	f.command = "sleep 60"
	vast := newFakeVast(t)
	now := time.Now()
	cfg := f.cfg(t)
	cfg.SelfStop = testSelfStop(vast, &now, map[string]string{"CONTAINER_API_KEY": "k"})
	ctx := context.Background()
	checked := make(chan struct{})
	go func() {
		defer close(checked)
		// Krater goes quiet once the command runs: the switch fires.
		for f.startCount() == 0 {
			time.Sleep(10 * time.Millisecond)
		}
		time.Sleep(200 * time.Millisecond)
		later := time.Now().Add(2 * time.Hour)
		cfg.SelfStop.Now = func() time.Time { return later }
		cfg.SelfStop.Check(ctx)
	}()
	start := time.Now()

	code, err := Run(ctx, cfg, http.DefaultClient, http.DefaultClient, noGPU)
	<-checked // in the runner, main stays up for the switch after Run returns

	if err != nil || code != 128+15 {
		t.Fatalf("Run = %d, %v; want SIGTERM's 143", code, err)
	}
	if len(f.exitCodes) != 0 {
		t.Errorf("reported exit codes %v to Krater", f.exitCodes)
	}
	if calls := vast.calls(); len(calls) != 1 {
		t.Errorf("Vast got %v", calls)
	}
	if time.Since(start) > 20*time.Second {
		t.Errorf("took %s", time.Since(start))
	}
}

func (f *fakeKrater) startCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.starts
}

// A switch for the descriptor tests: Krater's /start says `info`; `execs` records what the machine was told to run.
func descriptorSelfStop(vast *fakeVast, info *StartInfo, execs *[]string, failFirst int) (*SelfStop, *time.Time) {
	now := time.Date(2026, 10, 14, 11, 0, 0, 0, time.UTC)
	s := NewSelfStop(http.DefaultClient)
	s.Contact = NewContact(now)
	s.Now = func() time.Time { return now }
	s.Env = func(name string) string {
		return map[string]string{"CONTAINER_API_KEY": "k", "CONTAINER_ID": "777"}[name]
	}
	s.Exec = func(name string, args ...string) error {
		*execs = append(*execs, strings.Join(append([]string{name}, args...), " "))
		if len(*execs) <= failFirst {
			return fmt.Errorf("not found")
		}
		return nil
	}
	info.SelfStopAfterSeconds = 3600
	if info.SelfStop != nil && info.SelfStop.URL == "FAKE" {
		d := *info.SelfStop
		d.URL = vast.server.URL
		info.SelfStop = &d
	}
	if info.VastAPIURL == "FAKE" {
		info.VastAPIURL = vast.server.URL
	}
	s.Configure(info)
	later := now.Add(2 * time.Hour)
	return s, &later
}

func fire(s *SelfStop, later *time.Time) {
	s.Now = func() time.Time { return *later }
	s.Check(context.Background())
}

func TestADescriptorNamesVastsInstanceAndAddress(t *testing.T) {
	vast := newFakeVast(t)
	var execs []string
	s, later := descriptorSelfStop(vast, &StartInfo{
		SelfStop: &SelfStopDescriptor{Method: "vast_api", URL: "FAKE", InstanceID: "55916494"},
	}, &execs, 0)

	fire(s, later)

	if calls := vast.calls(); len(calls) != 1 || !strings.HasPrefix(calls[0], "PUT /api/v0/instances/55916494/ Bearer k ") {
		t.Errorf("Vast got %v", calls)
	}
	if len(execs) != 0 {
		t.Errorf("ran %v", execs)
	}
}

func TestAnOlderKraterSendsOnlyTheVastFields(t *testing.T) {
	vast := newFakeVast(t)
	var execs []string
	s, later := descriptorSelfStop(vast, &StartInfo{VastInstanceID: 4242, VastAPIURL: "FAKE"}, &execs, 0)

	fire(s, later)

	if calls := vast.calls(); len(calls) != 1 || !strings.HasPrefix(calls[0], "PUT /api/v0/instances/4242/ Bearer k ") {
		t.Errorf("Vast got %v", calls)
	}
}

func TestTheDescriptorWinsOverTheLegacyFields(t *testing.T) {
	vast := newFakeVast(t)
	other := newFakeVast(t)
	var execs []string
	s, later := descriptorSelfStop(vast, &StartInfo{
		SelfStop:       &SelfStopDescriptor{Method: "vast_api", URL: "FAKE", InstanceID: "11"},
		VastInstanceID: 22,
		VastAPIURL:     other.server.URL,
	}, &execs, 0)

	fire(s, later)

	if calls := vast.calls(); len(calls) != 1 || !strings.HasPrefix(calls[0], "PUT /api/v0/instances/11/ ") {
		t.Errorf("Vast got %v", calls)
	}
	if len(other.calls()) != 0 {
		t.Errorf("the legacy address got %v", other.calls())
	}
}

func TestShutdownMethodShutsTheMachineDownAndNeverCallsVast(t *testing.T) {
	vast := newFakeVast(t)
	var execs []string
	s, later := descriptorSelfStop(vast, &StartInfo{
		SelfStop:       &SelfStopDescriptor{Method: "shutdown"},
		VastInstanceID: 22,
		VastAPIURL:     "FAKE",
	}, &execs, 0)
	stopped := 0
	s.Before(func() { stopped++ })

	fire(s, later)
	fire(s, later) // done: nothing is asked twice

	if len(execs) != 1 || execs[0] != "shutdown -h now" {
		t.Errorf("ran %v", execs)
	}
	if len(vast.calls()) != 0 || stopped != 1 || !s.Fired() {
		t.Errorf("Vast got %v, command stopped %d times, fired %v", vast.calls(), stopped, s.Fired())
	}
}

func TestShutdownFallsBackToPoweroffThenHalt(t *testing.T) {
	vast := newFakeVast(t)
	var execs []string
	s, later := descriptorSelfStop(vast, &StartInfo{SelfStop: &SelfStopDescriptor{Method: "shutdown"}}, &execs, 2)

	fire(s, later)

	if want := []string{"shutdown -h now", "poweroff", "halt"}; strings.Join(execs, "|") != strings.Join(want, "|") {
		t.Errorf("ran %v, want %v", execs, want)
	}
}

func TestAFailedShutdownIsTriedAgain(t *testing.T) {
	vast := newFakeVast(t)
	var execs []string
	s, later := descriptorSelfStop(vast, &StartInfo{SelfStop: &SelfStopDescriptor{Method: "shutdown"}}, &execs, 3)

	fire(s, later) // all three fail
	fire(s, later) // the next try works

	if len(execs) != 4 || execs[3] != "shutdown -h now" {
		t.Errorf("ran %v", execs)
	}
}

// capturedLog is what the runner logs while f runs.
func capturedLog(f func()) string {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)
	f()
	return buf.String()
}

func TestAnUnknownMethodStopsTheCommandAndSaysSoOnce(t *testing.T) {
	vast := newFakeVast(t)
	var execs []string
	s, later := descriptorSelfStop(vast, &StartInfo{
		SelfStop:       &SelfStopDescriptor{Method: "teleport", URL: "FAKE", InstanceID: "5"},
		VastInstanceID: 22,
		VastAPIURL:     "FAKE",
	}, &execs, 0)
	stopped := 0
	s.Before(func() { stopped++ })

	out := capturedLog(func() {
		fire(s, later)
		fire(s, later)
	})

	if len(vast.calls()) != 0 || len(execs) != 0 {
		t.Errorf("Vast got %v, ran %v", vast.calls(), execs)
	}
	if stopped != 1 || !s.Fired() {
		t.Errorf("command stopped %d times, fired %v", stopped, s.Fired())
	}
	if n := strings.Count(out, "Krater named a self-stop method this runner doesn't know: teleport"); n != 1 {
		t.Errorf("logged %d times: %q", n, out)
	}
}

func TestAnInstanceIdThatIsNotDigitsIsNotUsed(t *testing.T) {
	for _, id := range []string{"i-0abc", "12x", "-5", "+5", "1.0", " 5"} {
		vast := newFakeVast(t)
		var execs []string
		s, later := descriptorSelfStop(vast, &StartInfo{
			SelfStop: &SelfStopDescriptor{Method: "vast_api", URL: "FAKE", InstanceID: id},
		}, &execs, 0)

		fire(s, later)

		if len(vast.calls()) != 0 || len(execs) != 0 || !s.Fired() {
			t.Errorf("id %q: Vast got %v, ran %v, fired %v", id, vast.calls(), execs, s.Fired())
		}
	}
}

func TestMinusOneTurnsTheSwitchOffWhateverTheDescriptorSays(t *testing.T) {
	vast := newFakeVast(t)
	var execs []string
	s, later := descriptorSelfStop(vast, &StartInfo{
		SelfStop: &SelfStopDescriptor{Method: "shutdown"},
	}, &execs, 0)
	s.Configure(&StartInfo{SelfStopAfterSeconds: -1, SelfStop: &SelfStopDescriptor{Method: "shutdown"}})

	fire(s, later)

	if len(vast.calls()) != 0 || len(execs) != 0 || s.Fired() {
		t.Errorf("Vast got %v, ran %v, fired %v", vast.calls(), execs, s.Fired())
	}
}
