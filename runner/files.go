package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Syncer keeps a local directory and the job's stored area ("checkpoints" or "outputs") in step. Files go up only
// through presigned URLs Krater hands out for this job; the machine never holds storage credentials.
type Syncer struct {
	API     *API
	Area    string
	Dir     string
	Storage *http.Client
	// Manifest keeps folders, symlinks, permissions and deletions too (a box's /workspace; see workspace.go).
	Manifest bool
	// What was last uploaded, by relative name: size and modification time.
	uploaded map[string]fileStamp
	// With Manifest: the manifest last stored, and whether the stored workspace was fully restored.
	lastManifest []byte
	restored     bool
	// With Manifest: the entries of the manifest last read or stored (manifestRead: one was read or there was none),
	// and whether storage has been pruned to it.
	storedEntries []manifestEntry
	manifestRead  bool
	pruned        bool
	// One push at a time: the timed sync and a save Krater asks for can overlap.
	mu sync.Mutex
}

type fileStamp struct {
	size    int64
	modTime int64 // Unix nanoseconds
}

func stampOf(info fs.FileInfo) fileStamp {
	return fileStamp{info.Size(), info.ModTime().UnixNano()}
}

func NewSyncer(api *API, area, dir string, storage *http.Client) *Syncer {
	return &Syncer{API: api, Area: area, Dir: dir, Storage: storage, uploaded: map[string]fileStamp{}}
}

// Restore downloads every stored file that isn't already here at the same size (with Manifest: see restore).
func (s *Syncer) Restore(ctx context.Context) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.Manifest {
		return s.restore(ctx, false)
	}
	files, err := s.API.List(ctx, s.Area)
	if err != nil {
		return 0, err
	}
	return s.restoreFiles(ctx, files, false)
}

// restoreFiles downloads each listed file that isn't here at the same size; with `onlyMissing`, only those not
// here at all.
func (s *Syncer) restoreFiles(ctx context.Context, files []RemoteFile, onlyMissing bool) (int, error) {
	var names []string
	var targets []pendingDownload
	for _, f := range files {
		local, err := s.localPath(f.Name)
		if err != nil {
			logf("skipping stored file %q: %v", f.Name, err)
			continue
		}
		if info, err := os.Stat(local); err == nil && (onlyMissing || info.Size() == f.Size) {
			if info.Size() == f.Size {
				s.uploaded[f.Name] = stampOf(info)
			}
			continue
		}
		names = append(names, f.Name)
		targets = append(targets, pendingDownload{f.URL, local})
	}
	restored := 0
	var firstErr error
	for i, err := range s.downloadAll(ctx, targets) {
		if err != nil {
			if firstErr == nil {
				firstErr = fmt.Errorf("restoring %s: %w", names[i], err)
			}
			continue
		}
		if info, err := os.Stat(targets[i].local); err == nil {
			s.uploaded[names[i]] = stampOf(info)
		}
		restored++
	}
	return restored, firstErr
}

// transferWorkers is how many files go up or down at once. Small files are most of a workspace (a Python virtual
// environment is thousands), and each is a round trip to storage.
var transferWorkers = 16

// uploadBatch is how many uploads one call to Krater begins. Each call is a quota check over the whole project, so
// one per file was too slow; URLs last an hour, so a batch's are used well before they expire.
var uploadBatch = 250

type pendingUpload struct {
	name  string // stored name
	local string
	size  int64
}

type pendingDownload struct {
	url   string
	local string
}

// uploadAll uploads the files, beginning them a batch at a time and sending each batch's transferWorkers at a
// time. It returns each file's error (nil once stored) and the number of calls to Krater it made.
func (s *Syncer) uploadAll(ctx context.Context, files []pendingUpload) ([]error, int) {
	errs := make([]error, len(files))
	calls := 0
	for start := 0; start < len(files); start += uploadBatch {
		chunk := files[start:min(start+uploadBatch, len(files))]
		asks := make([]UploadAsk, len(chunk))
		for i, f := range chunk {
			asks[i] = UploadAsk{Name: f.name, Size: f.size}
		}
		calls++
		ups, err := s.API.BeginUploads(ctx, s.Area, asks)
		if err == nil && len(ups) != len(chunk) {
			err = fmt.Errorf("krater answered %d uploads for %d files", len(ups), len(chunk))
		}
		if err != nil {
			for i := range chunk {
				errs[start+i] = err
			}
			var denied *ErrDenied
			if errors.As(err, &denied) {
				for i := start + len(chunk); i < len(files); i++ {
					errs[i] = err
				}
				break
			}
			continue
		}
		parallel(len(chunk), func(i int) {
			up, f := ups[i], chunk[i]
			switch {
			case up.Error != "":
				errs[start+i] = errors.New(up.Error)
			case up.Name != f.name:
				errs[start+i] = fmt.Errorf("krater answered for %q", up.Name)
			default:
				errs[start+i] = s.send(ctx, f.name, f.local, f.size, &up.Upload)
			}
		})
	}
	return errs, calls
}

// downloadAll downloads the files transferWorkers at a time and returns each one's error.
func (s *Syncer) downloadAll(ctx context.Context, files []pendingDownload) []error {
	errs := make([]error, len(files))
	parallel(len(files), func(i int) {
		errs[i] = s.download(ctx, files[i].url, files[i].local)
	})
	return errs
}

// parallel calls fn(0) to fn(n-1), transferWorkers at a time, and returns when all have.
func parallel(n int, fn func(i int)) {
	work := make(chan int)
	var wg sync.WaitGroup
	for w := 0; w < min(transferWorkers, n); w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range work {
				fn(i)
			}
		}()
	}
	for i := 0; i < n; i++ {
		work <- i
	}
	close(work)
	wg.Wait()
}

// Push uploads every file that changed since it was last uploaded. Files still being written may change again;
// the next push picks them up.
func (s *Syncer) Push(ctx context.Context) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.Manifest {
		return s.pushWorkspace(ctx)
	}
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
		stamp := stampOf(info)
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
	todo := make([]pendingUpload, 0, len(names))
	for _, name := range names {
		local, _ := s.localPath(name) // from the walk: always inside the directory
		todo = append(todo, pendingUpload{name, local, stamps[name].size})
	}
	errs, _ := s.uploadAll(ctx, todo)
	pushed := 0
	var firstErr error
	for i, name := range names {
		if err := errs[i]; err != nil {
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

// uploadFile stores the local file `local` as `name`.
func (s *Syncer) uploadFile(ctx context.Context, name, local string, size int64) error {
	up, err := s.API.BeginUpload(ctx, s.Area, name, size)
	if err != nil {
		return err
	}
	return s.send(ctx, name, local, size, up)
}

// send uploads the file to the URLs Krater handed out for it, and finishes a multipart upload.
func (s *Syncer) send(ctx context.Context, name, local string, size int64, up *Upload) error {
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

// putTries is how many times a PUT is tried when storage is briefly unavailable or rate-limits (429, 5xx).
const putTries = 3

// put uploads `length` bytes of `local` from `offset` and returns the ETag storage answered with.
func (s *Syncer) put(ctx context.Context, url, local string, offset, length int64) (string, error) {
	wait := retryFirstWait / 4
	for try := 1; ; try++ {
		etag, status, err := s.putOnce(ctx, url, local, offset, length)
		if err == nil || try == putTries || !(status == http.StatusTooManyRequests || status >= 500) {
			return etag, err
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(wait):
		}
		wait *= 2
	}
}

func (s *Syncer) putOnce(ctx context.Context, url, local string, offset, length int64) (string, int, error) {
	file, err := os.Open(local)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	body := io.NewSectionReader(file, offset, length)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, body)
	if err != nil {
		return "", 0, err
	}
	req.ContentLength = length
	resp, err := s.Storage.Do(req)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode/100 != 2 {
		return "", resp.StatusCode, fmt.Errorf("storage answered %d", resp.StatusCode)
	}
	return resp.Header.Get("ETag"), resp.StatusCode, nil
}

func isPartial(name string) bool {
	for _, suffix := range []string{".tmp", ".partial", ".krater-partial"} {
		if strings.HasSuffix(name, suffix) {
			return true
		}
	}
	return false
}
