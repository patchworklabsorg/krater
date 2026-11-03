/*
 * Write / Preview tabs for the final write-up on the completion-draft edit page. Vanilla JS, no build
 * step, no CDN dependencies -- served straight from /static.
 *
 * Preview posts the unsaved text to POST <preview-url> (krater/web/routers/projects.py,
 * `preview_write_up`), which renders it with the gallery's own write-up card (gallery/_write_up.html)
 * and Krater's Markdown filter, so what shows is exactly what the gallery will show. Nothing is saved.
 * The fragment comes from Krater's own template, and the filter never passes a member's raw HTML
 * through, so it's safe to insert as HTML.
 *
 * Without JavaScript the tabs stay hidden and the box is a plain text field.
 */
(function () {
  "use strict";

  function init() {
    var tabs = document.getElementById("write-up-tabs");
    var textarea = document.getElementById("write_up");
    var panel = document.getElementById("write-up-preview");
    if (!tabs || !textarea || !panel) return;

    var writeTab = document.getElementById("write-up-tab-write");
    var previewTab = document.getElementById("write-up-tab-preview");
    var previewUrl = tabs.getAttribute("data-preview-url");
    var csrfToken = tabs.getAttribute("data-csrf-token");
    var request = 0;

    function select(tab) {
      var previewing = tab === previewTab;
      [writeTab, previewTab].forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
      });
      textarea.hidden = previewing;
      panel.hidden = !previewing;
    }

    function showMessage(message) {
      panel.textContent = "";
      var p = document.createElement("p");
      p.className = "muted";
      p.textContent = message;
      panel.appendChild(p);
    }

    function preview() {
      select(previewTab);
      var mine = ++request;
      showMessage("Loading the preview...");
      var body = new URLSearchParams();
      body.append("csrf_token", csrfToken);
      body.append("write_up", textarea.value);
      fetch(previewUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body,
        credentials: "same-origin",
      })
        .then(function (response) {
          return response.text().then(function (text) {
            if (!response.ok) throw new Error(response.status === 409 ? text : "");
            return text;
          });
        })
        .then(function (html) {
          // A later click (or a switch back to Write) wins over a slow answer.
          if (mine === request) panel.innerHTML = html;
        })
        .catch(function (error) {
          if (mine === request) showMessage(error.message || "Couldn't load the preview. Try again in a minute.");
        });
    }

    writeTab.addEventListener("click", function () {
      request++;
      select(writeTab);
      textarea.focus();
    });
    previewTab.addEventListener("click", preview);

    // Arrow keys move between the two tabs, as in any tab list.
    tabs.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      var next = document.activeElement === writeTab ? previewTab : writeTab;
      next.focus();
      next.click();
    });

    tabs.hidden = false;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
