package main

import (
	"bufio"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
)

// NetCounter reports how many bytes this container has received and sent since its job's first start, from the
// kernel's counters for the container's own network (/proc/net/dev). Vast bills data per GB at the host's prices,
// and Krater adds these to its live spend estimate between its hourly checks of Vast's charges.
//
// It counts everything through the container's interface: checkpoint restores and uploads, datasets, pip, SSH and
// Jupyter. Not the image pull, which the host does before the container exists (Krater estimates that from the
// image's size).
//
// The kernel's counters start again from zero in a new network namespace (Vast restarts the container after an
// outbid, a self-stop, a box resume), so the totals carry over: the last reading and the namespace it came from are
// kept in StatePath, and a new namespace, or a counter that went down, adds the last reading to the base.
//
// It reports nothing when it can't tell it sees only the container's own network: a host-network container would
// count other renters' traffic.
type NetCounter struct {
	Path      string // /proc/net/dev
	NSPath    string // /proc/self/ns/net
	StatePath string // blank: totals don't survive a restart

	mu      sync.Mutex
	state   netState
	warned  bool
	skipped bool
}

type netState struct {
	Namespace string `json:"namespace"`
	BaseIn    uint64 `json:"base_in"`
	BaseOut   uint64 `json:"base_out"`
	LastIn    uint64 `json:"last_in"`
	LastOut   uint64 `json:"last_out"`
}

var ethIface = regexp.MustCompile(`^eth[0-9]+$`)

// Interface names that mean the container shares the host's network.
var hostIfacePrefixes = []string{"docker", "br-", "veth", "virbr", "cni", "flannel", "cali", "vxlan", "kube"}

// Load reads the totals a previous run of this container kept. It returns whether there were any.
func (n *NetCounter) Load() bool {
	if n.StatePath == "" {
		return false
	}
	data, err := os.ReadFile(n.StatePath)
	if err != nil {
		return false
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	return json.Unmarshal(data, &n.state) == nil
}

// Sample returns the bytes received and sent since the job's first start, or false when it can't count them.
func (n *NetCounter) Sample() (uint64, uint64, bool) {
	f, err := os.Open(n.Path)
	if err != nil {
		return 0, 0, false
	}
	defer f.Close()
	in, out, ok, why := containerBytes(f)
	n.mu.Lock()
	defer n.mu.Unlock()
	if !ok {
		if !n.skipped {
			logf("not reporting network use: %s", why)
			n.skipped = true
		}
		return 0, 0, false
	}
	namespace := ""
	if n.NSPath != "" {
		namespace, _ = os.Readlink(n.NSPath)
	}
	if (namespace != "" && n.state.Namespace != "" && namespace != n.state.Namespace) ||
		in < n.state.LastIn || out < n.state.LastOut {
		// A new network namespace: the counters started again.
		n.state.BaseIn += n.state.LastIn
		n.state.BaseOut += n.state.LastOut
	}
	n.state.Namespace = namespace
	n.state.LastIn, n.state.LastOut = in, out
	n.save()
	return n.state.BaseIn + in, n.state.BaseOut + out, true
}

func (n *NetCounter) save() {
	if n.StatePath == "" {
		return
	}
	data, _ := json.Marshal(n.state)
	tmp := n.StatePath + ".tmp"
	err := os.WriteFile(tmp, data, 0o600)
	if err == nil {
		err = os.Rename(tmp, n.StatePath)
	}
	if err != nil && !n.warned {
		logf("can't keep the network totals in %s: %v", filepath.Dir(n.StatePath), err)
		n.warned = true
	}
}

// containerBytes sums the received and sent bytes of the container's ethN interfaces in /proc/net/dev. It refuses
// (with why) when an interface shows this is the host's network, or there is no ethN at all.
func containerBytes(r io.Reader) (uint64, uint64, bool, string) {
	var in, out uint64
	found := false
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		name, rest, ok := strings.Cut(scanner.Text(), ":")
		if !ok {
			continue // the two header lines
		}
		name = strings.TrimSpace(name)
		for _, prefix := range hostIfacePrefixes {
			if strings.HasPrefix(name, prefix) {
				return 0, 0, false, "interface " + name + " means this is the host's network"
			}
		}
		if !ethIface.MatchString(name) {
			continue // lo, and the kernel's tunnel devices (tunl0, sit0, ...) every namespace has
		}
		fields := strings.Fields(rest)
		if len(fields) < 9 {
			continue
		}
		rx, err1 := strconv.ParseUint(fields[0], 10, 64)
		tx, err2 := strconv.ParseUint(fields[8], 10, 64)
		if err1 != nil || err2 != nil {
			continue
		}
		in += rx
		out += tx
		found = true
	}
	if !found {
		return 0, 0, false, "no ethN interface in /proc/net/dev"
	}
	return in, out, true, ""
}

// netSample is what heartbeats report network use from; nil (tests, or no counter) reports none.
var netSample func() (uint64, uint64, bool)

// addNetUse puts the network totals on a heartbeat.
func addNetUse(beat map[string]any) {
	if netSample == nil {
		return
	}
	if in, out, ok := netSample(); ok {
		beat["net_in_bytes"] = in
		beat["net_out_bytes"] = out
	}
}
