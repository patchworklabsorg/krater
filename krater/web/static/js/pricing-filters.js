/*
 * /pricing's filter/sort <select>s: submits the surrounding form on change, so picking a filter
 * doesn't need an extra click. Progressive enhancement only -- the page works without this (a
 * <noscript> "Apply" button submits the form instead; see krater/web/templates/pricing/index.html),
 * and the filtering/sorting itself is entirely server-side query-string handling
 * (krater/web/routers/pricing.py). CSP forbids inline `onchange=`, hence this file.
 */
(function () {
  "use strict";

  function init() {
    var form = document.querySelector('[data-role="pricing-filter-form"]');
    if (!form) return;
    var selects = form.querySelectorAll('[data-role="auto-submit"]');
    selects.forEach(function (select) {
      select.addEventListener("change", function () {
        form.submit();
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
