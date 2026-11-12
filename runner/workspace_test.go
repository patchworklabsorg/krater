package main

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func workspaceSyncer(f *fakeKrater, dir string) *Syncer {
	api := &API{BaseURL: f.server.URL, Token: f.token, HTTP: http.DefaultClient}
	s := NewSyncer(api, "workspace", dir, http.DefaultClient)
	s.Manifest = true
	return s
}

func write(t *testing.T, path, content string, mode os.FileMode) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil { // past the umask
		t.Fatal(err)
	}
}

func storedManifest(t *testing.T, f *fakeKrater) workspaceManifest {
	t.Helper()
	var m workspaceManifest
	if err := json.Unmarshal(f.objects["workspace/"+manifestName], &m); err != nil {
		t.Fatalf("manifest: %v (objects %v)", err, keys(f.objects))
	}
	return m
}

// A virtual environment needs its executable bits and symlinks. Before the manifest a box moved to another machine
// got its files back without either, and deleted files came back.
func TestAWorkspaceComesBackWithModesSymlinksEmptyFoldersAndWithoutDeletedFiles(t *testing.T) {
	f := newFakeKrater(t)
	ctx := context.Background()
	a := t.TempDir()
	write(t, filepath.Join(a, "venv", "bin", "activate"), "export VIRTUAL_ENV\n", 0o644)
	write(t, filepath.Join(a, "venv", "bin", "pip"), "#!/usr/bin/env python\n", 0o755)
	os.Symlink("/usr/bin/python3", filepath.Join(a, "venv", "bin", "python"))
	os.Symlink("python", filepath.Join(a, "venv", "bin", "python3"))
	os.MkdirAll(filepath.Join(a, "empty", "nested"), 0o755)
	os.Chmod(filepath.Join(a, "empty"), 0o700)
	write(t, filepath.Join(a, "report (1).txt"), "odd name", 0o600)
	write(t, filepath.Join(a, manifestName), "a member's file with Krater's name", 0o644)
	write(t, filepath.Join(a, "gone.txt"), "deleted later", 0o644)
	sa := workspaceSyncer(f, a)
	if _, err := sa.Restore(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := sa.Push(ctx); err != nil {
		t.Fatal(err)
	}
	if _, ok := f.objects["workspace/gone.txt"]; !ok {
		t.Fatalf("gone.txt wasn't stored: %v", keys(f.objects))
	}
	os.Remove(filepath.Join(a, "gone.txt"))
	if _, err := sa.Push(ctx); err != nil {
		t.Fatal(err)
	}
	if _, ok := f.objects["workspace/gone.txt"]; ok {
		t.Errorf("a deleted file stayed in storage: %v", keys(f.objects))
	}
	for key := range f.objects {
		if strings.Contains(key, "(") {
			t.Errorf("%q was stored under a name Krater refuses", key)
		}
	}

	b := t.TempDir()
	n, err := workspaceSyncer(f, b).Restore(ctx)
	if err != nil || n == 0 {
		t.Fatalf("Restore = %d, %v", n, err)
	}
	for path, want := range map[string]os.FileMode{
		"venv/bin/pip": 0o755, "venv/bin/activate": 0o644, "report (1).txt": 0o600, "empty": 0o700 | os.ModeDir,
	} {
		info, err := os.Lstat(filepath.Join(b, path))
		if err != nil || info.Mode() != want {
			t.Errorf("%s: %v; want %v", path, err, want)
			if err == nil {
				t.Errorf("%s mode %v", path, info.Mode())
			}
		}
	}
	for link, want := range map[string]string{"venv/bin/python": "/usr/bin/python3", "venv/bin/python3": "python"} {
		if got, err := os.Readlink(filepath.Join(b, link)); err != nil || got != want {
			t.Errorf("%s -> %q, %v; want %q", link, got, err, want)
		}
	}
	if info, err := os.Stat(filepath.Join(b, "empty", "nested")); err != nil || !info.IsDir() {
		t.Errorf("empty/nested: %v", err)
	}
	if got, _ := os.ReadFile(filepath.Join(b, "report (1).txt")); string(got) != "odd name" {
		t.Errorf("report (1).txt = %q", got)
	}
	if got, _ := os.ReadFile(filepath.Join(b, manifestName)); string(got) != "a member's file with Krater's name" {
		t.Errorf("%s = %q", manifestName, got)
	}
	if _, err := os.Lstat(filepath.Join(b, "gone.txt")); !os.IsNotExist(err) {
		t.Errorf("a deleted file came back: %v", err)
	}
	src, _ := os.Stat(filepath.Join(a, "venv", "bin", "pip"))
	dst, _ := os.Stat(filepath.Join(b, "venv", "bin", "pip"))
	if !src.ModTime().Equal(dst.ModTime()) {
		t.Errorf("modification time %v, want %v", dst.ModTime(), src.ModTime())
	}
}

// An emergency stop doesn't save: a box resumed on its own machine has newer files than storage.
func TestARestoreKeepsALocalFileNewerThanTheStoredOne(t *testing.T) {
	f := newFakeKrater(t)
	ctx := context.Background()
	a := t.TempDir()
	write(t, filepath.Join(a, "newer.txt"), "saved", 0o644)
	write(t, filepath.Join(a, "older.txt"), "saved", 0o644)
	sa := workspaceSyncer(f, a)
	sa.Restore(ctx)
	if _, err := sa.Push(ctx); err != nil {
		t.Fatal(err)
	}

	b := t.TempDir()
	write(t, filepath.Join(b, "newer.txt"), "worked on after the save", 0o644)
	future := time.Now().Add(time.Hour)
	os.Chtimes(filepath.Join(b, "newer.txt"), future, future)
	write(t, filepath.Join(b, "older.txt"), "stale", 0o644)
	past := time.Now().Add(-time.Hour)
	os.Chtimes(filepath.Join(b, "older.txt"), past, past)
	if _, err := workspaceSyncer(f, b).Restore(ctx); err != nil {
		t.Fatal(err)
	}

	if got, _ := os.ReadFile(filepath.Join(b, "newer.txt")); string(got) != "worked on after the save" {
		t.Errorf("newer.txt = %q", got)
	}
	if got, _ := os.ReadFile(filepath.Join(b, "older.txt")); string(got) != "saved" {
		t.Errorf("older.txt = %q", got)
	}
}

// A save after a restore that missed files mustn't store a manifest without them, or remove them from storage.
func TestASaveAfterAnIncompleteRestoreRemovesNothing(t *testing.T) {
	f := newFakeKrater(t)
	ctx := context.Background()
	a := t.TempDir()
	write(t, filepath.Join(a, "model.pt"), "weights", 0o644)
	sa := workspaceSyncer(f, a)
	sa.Restore(ctx)
	if _, err := sa.Push(ctx); err != nil {
		t.Fatal(err)
	}
	f.unreadable = "workspace/model.pt" // storage can't serve it for now

	b := t.TempDir()
	sb := workspaceSyncer(f, b)
	if _, err := sb.Restore(ctx); err == nil {
		t.Fatal("a restore missing a file said it was complete")
	}
	// Storage serves it again: the next save first finishes the restore, then saves everything.
	f.unreadable = ""
	write(t, filepath.Join(b, "new.txt"), "new", 0o644)
	if _, err := sb.Push(ctx); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(filepath.Join(b, "model.pt")); string(got) != "weights" {
		t.Errorf("model.pt = %q", got)
	}
	if len(f.deleted) != 0 || string(f.objects["workspace/model.pt"]) != "weights" {
		t.Errorf("deleted %v", f.deleted)
	}
	paths := map[string]bool{}
	for _, e := range storedManifest(t, f).Entries {
		paths[e.Path] = true
	}
	if !paths["model.pt"] || !paths["new.txt"] {
		t.Errorf("manifest %v", paths)
	}

	// Still missing on the next box: its new files are saved and named next to everything stored, nothing is
	// removed, and the save isn't reported as complete.
	f.unreadable = "workspace/model.pt"
	c := t.TempDir()
	sc := workspaceSyncer(f, c)
	sc.Restore(ctx)
	os.Remove(filepath.Join(c, "new.txt")) // looks deleted here, but the restore didn't finish
	write(t, filepath.Join(c, "other.txt"), "other", 0o644)
	if _, err := sc.Push(ctx); err == nil {
		t.Error("a save over an incomplete restore said it was complete")
	}
	if len(f.deleted) != 0 {
		t.Errorf("deleted %v", f.deleted)
	}
	paths = map[string]bool{}
	for _, e := range storedManifest(t, f).Entries {
		paths[e.Path] = true
	}
	if !paths["model.pt"] || !paths["new.txt"] || !paths["other.txt"] {
		t.Errorf("manifest %v", paths)
	}

	// The box after that, with storage back, gets all of it, and its first save deletes nothing.
	f.unreadable = ""
	d := t.TempDir()
	sd := workspaceSyncer(f, d)
	if _, err := sd.Restore(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := sd.Push(ctx); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"model.pt": "weights", "new.txt": "new", "other.txt": "other"} {
		if got, _ := os.ReadFile(filepath.Join(d, name)); string(got) != want {
			t.Errorf("%s = %q", name, got)
		}
	}
	if len(f.deleted) != 0 {
		t.Errorf("deleted %v", f.deleted)
	}
}

// A save where one upload failed still names the files it stored, and the failed file's last stored copy.
func TestAPartlyFailedSaveStillNamesWhatItStored(t *testing.T) {
	f := newFakeKrater(t)
	ctx := context.Background()
	a := t.TempDir()
	write(t, filepath.Join(a, "big.bin"), "v1", 0o644)
	sa := workspaceSyncer(f, a)
	sa.Restore(ctx)
	if _, err := sa.Push(ctx); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(a, "big.bin"), "v2 refused", 0o644)
	write(t, filepath.Join(a, "small.txt"), "stored", 0o644)
	f.refuse = "big.bin"
	if _, err := sa.Push(ctx); err == nil {
		t.Fatal("a save with a refused file said it was complete")
	}

	f.refuse = ""
	b := t.TempDir()
	if _, err := workspaceSyncer(f, b).Restore(ctx); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{"big.bin": "v1", "small.txt": "stored"} {
		if got, _ := os.ReadFile(filepath.Join(b, name)); string(got) != want {
			t.Errorf("%s = %q", name, got)
		}
	}
}

// An older save without a manifest, restored only in part: a save names the stored files it hasn't brought back.
func TestAnIncompleteRestoreOfAnOlderSaveKeepsItsFiles(t *testing.T) {
	f := newFakeKrater(t)
	ctx := context.Background()
	f.objects["workspace/a.txt"] = []byte("a")
	f.objects["workspace/b.txt"] = []byte("b")
	f.unreadable = "workspace/b.txt"
	c := t.TempDir()
	sc := workspaceSyncer(f, c)
	sc.Restore(ctx)
	write(t, filepath.Join(c, "c.txt"), "c", 0o644)
	sc.Push(ctx)

	f.unreadable = ""
	d := t.TempDir()
	sd := workspaceSyncer(f, d)
	if _, err := sd.Restore(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := sd.Push(ctx); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"a.txt", "b.txt", "c.txt"} {
		if got, _ := os.ReadFile(filepath.Join(d, name)); string(got) != name[:1] {
			t.Errorf("%s = %q", name, got)
		}
		if info, err := os.Stat(filepath.Join(d, name)); err != nil || info.Mode().Perm() != 0o644 {
			t.Errorf("%s: %v", name, err)
		}
	}
	if len(f.deleted) != 0 {
		t.Errorf("deleted %v", f.deleted)
	}
}

// A file the manifest names but storage doesn't hold can't be lost by a save, so it doesn't block saves forever.
func TestAFileMissingFromStorageDoesNotBlockSaves(t *testing.T) {
	f := newFakeKrater(t)
	ctx := context.Background()
	a := t.TempDir()
	write(t, filepath.Join(a, "lost.txt"), "lost", 0o644)
	write(t, filepath.Join(a, "kept.txt"), "kept", 0o644)
	sa := workspaceSyncer(f, a)
	sa.Restore(ctx)
	if _, err := sa.Push(ctx); err != nil {
		t.Fatal(err)
	}
	delete(f.objects, "workspace/lost.txt")

	b := t.TempDir()
	sb := workspaceSyncer(f, b)
	if _, err := sb.Restore(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := sb.Push(ctx); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(filepath.Join(b, "kept.txt")); string(got) != "kept" {
		t.Errorf("kept.txt = %q", got)
	}
}

// Workspaces saved before the manifest come back as files, and the next save adds one.
func TestAWorkspaceSavedWithoutAManifestStillRestores(t *testing.T) {
	f := newFakeKrater(t)
	ctx := context.Background()
	f.objects["workspace/notes/a.txt"] = []byte("old save")
	b := t.TempDir()
	s := workspaceSyncer(f, b)
	if n, err := s.Restore(ctx); err != nil || n != 1 {
		t.Fatalf("Restore = %d, %v", n, err)
	}
	if got, _ := os.ReadFile(filepath.Join(b, "notes", "a.txt")); string(got) != "old save" {
		t.Errorf("a.txt = %q", got)
	}
	if _, err := s.Push(ctx); err != nil {
		t.Fatal(err)
	}
	if m := storedManifest(t, f); len(m.Entries) != 2 || len(f.deleted) != 0 {
		t.Errorf("manifest %v, deleted %v", m.Entries, f.deleted)
	}
}

func TestARestoreDoesNotWriteThroughASymlinkedFolder(t *testing.T) {
	f := newFakeKrater(t)
	ctx := context.Background()
	a := t.TempDir()
	write(t, filepath.Join(a, "sub", "x.txt"), "x", 0o644)
	sa := workspaceSyncer(f, a)
	sa.Restore(ctx)
	if _, err := sa.Push(ctx); err != nil {
		t.Fatal(err)
	}

	b, outside := t.TempDir(), t.TempDir()
	os.Symlink(outside, filepath.Join(b, "sub"))
	workspaceSyncer(f, b).Restore(ctx)
	if _, err := os.Stat(filepath.Join(outside, "x.txt")); !os.IsNotExist(err) {
		t.Errorf("wrote outside the workspace: %v", err)
	}
}

func TestStoredNamesKraterRefusesAreHashed(t *testing.T) {
	for path, plain := range map[string]bool{
		"venv/bin/python3.12": true, "data/a b.csv": true, "sub/.krater-x": true, "-x": true,
		"report (1).txt": false, ".krater-workspace.json": false, ".krater-files/x": false, " lead": false,
		"tail ": false, "naïve.txt": false, "a\nb": false,
	} {
		got := storedNameFor(path)
		if (got == path) != plain || (!plain && !strings.HasPrefix(got, encodedPrefix)) {
			t.Errorf("%q stored as %q", path, got)
		}
	}
}
