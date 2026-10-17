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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
