package main

import (
	"context"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

// nvidiaSMI returns the mean utilization (0-100) across the machine's GPUs, or false when nvidia-smi isn't usable.
// Vast's own gpu_util lags 50-105 s and flickers, so the runner's sample is Krater's main idle signal.
func nvidiaSMI() (float64, bool) {
	out, err := exec.Command("nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits").Output()
	if err != nil {
		return 0, false
	}
	return meanOfLines(string(out))
}

func meanOfLines(s string) (float64, bool) {
	var sum float64
	n := 0
	for _, line := range strings.Split(strings.TrimSpace(s), "\n") {
		v, err := strconv.ParseFloat(strings.TrimSpace(line), 64)
		if err != nil {
			continue
		}
		sum += v
		n++
	}
	if n == 0 {
		return 0, false
	}
	return sum / float64(n), true
}

// gpuSampler averages GPU samples taken between heartbeats.
type gpuSampler struct {
	sample func() (float64, bool)
	mu     sync.Mutex
	sum    float64
	n      int
}

func newGPUSampler(sample func() (float64, bool)) *gpuSampler { return &gpuSampler{sample: sample} }

func (g *gpuSampler) take() {
	if g.sample == nil {
		return
	}
	if v, ok := g.sample(); ok {
		g.mu.Lock()
		g.sum += v
		g.n++
		g.mu.Unlock()
	}
}

func (g *gpuSampler) run(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			g.take()
		}
	}
}

// average returns the mean since the last call, and resets it.
func (g *gpuSampler) average() (float64, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.n == 0 {
		return 0, false
	}
	avg := g.sum / float64(g.n)
	g.sum, g.n = 0, 0
	return avg, true
}
