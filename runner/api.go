package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// API talks to Krater's /runner/v1/ endpoints with this instance's token. It is the only thing on the machine
// that knows the token, and the token only works for this one instance.
type API struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
	// Contact records each answer from Krater, for the dead-man switch (selfstop.go). Optional.
	Contact *Contact
}

// StartInfo is Krater's answer to /start.
type StartInfo struct {
	JobID                 string   `json:"job_id"`
	Attempt               int      `json:"attempt"`
	Resumed               bool     `json:"resumed"`
	Command               string   `json:"command"`
	Kind                  string   `json:"kind"`
	SSHKeys               []string `json:"ssh_keys"`
	HeartbeatSeconds      int      `json:"heartbeat_seconds"`
	CheckpointSyncSeconds int      `json:"checkpoint_sync_seconds"`
	LogSyncSeconds        int      `json:"log_sync_seconds"`
	// With no answer from Krater for this long, stop this machine (selfstop.go).
	SelfStopAfterSeconds int `json:"self_stop_after_seconds"`
	// How to stop it, whatever the provider (nil from an older Krater, which sends only the two Vast fields below).
	SelfStop       *SelfStopDescriptor `json:"self_stop"`
	VastInstanceID int64               `json:"vast_instance_id"`
	VastAPIURL     string              `json:"vast_api_url"`
	// The project's secrets, name -> value. Never logged.
	Secrets map[string]string `json:"secrets"`
}

// Reply is Krater's answer to a heartbeat.
type Reply struct {
	Stop bool `json:"stop"` // the job has ended
	Sync bool `json:"sync"` // save /workspace and confirm: Krater is about to stop the box
}

// RemoteFile is one stored checkpoint or output.
type RemoteFile struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
	URL  string `json:"url"`
}

// Upload is Krater's answer to /files/{area}/upload: one URL, or a multipart upload with a URL per part.
type Upload struct {
	Name      string   `json:"name"`
	URL       string   `json:"url"`
	UploadID  string   `json:"upload_id"`
	PartBytes int64    `json:"part_bytes"`
	Parts     []string `json:"parts"`
}

// ErrDenied means Krater no longer accepts this token: the machine is being destroyed or the job has ended.
type ErrDenied struct{ Message string }

func (e *ErrDenied) Error() string { return "krater refused the runner: " + e.Message }

func (a *API) post(ctx context.Context, path string, body any, out any) error {
	var payload io.Reader = http.NoBody
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		payload = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(a.BaseURL, "/")+"/runner/v1"+path, payload)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+a.Token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	var answer map[string]any
	isJSON := json.Unmarshal(data, &answer) == nil && answer != nil
	// Only Krater's own answers count as contact: a success, or a refusal in its JSON shape. A proxy in front of it
	// (a WAF challenge, Funnel) answers 403/429/5xx with an HTML page, which must neither end the job nor keep the
	// dead-man switch from firing.
	fromKrater := resp.StatusCode/100 == 2 || (resp.StatusCode/100 == 4 && isJSON)
	if fromKrater {
		a.Contact.Touch(time.Now())
	}
	if resp.StatusCode == http.StatusForbidden && fromKrater {
		if msg, ok := answer["error"].(string); ok {
			return &ErrDenied{Message: msg}
		}
	}
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("krater answered %d to %s: %s", resp.StatusCode, path, truncate(string(data), 300))
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	return nil
}

// Start reports that the runner started and returns what to run.
func (a *API) Start(ctx context.Context) (*StartInfo, error) {
	var info StartInfo
	if err := a.post(ctx, "/start", nil, &info); err != nil {
		return nil, err
	}
	return &info, nil
}

// Heartbeat reports the machine's state; it returns true when Krater wants the job stopped.
func (a *API) Heartbeat(ctx context.Context, beat map[string]any) (bool, error) {
	reply, err := a.HeartbeatReply(ctx, beat)
	if err != nil {
		return false, err
	}
	return reply.Stop, nil
}

// HeartbeatReply reports the machine's state and returns all of Krater's answer.
func (a *API) HeartbeatReply(ctx context.Context, beat map[string]any) (*Reply, error) {
	var out Reply
	if err := a.post(ctx, "/heartbeat", beat, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// Synced confirms /workspace was saved.
func (a *API) Synced(ctx context.Context) error {
	return a.post(ctx, "/synced", nil, nil)
}

// Exit reports the command's exit code.
func (a *API) Exit(ctx context.Context, code int) error {
	return a.post(ctx, "/exit", map[string]any{"exit_code": code}, nil)
}

// List returns the stored files of an area ("checkpoints" or "outputs").
func (a *API) List(ctx context.Context, area string) ([]RemoteFile, error) {
	var out struct {
		Files []RemoteFile `json:"files"`
	}
	if err := a.post(ctx, "/files/"+area+"/list", nil, &out); err != nil {
		return nil, err
	}
	return out.Files, nil
}

// BeginUpload asks where to upload a file of `size` bytes.
func (a *API) BeginUpload(ctx context.Context, area, name string, size int64) (*Upload, error) {
	var out Upload
	if err := a.post(ctx, "/files/"+area+"/upload", map[string]any{"name": name, "size": size}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// CompleteUpload finishes a multipart upload.
func (a *API) CompleteUpload(ctx context.Context, area, name, uploadID string, etags []string) error {
	body := map[string]any{"name": name, "upload_id": uploadID, "etags": etags}
	return a.post(ctx, "/files/"+area+"/complete", body, nil)
}

// retryFirstWait is the pause after the first failure; it doubles up to 30 s. A variable so tests needn't wait.
var retryFirstWait = 2 * time.Second

// retry calls fn until it succeeds, Krater refuses the token, or the deadline passes.
func retry(ctx context.Context, deadline time.Duration, fn func() error) error {
	end := time.Now().Add(deadline)
	wait := retryFirstWait
	for {
		err := fn()
		if err == nil {
			return nil
		}
		var denied *ErrDenied
		if errors.As(err, &denied) || time.Now().After(end) {
			return err
		}
		logf("%v; retrying in %s", err, wait)
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(wait):
		}
		if wait < 30*time.Second {
			wait *= 2
		}
	}
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
