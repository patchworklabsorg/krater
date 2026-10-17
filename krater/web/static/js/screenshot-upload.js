/*
 * Screenshot upload widget for the completion-draft edit page. Vanilla JS, no build step, no CDN
 * dependencies -- served straight from /static (see krater/web/app.py's StaticFiles mount).
 *
 * Flow per file, matching krater/services/screenshots.py:
 *   1. POST <presign-url> (this app) -- returns a presigned S3 POST policy for a server-chosen key.
 *   2. POST straight to that policy's `url` (object storage, a *different* origin from this app in
 *      production -- see docs/dev/storage.md's CORS section; this is why step 2 is a plain `fetch`
 *      with no credentials, not a same-origin form submit).
 *   3. POST <confirm-url> (this app) -- tells Krater the object exists, so it gets appended to the
 *      draft's screenshot_keys after a server-side re-check (content type, size).
 *   4. Reload the page so the new thumbnail renders from the server-rendered list (also picks up a
 *      fresh presigned GET URL for every existing thumbnail, which are short-lived).
 *
 * There's no partial/AJAX re-render on purpose: this widget is a small enhancement over a page that
 * already works without it (see the <noscript> message next to it in edit.html) -- removal is a plain
 * form post, and reloading after an upload keeps this file simple.
 */
(function () {
  "use strict";

  function init() {
    var widget = document.getElementById("screenshot-widget");
    if (!widget) return;

    var input = widget.querySelector("[data-role=file-input]");
    var status = widget.querySelector("[data-role=status]");
    var presignUrl = widget.getAttribute("data-presign-url");
    var confirmUrl = widget.getAttribute("data-confirm-url");
    var csrfToken = widget.getAttribute("data-csrf-token");
    var maxScreenshots = parseInt(widget.getAttribute("data-max-screenshots"), 10) || 6;
    var maxBytes = parseInt(widget.getAttribute("data-max-bytes"), 10) || 5 * 1024 * 1024;
    var remaining = parseInt(widget.getAttribute("data-remaining"), 10);
    if (isNaN(remaining)) remaining = maxScreenshots;

    var ALLOWED_TYPES = { "image/png": true, "image/jpeg": true, "image/webp": true };

    function setStatus(message, isError) {
      status.textContent = message || "";
      status.classList.toggle("field-error", !!isError);
    }

    function formBody(fields) {
      var params = new URLSearchParams();
      Object.keys(fields).forEach(function (name) {
        params.append(name, fields[name]);
      });
      return params;
    }

    function presign(file) {
      return fetch(presignUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formBody({ csrf_token: csrfToken, content_type: file.type }),
      }).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) throw new Error((data.errors && data.errors.screenshot) || "Could not start the upload.");
          return data;
        });
      });
    }

    function uploadToStorage(presigned, file) {
      var form = new FormData();
      Object.keys(presigned.fields).forEach(function (name) {
        form.append(name, presigned.fields[name]);
      });
      // The file field must come last: S3 (and S3-compatible servers) ignore POST fields after it.
      form.append("file", file);
      return fetch(presigned.url, { method: "POST", body: form }).then(function (response) {
        if (!response.ok && response.status !== 204) {
          throw new Error("Upload to storage failed (" + response.status + ").");
        }
      });
    }

    function confirm(key) {
      return fetch(confirmUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formBody({ csrf_token: csrfToken, key: key }),
      }).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) throw new Error((data.errors && data.errors.screenshot) || "Could not confirm the upload.");
          return data;
        });
      });
    }

    function uploadOne(file) {
      if (!ALLOWED_TYPES[file.type]) {
        return Promise.reject(new Error(file.name + ": only PNG, JPEG or WebP screenshots are allowed."));
      }
      if (file.size > maxBytes) {
        return Promise.reject(new Error(file.name + ": file is too large."));
      }
      return presign(file).then(function (presigned) {
        return uploadToStorage(presigned, file).then(function () {
          return confirm(presigned.key);
        });
      });
    }

    function handleFiles(files) {
      var list = Array.prototype.slice.call(files);
      if (list.length === 0) return;
      if (list.length > remaining) {
        setStatus("Only " + remaining + " more screenshot(s) can be added.", true);
        return;
      }

      input.disabled = true;
      setStatus("Uploading " + list.length + " screenshot(s)…", false);

      var chain = list.reduce(function (promise, file) {
        return promise.then(function () {
          return uploadOne(file);
        });
      }, Promise.resolve());

      chain
        .then(function () {
          setStatus("Uploaded. Refreshing…", false);
          window.location.reload();
        })
        .catch(function (err) {
          input.disabled = false;
          setStatus(err.message || "Upload failed.", true);
        });
    }

    input.addEventListener("change", function () {
      handleFiles(input.files);
      input.value = "";
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
