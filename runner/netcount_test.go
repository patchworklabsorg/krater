package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

const netDevHeader = `Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
`

func netDev(lines ...string) string { return netDevHeader + strings.Join(lines, "\n") + "\n" }

func devLine(name string, rx, tx int) string {
	return "  " + name + ": " + strconv.Itoa(rx) + " 10 0 0 0 0 0 0 " + strconv.Itoa(tx) + " 20 0 0 0 0 0 0"
}

func TestContainerBytesSumsEthInterfacesOnly(t *testing.T) {
	in, out, ok, _ := containerBytes(strings.NewReader(netDev(
		devLine("lo", 999, 999), devLine("eth0", 1000, 200), devLine("eth1", 50, 5), devLine("tunl0", 7, 7),
	)))
	if !ok || in != 1050 || out != 205 {
		t.Fatalf("got %d %d %v", in, out, ok)
	}
}

func TestContainerBytesRefusesTheHostNetwork(t *testing.T) {
	for _, iface := range []string{"docker0", "veth12ab", "br-3f2a"} {
		_, _, ok, why := containerBytes(strings.NewReader(netDev(devLine("eth0", 1, 1), devLine(iface, 1, 1))))
		if ok || !strings.Contains(why, iface) {
			t.Fatalf("%s: ok=%v why=%q", iface, ok, why)
		}
	}
	if _, _, ok, _ := containerBytes(strings.NewReader(netDev(devLine("lo", 1, 1), devLine("ens3", 1, 1)))); ok {
		t.Fatal("no ethN: should report nothing")
	}
}

func TestNetCounterCarriesTotalsAcrossARestart(t *testing.T) {
	dir := t.TempDir()
	dev := filepath.Join(dir, "dev")
	ns := filepath.Join(dir, "ns")
	state := filepath.Join(dir, "net.json")
	write := func(rx, tx int) {
		if err := os.WriteFile(dev, []byte(netDev(devLine("eth0", rx, tx))), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	link := func(target string) {
		_ = os.Remove(ns)
		if err := os.Symlink(target, ns); err != nil {
			t.Skip("no symlinks here:", err)
		}
	}
	link("net:[1]")
	write(1000, 100)
	counter := &NetCounter{Path: dev, NSPath: ns, StatePath: state}
	if in, out, ok := counter.Sample(); !ok || in != 1000 || out != 100 {
		t.Fatalf("first: %d %d", in, out)
	}
	write(5000, 300)
	if in, _, _ := counter.Sample(); in != 5000 {
		t.Fatalf("growing: %d", in)
	}

	// The container restarts: a new namespace whose counters already passed the old reading.
	link("net:[2]")
	write(6000, 50)
	again := &NetCounter{Path: dev, NSPath: ns, StatePath: state}
	if !again.Load() {
		t.Fatal("state not kept")
	}
	if in, out, _ := again.Sample(); in != 11000 || out != 350 {
		t.Fatalf("after restart: %d %d", in, out)
	}

	// Same namespace but the counter went down (no namespace link): a reset too.
	again.NSPath = ""
	write(10, 10)
	if in, out, _ := again.Sample(); in != 11010 || out != 360 {
		t.Fatalf("after reset: %d %d", in, out)
	}
}

func TestAddNetUseLeavesOutWhatItCantCount(t *testing.T) {
	defer func() { netSample = nil }()
	beat := map[string]any{}
	addNetUse(beat)
	if len(beat) != 0 {
		t.Fatal("no sampler: nothing added")
	}
	netSample = func() (uint64, uint64, bool) { return 0, 0, false }
	addNetUse(beat)
	if len(beat) != 0 {
		t.Fatal("can't count: nothing added")
	}
	netSample = func() (uint64, uint64, bool) { return 7, 9, true }
	addNetUse(beat)
	if beat["net_in_bytes"] != uint64(7) || beat["net_out_bytes"] != uint64(9) {
		t.Fatalf("got %v", beat)
	}
}
