package main

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// LogMaxBytes caps a job's saved output: past it the runner notes that the rest wasn't saved, and the output still
// reaches the container's own log on Vast. The cap keeps a chatty job from filling the project's storage quota.
const LogMaxBytes = 16 << 20

// logFile is the job's output as its team reads it in Krater: the command's stdout and stderr, and the runner's own
// lines, in one file that the "logs" syncer uploads every minute. Safe for concurrent writers.
type logFile struct {
	mu      sync.Mutex
	f       *os.File
	written int64
	max     int64
	full    bool
}

func newLogFile(dir string, attempt int, max int64) (*logFile, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, fmt.Sprintf("attempt-%d.log", attempt)), os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		return nil, err
	}
	return &logFile{f: f, max: max}, nil
}

// Write keeps what fits under the cap and always reports success: a full log must never fail the command's writes.
func (l *logFile) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.full {
		return len(p), nil
	}
	keep := p
	if room := l.max - l.written; int64(len(p)) > room {
		keep = p[:room]
		l.full = true
	}
	n, _ := l.f.Write(keep)
	l.written += int64(n)
	if l.full {
		_, _ = fmt.Fprintf(l.f, "\n[krater: the output passed %d MB; the rest isn't saved here]\n", l.max>>20)
	}
	return len(p), nil
}

func (l *logFile) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.f.Close()
}
