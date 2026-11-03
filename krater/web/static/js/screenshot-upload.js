/*
 * Image upload widgets for the completion-draft edit page: the primary image, and the images the final
 * write-up shows inline. Vanilla JS, no build step, no CDN dependencies -- served straight from /static
 * (see krater/web/app.py's StaticFiles mount).
 *
 * Flow per file, matching krater/services/screenshots.py:
 *   1. POST <presign-url> (this app) with the widget's role -- returns a presigned S3 POST policy for a
 *      server-chosen key.
 *   2. POST straight to that policy's `url` (object storage, a *different* origin from this app in
 *      production -- see docs/dev/storage.md's CORS section; this is why step 2 is a plain `fetch`
 *      with no credentials, not a same-origin form submit).
 *   3. POST <confirm-url> (this app) -- tells Krater the object exists, so it becomes the primary image
 *      or joins the inline images after a server-side re-check (content type, size, signature). The
 *      answer carries a thumbnail URL and, for an inline image, the path its Markdown names.
 *
 * Nothing reloads the page: it holds the form's unsaved edits. A primary image swaps in place; an inline
 * image is added to the list and its Markdown goes in the write-up where the cursor was. Remove posts the
 * same form a browser without JavaScript would, asking for JSON back.
 */
(function () {
  "use strict";

  var ALLOWED_TYPES = { "image/png": true, "image/jpeg": true, "image/webp": true };
  var ALT_PLACEHOLDER = "Describe the image";

  function formBody(fields) {
    var params = new URLSearchParams();
    Object.keys(fields).forEach(function (name) {
      params.append(name, fields[name]);
    });
    return params;
  }

  function postForm(url, fields) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body: formBody(fields),
    }).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          if (!response.ok) throw new Error((data.errors && data.errors.screenshot) || "Something went wrong. Try again.");
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

  function setup(widget) {
    var role = widget.getAttribute("data-image-widget");
    var input = widget.querySelector("[data-role=file-input]");
    var status = widget.querySelector("[data-role=status]");
    var csrfToken = widget.getAttribute("data-csrf-token");
    var presignUrl = widget.getAttribute("data-presign-url");
    var confirmUrl = widget.getAttribute("data-confirm-url");
    var deleteUrl = widget.getAttribute("data-delete-url");
    var maxBytes = parseInt(widget.getAttribute("data-max-bytes"), 10) || 5 * 1024 * 1024;
    var maxCount = parseInt(widget.getAttribute("data-max-count"), 10) || 0;
    var list = widget.querySelector("[data-role=list]");
    var textarea = document.getElementById(widget.getAttribute("data-textarea") || "");

    widget.querySelector("[data-role=controls]").hidden = false;

    function setStatus(message, isError) {
      status.textContent = message || "";
      status.classList.toggle("field-error", !!isError);
    }

    function removeUrl(key) {
      return deleteUrl + key + "/delete";
    }

    function uploadOne(file) {
      if (!ALLOWED_TYPES[file.type]) {
        return Promise.reject(new Error(file.name + ": only PNG, JPEG or WebP images are allowed."));
      }
      if (file.size > maxBytes) {
        return Promise.reject(new Error(file.name + ": the file is over 5 MB."));
      }
      return postForm(presignUrl, { csrf_token: csrfToken, content_type: file.type, role: role }).then(function (presigned) {
        return uploadToStorage(presigned, file).then(function () {
          return postForm(confirmUrl, { csrf_token: csrfToken, key: presigned.key, role: role });
        });
      });
    }

    // ── Primary image ──

    function showPrimary(image) {
      var current = widget.querySelector("[data-role=current]");
      current.querySelector("[data-role=thumb]").src = image ? image.url : "";
      current.querySelector("[data-role=remove-form]").action = image ? removeUrl(image.key) : "";
      current.hidden = !image;
    }

    function setupPrimary() {
      var removeForm = widget.querySelector("[data-role=remove-form]");
      removeForm.addEventListener("submit", function (event) {
        event.preventDefault();
        setStatus("Removing…", false);
        postForm(removeForm.action, { csrf_token: csrfToken })
          .then(function () {
            showPrimary(null);
            setStatus("Image removed.", false);
          })
          .catch(function (err) {
            setStatus(err.message, true);
          });
      });

      input.addEventListener("change", function () {
        var file = input.files[0];
        input.value = "";
        if (!file) return;
        input.disabled = true;
        setStatus("Uploading…", false);
        uploadOne(file)
          .then(function (image) {
            showPrimary(image);
            setStatus("Saved as the primary image.", false);
          })
          .catch(function (err) {
            setStatus(err.message, true);
          })
          .then(function () {
            input.disabled = false;
          });
      });
    }

    // ── Inline images ──

    function snippet(path) {
      return "![" + ALT_PLACEHOLDER + "](" + path + ")";
    }

    // Puts `path`'s Markdown in the write-up at the cursor, on a line of its own, and selects the alt text
    // so typing replaces it. Returns where the Markdown ends, for the next of a batch to go after it.
    function insert(path) {
      if (!textarea) return null;
      var text = textarea.value;
      var start = textarea.selectionStart;
      var end = textarea.selectionEnd;
      if (typeof start !== "number") start = end = text.length;
      var before = text.slice(0, start);
      var after = text.slice(end);
      var lead = before === "" || /\n\n$/.test(before) ? "" : /\n$/.test(before) ? "\n" : "\n\n";
      var trail = after === "" || /^\n\n/.test(after) ? "" : /^\n/.test(after) ? "\n" : "\n\n";
      var markdown = snippet(path);
      textarea.value = before + lead + markdown + trail + after;
      var altStart = before.length + lead.length + 2;
      textarea.focus();
      textarea.setSelectionRange(altStart, altStart + ALT_PLACEHOLDER.length);
      return before.length + lead.length + markdown.length;
    }

    function count() {
      return list.querySelectorAll("[data-key]").length;
    }

    function wireItem(item) {
      var insertButton = item.querySelector("[data-role=insert]");
      insertButton.hidden = false;
      insertButton.addEventListener("click", function () {
        insert(item.getAttribute("data-path"));
      });
      var removeButton = item.querySelector(".button--danger");
      removeButton.addEventListener("click", function (event) {
        event.preventDefault();
        setStatus("Removing…", false);
        postForm(removeUrl(item.getAttribute("data-key")), { csrf_token: csrfToken })
          .then(function () {
            item.remove();
            setStatus("Image removed. Take its Markdown out of the write-up too, or it shows as a broken image.", false);
          })
          .catch(function (err) {
            setStatus(err.message, true);
          });
      });
    }

    function addItem(image) {
      var item = document.createElement("li");
      item.className = "image-list__item";
      item.setAttribute("data-key", image.key);
      item.setAttribute("data-path", image.path);
      var thumb = document.createElement("img");
      thumb.className = "image-list__thumb";
      thumb.src = image.url;
      thumb.alt = "";
      var code = document.createElement("code");
      code.className = "image-list__snippet";
      code.textContent = snippet(image.path);
      var actions = document.createElement("span");
      actions.className = "image-list__actions";
      actions.innerHTML =
        '<button type="button" class="button button--small" data-role="insert" hidden>Insert</button>' +
        '<button type="button" class="button button--small button--danger">Remove</button>';
      item.appendChild(thumb);
      item.appendChild(code);
      item.appendChild(actions);
      list.appendChild(item);
      wireItem(item);
    }

    function setupInline() {
      Array.prototype.forEach.call(list.querySelectorAll("[data-key]"), wireItem);

      input.addEventListener("change", function () {
        var files = Array.prototype.slice.call(input.files);
        input.value = "";
        if (files.length === 0) return;
        var room = maxCount - count();
        if (files.length > room) {
          setStatus(room > 0 ? "Only " + room + " more image(s) can be added." : "The write-up has the most images it can.", true);
          return;
        }
        input.disabled = true;
        setStatus("Uploading " + files.length + " image(s)…", false);
        var after = null;
        files
          .reduce(function (promise, file) {
            return promise.then(function () {
              return uploadOne(file).then(function (image) {
                addItem(image);
                // Each after the last, not over the alt text the last one selected.
                if (after !== null) textarea.setSelectionRange(after, after);
                after = insert(image.path);
              });
            });
          }, Promise.resolve())
          .then(function () {
            setStatus("Uploaded and put in the write-up. Save the draft to keep the text.", false);
          })
          .catch(function (err) {
            setStatus(err.message, true);
          })
          .then(function () {
            input.disabled = false;
          });
      });
    }

    if (role === "primary") setupPrimary();
    else setupInline();
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-image-widget]"), setup);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
