/*
 * The price history chart's hover layer (krater/web/charts.py draws the chart itself, server-side): a crosshair at
 * the nearest sample and a tooltip with its cheapest, 10th-percentile and median price. Progressive enhancement:
 * the page has the same numbers in a table without this.
 */
(function () {
  "use strict";

  function init(wrap) {
    var svg = wrap.querySelector("svg.chart");
    var tip = wrap.querySelector(".chart-tooltip");
    var cross = svg && svg.querySelector(".chart__crosshair");
    if (!svg || !tip || !cross) return;
    var points = (svg.getAttribute("data-points") || "").split("|").map(function (row) {
      var f = row.split(",");
      return { x: parseFloat(f[0]), when: f[1], min: f[2], p10: f[3], median: f[4] };
    });
    if (!points.length) return;
    var viewWidth = svg.viewBox.baseVal.width;

    function show(clientX) {
      var box = svg.getBoundingClientRect();
      var x = ((clientX - box.left) / box.width) * viewWidth;
      var best = points[0];
      points.forEach(function (p) {
        if (Math.abs(p.x - x) < Math.abs(best.x - x)) best = p;
      });
      cross.setAttribute("x1", best.x);
      cross.setAttribute("x2", best.x);
      cross.removeAttribute("hidden");
      tip.textContent = "";
      [["", best.when], ["Cheapest ", best.min], ["10th percentile ", best.p10], ["Median ", best.median]].forEach(
        function (line, i) {
          var row = document.createElement("div");
          if (i === 0) row.className = "chart-tooltip__title";
          row.textContent = line[0] + line[1];
          tip.appendChild(row);
        }
      );
      tip.hidden = false;
      var left = (best.x / viewWidth) * box.width;
      tip.style.left = Math.min(Math.max(left - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth) + "px";
    }

    function hide() {
      cross.setAttribute("hidden", "");
      tip.hidden = true;
    }

    svg.addEventListener("pointermove", function (e) { show(e.clientX); });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("touchstart", function (e) { show(e.touches[0].clientX); }, { passive: true });
  }

  function start() {
    document.querySelectorAll('[data-role="price-chart"]').forEach(init);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
