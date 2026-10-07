/*
 * Budget estimator widget for the proposal/amendment draft form (projects/new.html, projects/edit.html).
 * Vanilla JS, no build step, no CDN dependencies -- served straight from /static (CSP forbids inline
 * scripts; see krater/web/security_headers.py).
 *
 * Without this file, the widget's "Estimate" button (krater/web/templates/_macros.html's
 * budget_estimator macro) is a plain form submit: the whole page reloads, and the server
 * (krater.web.routers.projects) recomputes the estimate and re-renders the page with the requested-
 * budget field already filled in. That's the no-JS baseline this file enhances, never replaces.
 *
 * With JS: clicking "Estimate" is intercepted (no page reload) and instead POSTs the same fields to
 * /pricing/estimate (krater.web.routers.pricing), which recomputes server-side exactly the way the
 * no-JS path does -- this widget never computes a rate/total itself, only displays what the server
 * returns. The result is shown, plus a "Use this estimate" button that copies the computed dollar
 * amount into the requested-budget field and marks the hidden `estimator_used` field, so a later
 * "Save draft"/"Create" submit tells the server this draft's budget came from the estimator (the
 * server then stores its own freshly-recomputed breakdown on the revision -- see
 * krater.services.projects.set_budget_estimate).
 */
(function () {
  "use strict";

  function init() {
    var widget = document.getElementById("budget-estimator");
    if (!widget) return;

    var form = widget.closest("form");
    var estimateUrl = widget.getAttribute("data-estimate-url");
    var csrfToken = widget.getAttribute("data-csrf-token");
    var estimateButton = widget.querySelector('[data-role="estimate-button"]');
    var useButton = widget.querySelector('[data-role="use-estimate-button"]');
    var resultEl = widget.querySelector('[data-role="estimate-result"]');
    var usedField = widget.querySelector('[data-role="estimator-used"]');
    var budgetField = form ? form.querySelector("#budget_requested") : null;
    if (!form || !estimateButton || !budgetField) return;

    var fieldNames = ["estimator_gpu", "estimator_hours", "estimator_basis", "estimator_margin_percent"];
    var errorClearTargets = ["estimator_gpu", "estimator_hours", "estimator_basis", "estimator_margin_percent"];

    function clearFieldErrors() {
      errorClearTargets.forEach(function (name) {
        var input = widget.querySelector('[name="' + name + '"]');
        if (!input) return;
        var error = input.parentElement.querySelector(".field-error");
        if (error) error.textContent = "";
      });
    }

    function showFieldError(name, message) {
      var input = widget.querySelector('[name="' + name + '"]');
      if (!input) return;
      var error = input.parentElement.querySelector(".field-error");
      if (!error) {
        error = document.createElement("p");
        error.className = "field-error";
        input.parentElement.appendChild(error);
      }
      error.textContent = message;
    }

    function setStatus(message) {
      if (!resultEl) return;
      resultEl.textContent = message || "";
      resultEl.hidden = !message;
    }

    function formBody() {
      var params = new URLSearchParams();
      params.append("csrf_token", csrfToken);
      fieldNames.forEach(function (name) {
        var input = widget.querySelector('[name="' + name + '"]');
        params.append(name, input ? input.value : "");
      });
      return params;
    }

    estimateButton.addEventListener("click", function (event) {
      event.preventDefault();
      clearFieldErrors();
      if (useButton) useButton.hidden = true;
      setStatus("Estimating…");

      fetch(estimateUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: formBody(),
      })
        .then(function (response) {
          return response.json().then(function (data) {
            return { ok: response.ok, data: data };
          });
        })
        .then(function (result) {
          if (!result.ok) {
            var errors = (result.data && result.data.errors) || {};
            var messages = [];
            Object.keys(errors).forEach(function (name) {
              showFieldError(name, errors[name]);
              messages.push(errors[name]);
            });
            setStatus(messages.length ? "" : "Could not compute an estimate.");
            return;
          }
          setStatus("Estimate: " + result.data.summary);
          if (useButton) {
            useButton.hidden = false;
            useButton.setAttribute("data-total-dollars", result.data.total_dollars);
          }
        })
        .catch(function () {
          setStatus("Could not reach the pricing service. Try again, or use the no-JavaScript Estimate button.");
        });
    });

    if (useButton) {
      useButton.addEventListener("click", function () {
        var total = useButton.getAttribute("data-total-dollars");
        if (!total) return;
        budgetField.value = total;
        if (usedField) usedField.value = "1";
        // Confirm the copy; otherwise the click looks like it did nothing when the budget field is
        // scrolled out of view above the estimator.
        useButton.hidden = true;
        setStatus((resultEl ? resultEl.textContent : "") + " Copied into the requested budget.");
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
