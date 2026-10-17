/*
 * The compute estimators on the application form (krater/web/templates/projects/_application.html): one fieldset
 * per estimator, `data-estimator="<prefix>"` (`poc`, `full`), with fields named `<prefix>_gpu`, `<prefix>_hours` and
 * so on, and the amount the team requests in `<prefix>_budget`.
 * Vanilla JS, no build step, served from /static (CSP forbids inline scripts; see krater/web/security_headers.py).
 *
 * Without this file each estimator's "Estimate" button is a plain submit (form_action=estimate_<prefix>) and the
 * server fills the amount in (krater.web.application_form). With it, the estimate updates as the fields change: the
 * fields go to /pricing/estimate (krater.web.routers.pricing) as `estimator_*`, and the server computes the total,
 * never this file. The amount follows the estimate until the team types their own; then "Use the estimate" puts
 * the estimate back. Whatever is in the amount when the form is saved is what's requested: reviewers see both.
 */
(function () {
  "use strict";

  var FIELDS = ["gpu", "gpu_other", "gpu_count", "hours", "basis", "margin_percent", "rate"];

  function setUp(widget) {
    var prefix = widget.getAttribute("data-estimator");
    var estimateUrl = widget.getAttribute("data-estimate-url");
    var csrfToken = widget.getAttribute("data-csrf-token");
    var estimateButton = widget.querySelector('[data-role="estimate-button"]');
    var useButton = widget.querySelector('[data-role="use-estimate-button"]');
    var resultEl = widget.querySelector('[data-role="estimate-result"]');
    var amount = widget.querySelector('[data-role="amount"]');
    var otherFields = widget.querySelector('[data-role="other-gpu"]');
    if (!prefix || !estimateButton || !amount) return;

    // An amendment's amount is the project's new total: what's approved so far plus the estimated extra compute.
    var baseCents = parseInt(widget.getAttribute("data-base-cents") || "0", 10) || 0;
    var lastEstimate = null; // the amount the last estimate fills in, as dollars
    var amountFollows = !amount.value; // the amount tracks the estimate until the team types their own
    var timer = null;
    var requestNumber = 0;

    function input(field) {
      return widget.querySelector('[name="' + prefix + "_" + field + '"]');
    }

    function errorFor(field) {
      var el = input(field);
      if (!el) return null;
      var error = el.parentElement.querySelector(".field-error");
      if (!error) {
        error = document.createElement("p");
        error.className = "field-error";
        el.parentElement.appendChild(error);
      }
      return error;
    }

    function clearErrors() {
      FIELDS.forEach(function (field) {
        var el = input(field);
        var error = el && el.parentElement.querySelector(".field-error");
        if (error) error.textContent = "";
        if (el) el.removeAttribute("aria-invalid");
      });
    }

    function setStatus(message) {
      if (!resultEl) return;
      resultEl.textContent = message || "";
      resultEl.hidden = !message;
    }

    function showUseButton() {
      if (useButton) useButton.hidden = !(lastEstimate && amount.value.replace(/[$,\s]/g, "") !== lastEstimate);
    }

    function isBlank() {
      return !input("gpu").value && !input("hours").value;
    }

    function estimate() {
      if (isBlank()) {
        setStatus("");
        return;
      }
      var params = new URLSearchParams();
      params.append("csrf_token", csrfToken);
      FIELDS.forEach(function (field) {
        var el = input(field);
        params.append("estimator_" + field, el ? el.value : "");
      });
      var mine = ++requestNumber;
      fetch(estimateUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params,
      })
        .then(function (response) {
          return response.json().then(function (data) {
            return { ok: response.ok, data: data };
          });
        })
        .then(function (result) {
          if (mine !== requestNumber) return; // a newer estimate is on its way
          clearErrors();
          if (!result.ok) {
            var errors = (result.data && result.data.errors) || {};
            Object.keys(errors).forEach(function (key) {
              var field = key.replace(/^estimator_/, "");
              // Only complain about fields the team has filled in; a half-finished estimator is fine.
              var el = input(field);
              if (el && (el.value || field === "rate" || field === "gpu_other")) {
                var error = errorFor(field);
                if (error) error.textContent = errors[key];
              }
            });
            setStatus("");
            lastEstimate = null;
            showUseButton();
            return;
          }
          var totalCents = Math.round(parseFloat(result.data.total_dollars) * 100) + baseCents;
          lastEstimate = (totalCents / 100).toFixed(2);
          setStatus("Estimate: " + result.data.summary);
          if (amountFollows) amount.value = lastEstimate;
          showUseButton();
        })
        .catch(function () {
          if (mine === requestNumber) setStatus("Couldn't reach Krater's prices. Use the Estimate button instead.");
        });
    }

    function soon() {
      window.clearTimeout(timer);
      timer = window.setTimeout(estimate, 350);
    }

    FIELDS.forEach(function (field) {
      var el = input(field);
      if (!el) return;
      el.addEventListener(el.tagName === "SELECT" ? "change" : "input", soon);
    });

    input("gpu").addEventListener("change", function () {
      if (otherFields) otherFields.hidden = input("gpu").value !== "other";
    });

    amount.addEventListener("input", function () {
      amountFollows = !amount.value;
      showUseButton();
    });

    estimateButton.addEventListener("click", function (event) {
      event.preventDefault();
      window.clearTimeout(timer);
      estimate();
    });

    if (useButton) {
      useButton.addEventListener("click", function () {
        if (!lastEstimate) return;
        amount.value = lastEstimate;
        amountFollows = true;
        showUseButton();
      });
    }

    // Know the current estimate from the start, so "Use the estimate" can show for an amount saved earlier.
    if (!isBlank()) estimate();
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-estimator]"), setUp);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
