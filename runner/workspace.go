package main

// A box's /workspace is kept as more than its files' bytes. Plain file copies (checkpoints, outputs) lose each file's
// permissions, every symlink and every empty folder, and a file deleted on the machine stays in storage and comes back
// on the next restore. A Python virtual environment in /workspace, which the guide recommends, needs all of those, so
// a box moved to another machine got a broken one.
//
// So each save of a workspace also stores a manifest (manifestName): every path under it, with its type, permission
// bits, size and modification time, and a symlink's target. A restore follows the manifest, and once one is stored,
// files deleted on the machine are removed from storage too. A path the store can't name (it allows letters, digits
// and a few marks) is stored under a hashed name in encodedPrefix, so one odd file name no longer fails every save.
//
// On the machine, the newer copy wins: a restore writes a stored file only where the local one is missing or older.
// A box resumed on its own machine after a stop that didn't save (an emergency stop, say) keeps its newer work.

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// manifestName is the stored manifest of a workspace. Paths starting ".krater-" at its top are Krater's own, so a
// member's file with such a name is stored under a hashed name.
const manifestName = ".krater-workspace.json"

const encodedPrefix = ".krater-files/"

// storableName mirrors Krater's rule for stored names (runner_api._NAME_RE and check_name).
var storableName = regexp.MustCompile(`^[A-Za-z0-9._\-=+,@~][A-Za-z0-9._\-=+,@~ /]{0,511}$`)

// keptMode is the part of a file's mode a workspace keeps: permissions, and setuid, setgid and sticky.
const keptMode = fs.ModePerm | fs.ModeSetuid | fs.ModeSetgid | fs.ModeSticky

// deleteBatch is how many stored names one delete call carries (Krater takes up to 1000).
const deleteBatch = 500

type manifestEntry struct {
	Path   string      `json:"path"`
	Type   string      `json:"type"` // "file", "dir" or "link"
	Mode   fs.FileMode `json:"mode,omitempty"`
	Size   int64       `json:"size,omitempty"`
	MTime  int64       `json:"mtime,omitempty"` // Unix nanoseconds
	Stored string      `json:"stored,omitempty"`
	Target string      `json:"target,omitempty"`
}

type workspaceManifest struct {
	Version int             `json:"version"`
	Entries []manifestEntry `json:"entries"`
}

// storedNameFor is the name a workspace file is stored under: its own path when Krater accepts it, else a hash.
func storedNameFor(path string) string {
	ok := storableName.MatchString(path) && path == strings.TrimSpace(path) && !strings.HasPrefix(path, ".krater-")
	for _, segment := range strings.Split(path, "/") {
		if segment == "" || segment == "." || segment == ".." {
			ok = false
		}
	}
	if ok {
		return path
	}
	sum := sha256.Sum256([]byte(path))
	return encodedPrefix + hex.EncodeToString(sum[:20])
}

// scanWorkspace lists everything under the directory, sorted by path, without following symlinks.
func (s *Syncer) scanWorkspace() ([]manifestEntry, error) {
	var entries []manifestEntry
	err := filepath.WalkDir(s.Dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if os.IsNotExist(err) {
				return nil
			}
			return err
		}
		if path == s.Dir {
			return nil
		}
		rel, err := filepath.Rel(s.Dir, path)
		if err != nil {
			return nil
		}
		name := filepath.ToSlash(rel)
		info, err := d.Info()
		if err != nil {
			return nil
		}
		switch {
		case d.Type()&fs.ModeSymlink != 0:
			target, err := os.Readlink(path)
			if err != nil {
				return nil
			}
			entries = append(entries, manifestEntry{Path: name, Type: "link", Target: target})
		case d.IsDir():
			entries = append(entries, manifestEntry{Path: name, Type: "dir", Mode: info.Mode() & keptMode})
		case d.Type().IsRegular():
			if isPartial(name) {
				return nil
			}
			entries = append(entries, manifestEntry{
				Path: name, Type: "file", Mode: info.Mode() & keptMode, Size: info.Size(),
				MTime: info.ModTime().UnixNano(), Stored: storedNameFor(name),
			})
		}
		return nil
	})
	sort.Slice(entries, func(i, j int) bool { return entries[i].Path < entries[j].Path })
	return entries, err
}

// pushWorkspace saves the workspace: changed files, then the manifest, then removes from storage what the
// manifest no longer names.
//
// The manifest names only what storage holds: a file whose upload failed keeps its stored entry, if it has one. And
// until a restore has completed it keeps every stored entry too, since the files still missing here aren't deleted,
// only not back yet. Only a save that uploaded everything over a complete restore removes anything from storage.
func (s *Syncer) pushWorkspace(ctx context.Context) (int, error) {
	if !s.restored {
		if _, err := s.restore(ctx, true); err != nil {
			logf("the saved /workspace still isn't fully restored, so nothing is removed from storage: %v", err)
		}
	}
	entries, err := s.scanWorkspace()
	if err != nil {
		return 0, err
	}
	stored := map[string]manifestEntry{}
	for _, e := range s.storedEntries {
		stored[e.Path] = e
	}
	started := time.Now()
	current := map[string]bool{}
	var todo []pendingUpload
	upload := map[string]int{} // path -> index in todo
	for _, e := range entries {
		if e.Type != "file" {
			continue
		}
		current[e.Path] = true
		if prev, ok := s.uploaded[e.Path]; ok && prev == (fileStamp{e.Size, e.MTime}) {
			continue
		}
		upload[e.Path] = len(todo)
		todo = append(todo, pendingUpload{e.Stored, filepath.Join(s.Dir, filepath.FromSlash(e.Path)), e.Size})
	}
	errs, calls := s.uploadAll(ctx, todo)
	pushed := 0
	var savedBytes int64
	var firstErr error
	manifest := make([]manifestEntry, 0, len(entries))
	for _, e := range entries {
		i, sent := upload[e.Path]
		if !sent {
			manifest = append(manifest, e)
			continue
		}
		if err := errs[i]; err != nil {
			logf("uploading %s failed: %v", e.Path, err)
			if firstErr == nil {
				firstErr = fmt.Errorf("uploading %s: %w", e.Path, err)
			}
			if old, ok := stored[e.Path]; ok && old.Type == "file" {
				manifest = append(manifest, old)
			}
			continue
		}
		s.uploaded[e.Path] = fileStamp{e.Size, e.MTime}
		manifest = append(manifest, e)
		pushed++
		savedBytes += e.Size
	}
	if len(todo) > 0 {
		logf("saved %d of %d changed workspace files (%d bytes) in %s, %d upload calls",
			pushed, len(todo), savedBytes, time.Since(started).Round(time.Millisecond), calls)
	}
	for path := range s.uploaded {
		if !current[path] {
			delete(s.uploaded, path)
		}
	}
	complete := firstErr == nil && s.restored
	if !s.restored {
		if s.storedEntries == nil && !s.manifestRead {
			// The stored manifest couldn't be read: one written now would replace it without its entries. The
			// uploaded files are stored; the next save over a complete restore names them.
			return pushed, errors.New("the saved /workspace couldn't be read, so its manifest isn't replaced")
		}
		listed := map[string]bool{}
		for _, e := range manifest {
			listed[e.Path] = true
		}
		for _, e := range s.storedEntries {
			if !listed[e.Path] {
				manifest = append(manifest, e)
			}
		}
		sort.Slice(manifest, func(i, j int) bool { return manifest[i].Path < manifest[j].Path })
	}
	data, err := json.Marshal(workspaceManifest{Version: 1, Entries: manifest})
	if err != nil {
		return pushed, err
	}
	if !bytes.Equal(data, s.lastManifest) {
		if err := s.uploadBytes(ctx, manifestName, data); err != nil {
			return pushed, fmt.Errorf("uploading the workspace manifest: %w", err)
		}
		s.lastManifest, s.storedEntries, s.pruned = data, manifest, false
	}
	if firstErr != nil {
		return pushed, firstErr
	}
	if !complete {
		return pushed, errors.New("the saved /workspace isn't fully restored yet, so nothing was removed from storage")
	}
	if !s.pruned {
		s.pruned = s.pruneStored(ctx, manifest)
	}
	return pushed, nil
}

// pruneStored removes stored files the manifest doesn't name: deleted on the machine, or renamed. A failure only
// leaves them in storage, where a restore ignores them.
func (s *Syncer) pruneStored(ctx context.Context, entries []manifestEntry) bool {
	keep := map[string]bool{manifestName: true}
	for _, e := range entries {
		if e.Type == "file" {
			keep[e.Stored] = true
		}
	}
	remote, err := s.API.List(ctx, s.Area)
	if err != nil {
		logf("couldn't list the stored workspace to remove deleted files: %v", err)
		return false
	}
	var stale []string
	for _, f := range remote {
		if !keep[f.Name] {
			stale = append(stale, f.Name)
		}
	}
	for start := 0; start < len(stale); start += deleteBatch {
		end := min(start+deleteBatch, len(stale))
		if err := s.API.Delete(ctx, s.Area, stale[start:end]); err != nil {
			logf("removing %d deleted files from storage failed: %v", end-start, err)
			return false
		}
	}
	if len(stale) > 0 {
		logf("removed %d deleted workspace files from storage", len(stale))
	}
	return true
}

// restore brings the stored workspace back. With a manifest it recreates folders, files (with their permissions
// and times) and symlinks; without one (saved by an older runner) it copies the files as before. `onlyMissing`
// leaves every existing local file alone (a retry while the box is in use).
func (s *Syncer) restore(ctx context.Context, onlyMissing bool) (int, error) {
	remote, err := s.API.List(ctx, s.Area)
	if err != nil {
		return 0, err
	}
	urls := map[string]RemoteFile{}
	for _, f := range remote {
		urls[f.Name] = f
	}
	stored, ok := urls[manifestName]
	if !ok {
		// Saved by an older runner, or nothing saved yet: plain files. A failed restore here still lets a save
		// write a manifest, since what isn't back yet is kept by naming every stored file in it below.
		n, err := s.restoreFiles(ctx, remote, onlyMissing)
		s.restored = err == nil
		if !s.restored {
			s.manifestRead = true
			s.storedEntries = nil
			for _, f := range remote {
				if storedNameFor(f.Name) == f.Name {
					s.storedEntries = append(s.storedEntries, manifestEntry{
						Path: f.Name, Type: "file", Mode: 0o644, Size: f.Size, Stored: f.Name,
					})
				}
			}
		}
		return n, err
	}
	data, err := s.fetch(ctx, stored.URL)
	if err != nil {
		return 0, fmt.Errorf("reading the workspace manifest: %w", err)
	}
	var m workspaceManifest
	if err := json.Unmarshal(data, &m); err != nil {
		return 0, fmt.Errorf("reading the workspace manifest: %w", err)
	}
	s.storedEntries, s.manifestRead = m.Entries, true
	restored := 0
	var firstErr error
	fail := func(path string, err error) {
		logf("restoring %s failed: %v", path, err)
		if firstErr == nil {
			firstErr = fmt.Errorf("restoring %s: %w", path, err)
		}
	}
	var dirs, links, fetches []manifestEntry
	var targets []pendingDownload
	for _, e := range m.Entries {
		local, err := s.localPath(e.Path)
		if err != nil {
			logf("skipping saved path %q: %v", e.Path, err)
			continue
		}
		if !s.throughNoSymlink(local) {
			logf("skipping saved path %q: a folder on its way is a symlink", e.Path)
			continue
		}
		switch e.Type {
		case "dir":
			if err := os.MkdirAll(local, 0o755); err != nil {
				fail(e.Path, err)
			}
			dirs = append(dirs, e)
		case "link":
			links = append(links, e)
		case "file":
			info, err := os.Lstat(local)
			if err == nil {
				same := info.Mode().IsRegular() && info.Size() == e.Size && info.ModTime().UnixNano() == e.MTime
				if same {
					s.uploaded[e.Path] = fileStamp{e.Size, e.MTime}
					os.Chmod(local, e.Mode)
				}
				// Anything else already here is newer, or isn't a file: the machine's copy wins.
				if same || onlyMissing || !info.Mode().IsRegular() || info.ModTime().UnixNano() >= e.MTime {
					continue
				}
			} else if !os.IsNotExist(err) {
				fail(e.Path, err)
				continue
			}
			file, ok := urls[e.Stored]
			if !ok {
				// Not stored at all, so no save can lose it: counting it as a failure would block every save.
				logf("skipping saved path %q: it isn't in storage", e.Path)
				continue
			}
			fetches = append(fetches, e)
			targets = append(targets, pendingDownload{file.URL, local})
		}
	}
	started := time.Now()
	errs := s.downloadAll(ctx, targets)
	for i, e := range fetches {
		if errs[i] != nil {
			fail(e.Path, errs[i])
			continue
		}
		local := targets[i].local
		os.Chmod(local, e.Mode)
		if e.MTime != 0 { // 0: named from an older save, with no time
			mtime := time.Unix(0, e.MTime)
			os.Chtimes(local, mtime, mtime)
		}
		if info, err := os.Stat(local); err == nil {
			s.uploaded[e.Path] = stampOf(info) // as stored here: some filesystems round the time
		}
		restored++
	}
	if len(targets) > 0 {
		logf("downloaded %d of %d workspace files in %s", restored, len(targets), time.Since(started).Round(time.Millisecond))
	}
	for _, e := range links {
		local, _ := s.localPath(e.Path)
		if _, err := os.Lstat(local); err == nil {
			continue
		}
		if err := os.MkdirAll(filepath.Dir(local), 0o755); err != nil {
			fail(e.Path, err)
			continue
		}
		if err := os.Symlink(e.Target, local); err != nil {
			fail(e.Path, err)
			continue
		}
		restored++
	}
	// Deepest first, so a folder without write permission is set after what's inside it.
	sort.Slice(dirs, func(i, j int) bool { return dirs[i].Path > dirs[j].Path })
	for _, e := range dirs {
		local, _ := s.localPath(e.Path)
		os.Chmod(local, e.Mode)
	}
	s.restored = firstErr == nil
	if s.restored {
		s.lastManifest, s.pruned = nil, false // written again on the next save, which also prunes
	}
	return restored, firstErr
}

// throughNoSymlink reports whether no folder between the directory and `local` is a symlink, so a restore can't
// write outside the directory through one.
func (s *Syncer) throughNoSymlink(local string) bool {
	rel, err := filepath.Rel(s.Dir, filepath.Dir(local))
	if err != nil || rel == "." {
		return err == nil
	}
	path := s.Dir
	for _, part := range strings.Split(rel, string(filepath.Separator)) {
		path = filepath.Join(path, part)
		info, err := os.Lstat(path)
		if err != nil {
			return true // not there yet: MkdirAll makes a real folder
		}
		if info.Mode()&fs.ModeSymlink != 0 {
			return false
		}
	}
	return true
}

// uploadBytes stores `data` as `name`, through a temporary file outside the directory.
func (s *Syncer) uploadBytes(ctx context.Context, name string, data []byte) error {
	tmp, err := os.CreateTemp("", "krater-upload-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return s.uploadFile(ctx, name, tmp.Name(), int64(len(data)))
}

// fetch reads a stored file into memory (the manifest: small).
func (s *Syncer) fetch(ctx context.Context, url string) ([]byte, error) {
	tmp, err := os.CreateTemp("", "krater-download-*")
	if err != nil {
		return nil, err
	}
	tmp.Close()
	defer os.Remove(tmp.Name())
	if err := s.download(ctx, url, tmp.Name()); err != nil {
		return nil, err
	}
	return os.ReadFile(tmp.Name())
}
