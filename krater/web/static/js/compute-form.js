/*
 * The new-job form's price section: shows the limit field for a limit order and the target and max fields
 * otherwise. Progressive enhancement only: without this both show, and the server reads the ones the chosen order
 * needs (krater/web/routers/compute.py). CSP forbids inline handlers, hence this file.
 */
(function () {
  "use strict";

  function init() {
    var choices = document.querySelectorAll('[data-role="order-choice"]');
    if (!choices.length) return;
    function update() {
      var chosen = document.querySelector('[data-role="order-choice"]:checked');
      var order = chosen ? chosen.value : "limit";
      document.querySelectorAll("[data-order]").forEach(function (el) {
        el.hidden = el.getAttribute("data-order") !== order;
      });
    }
    choices.forEach(function (choice) {
      choice.addEventListener("change", update);
    });
    update();
  }

  // The estimate's live price: the worker searches Vast for this job's shape, and this asks for the answer every
  // 2 seconds for up to a minute (krater/services/live_prices.py).
  function pollQuote() {
    var el = document.querySelector('[data-role="live-quote"][data-status="checking"]');
    if (!el || !window.fetch) return;
    var tries = 0;
    function ask() {
      tries += 1;
      fetch(el.getAttribute("data-url"), { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (quote) {
          if (quote && quote.status !== "checking") {
            el.setAttribute("data-status", quote.status);
            el.querySelector("strong").textContent = quote.text;
          } else if (tries < 30) {
            setTimeout(ask, 2000);
          } else {
            el.querySelector("strong").textContent = "Vast's live prices didn't come back in time. Try the estimate again.";
          }
        })
        .catch(function () { if (tries < 30) setTimeout(ask, 2000); });
    }
    setTimeout(ask, 1500);
  }

  // The price table's "Cheapest now" column: opening the form asked the worker to search Vast again for each row,
  // and this asks for the answers every 3 seconds for up to 2 minutes, filling in rows as they come back
  // (krater/services/live_prices.py).
  function pollTable() {
    var table = document.querySelector('[data-role="live-table"]');
    if (!table || table.getAttribute("data-done") === "1" || !window.fetch) return;
    var note = document.querySelector('[data-role="live-note"]');
    var tries = 0;
    function show(answer) {
      Object.keys(answer.rows || {}).forEach(function (key) {
        var cell = table.querySelector('[data-live="' + key.replace(/"/g, '\\"') + '"]');
        if (!cell) return;
        cell.querySelector('[data-part="text"]').textContent = answer.rows[key].text;
        cell.querySelector('[data-part="at"]').textContent = answer.rows[key].at;
      });
      if (note && answer.note) {
        note.textContent = answer.note;
        note.hidden = false;
      }
    }
    function ask() {
      tries += 1;
      fetch(table.getAttribute("data-url"), { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (answer) {
          if (answer) show(answer);
          if ((!answer || !answer.done) && tries < 40) setTimeout(ask, 3000);
        })
        .catch(function () { if (tries < 40) setTimeout(ask, 3000); });
    }
    setTimeout(ask, 2000);
  }

  function start() {
    init();
    pollQuote();
    pollTable();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
