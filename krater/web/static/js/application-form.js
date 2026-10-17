/*
 * Small helpers for the application form (krater/web/templates/projects/_application.html):
 * - a live word count under each answer with a limit (`data-word-limit`), counted the way the server counts
 *   (krater.services.projects.word_count: runs of non-space characters);
 * - the contest/professor detail field appears once the answer isn't "Neither".
 * The form works without this file: the counts are rendered by the server at load, and the detail field shows
 * whenever it has an answer or an error.
 */
(function () {
  "use strict";

  function words(text) {
    var trimmed = text.trim();
    return trimmed ? trimmed.split(/\s+/).length : 0;
  }

  function setUpCounter(textarea) {
    var limit = parseInt(textarea.getAttribute("data-word-limit"), 10);
    var counter = document.querySelector('[data-word-count-for="' + textarea.id + '"]');
    if (!counter || !limit) return;
    function update() {
      var count = words(textarea.value);
      counter.textContent = count + " of " + limit + " words" + (count > limit ? ": too long to submit" : "");
      counter.classList.toggle("word-count--over", count > limit);
    }
    textarea.addEventListener("input", update);
    update();
  }

  function setUpAffiliation() {
    var detail = document.querySelector('[data-role="affiliation-detail"]');
    var radios = document.querySelectorAll('[data-role="affiliation"]');
    if (!detail || !radios.length) return;
    function update() {
      var checked = document.querySelector('[data-role="affiliation"]:checked');
      detail.hidden = !checked || checked.value === "none";
    }
    Array.prototype.forEach.call(radios, function (radio) {
      radio.addEventListener("change", update);
    });
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("textarea[data-word-limit]"), setUpCounter);
    setUpAffiliation();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
