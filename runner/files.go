package main

import (
	"context"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Syncer keeps a local directory and the job's stored area ("checkpoints" or "outputs") in step. Files go up only
// through presigned URLs Krater hands out for this job; the machine never holds storage credentials.
type Syncer struct {
	API     *API
	Area    string
	Dir     string
	Storage *http.Client
	// What was last uploaded, by relative name: size and modification time.
	uploaded map[string]fileStamp
}

type fileStamp struct {
	size    int64
	modTime time.Time
}

func NewSyncer(api *API, area, dir string, storage *http.Client) *Syncer {
	return &Syncer{API: api, Area: area, Dir: dir, Storage: storage, uploaded: map[string]fileStamp{}}
}

// Restore downloads every stored file that isn't already here at the same size.
func (s *Syncer) Restore(ctx context.Context) (int, error) {
	files, err := s.API.List(ctx, s.Area)
	if err != nil {
		return 0, err
	}
	restored := 0
	for _, f := range files {
		local, err := s.localPath(f.Name)
		if err != nil {
			logf("skipping stored file %q: %v", f.Name, err)
			continue
		}
		if info, err := os.Stat(local); err == nil && info.Size() == f.Size {
			s.uploaded[f.Name] = fileStamp{info.Size(), info.ModTime()}
			continue
		}
		if err := s.download(ctx, f.URL, local); err != nil {
			return restored, fmt.Errorf("restoring %s: %w", f.Name, err)
		}
		if info, err := os.Stat(local); err == nil {
			s.uploaded[f.Name] = fileStamp{info.Size(), info.ModTime()}
		}
		restored++
	}
	return restored, nil
}

// Push uploads every file that changed since it was last uploaded. Files still being written may change again;
// the next push picks them up.
func (s *Syncer) Push(ctx context.Context) (int, error) {
	var names []string
	stamps := map[string]fileStamp{}
	err := filepath.WalkDir(s.Dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			if os.IsNotExist(err) {
				return nil
			}
			return err
		}
		if d.IsDir() || !d.Type().IsRegular() {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		rel, err := filepath.Rel(s.Dir, path)
		if err != nil {
			return nil
		}
		name := filepath.ToSlash(rel)
		if isPartial(name) {
			// Still being written (the usual pattern: write to a temporary name, then rename).
			return nil
		}
		stamp := fileStamp{info.Size(), info.ModTime()}
		if prev, ok := s.uploaded[name]; ok && prev == stamp {
			return nil
		}
		names = append(names, name)
		stamps[name] = stamp
		return nil
	})
	if err != nil {
		return 0, err
	}
	sort.Strings(names)
	pushed := 0
	var firstErr error
	for _, name := range names {
		if err := s.upload(ctx, name, stamps[name].size); err != nil {
			// Keep going: one refused file (the project's storage quota, say) mustn't stop the others.
			logf("uploading %s failed: %v", name, err)
			if firstErr == nil {
				firstErr = fmt.Errorf("uploading %s: %w", name, err)
			}
			continue
		}
		s.uploaded[name] = stamps[name]
		pushed++
	}
	return pushed, firstErr
}

func (s *Syncer) localPath(name string) (string, error) {
	clean := filepath.Clean(filepath.FromSlash(name))
	if filepath.IsAbs(clean) || clean == "." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) || clean == ".." {
		return "", fmt.Errorf("not a relative path")
	}
	return filepath.Join(s.Dir, clean), nil
}

func (s *Syncer) download(ctx context.Context, url, local string) error {
	if err := os.MkdirAll(filepath.Dir(local), 0o755); err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := s.Storage.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("storage answered %d", resp.StatusCode)
	}
	tmp := local + ".krater-partial"
	out, err := os.Create(tmp)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, resp.Body); err != nil {
		out.Close()
		os.Remove(tmp)
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, local)
}

func (s *Syncer) upload(ctx context.Context, name string, size int64) error {
	local, err := s.localPath(name)
	if err != nil {
		return err
	}
	up, err := s.API.BeginUpload(ctx, s.Area, name, size)
	if err != nil {
		return err
	}
	if up.URL != "" {
		_, err := s.put(ctx, up.URL, local, 0, size)
		return err
	}
	if up.PartBytes <= 0 || len(up.Parts) == 0 {
		return fmt.Errorf("krater sent no upload URL")
	}
	etags := make([]string, 0, len(up.Parts))
	for i, url := range up.Parts {
		offset := int64(i) * up.PartBytes
		length := up.PartBytes
		if offset+length > size {
			length = size - offset
		}
		etag, err := s.put(ctx, url, local, offset, length)
		if err != nil {
			return fmt.Errorf("part %d: %w", i+1, err)
		}
		etags = append(etags, etag)
	}
	return s.API.CompleteUpload(ctx, s.Area, name, up.UploadID, etags)
}

// put uploads `length` bytes of `local` from `offset` and returns the ETag storage answered with.
func (s *Syncer) put(ctx context.Context, url, local string, offset, length int64) (string, error) {
	file, err := os.Open(local)
	if err != nil {
		return "", err
	}
	defer file.Close()
	body := io.NewSectionReader(file, offset, length)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, body)
	if err != nil {
		return "", err
	}
	req.ContentLength = length
	resp, err := s.Storage.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode/100 != 2 {
		return "", fmt.Errorf("storage answered %d", resp.StatusCode)
	}
	return resp.Header.Get("ETag"), nil
}

func isPartial(name string) bool {
	for _, suffix := range []string{".tmp", ".partial", ".krater-partial"} {
		if strings.HasSuffix(name, suffix) {
			return true
		}
	}
	return false
}
