package main

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// Project secrets (an HF_TOKEN, say) come from Krater in the start reply, never through Vast. The runner puts them in
// the job command's environment, or for a box in a root-only shell profile outside /workspace (so they're never
// synced to storage). It never logs a value: only how many there are.

var secretNameRE = regexp.MustCompile(`^[A-Z_][A-Z0-9_]{0,63}$`)

// secretEnv is `secrets` as sorted NAME=value pairs, skipping names Krater would never send.
func secretEnv(secrets map[string]string) []string {
	names := make([]string, 0, len(secrets))
	for name, value := range secrets {
		if secretNameRE.MatchString(name) && !strings.HasPrefix(name, "KRATER_") && !strings.ContainsRune(value, 0) {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	env := make([]string, 0, len(names))
	for _, name := range names {
		env = append(env, name+"="+secrets[name])
	}
	return env
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }

// writeSecretsProfile writes `export NAME='value'` lines to `path` (mode 0600), for a box's login shells, or removes
// the file when the project has no secrets (one may have been deleted since the box last started).
func writeSecretsProfile(path string, secrets map[string]string) error {
	env := secretEnv(secrets)
	if len(env) == 0 {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	}
	var b strings.Builder
	b.WriteString("# Krater project secrets: written by krater-runner at each start. Don't edit.\n")
	for _, pair := range env {
		name, value, _ := strings.Cut(pair, "=")
		b.WriteString("export " + name + "=" + shellQuote(value) + "\n")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".krater-partial"
	if err := os.WriteFile(tmp, []byte(b.String()), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
