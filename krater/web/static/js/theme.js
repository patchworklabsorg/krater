// Light/dark theme, the same mechanism as Weave's shared/_theme partial (and patchworklabs.org): an explicit
// choice saved in localStorage wins, otherwise the OS preference applies. Loaded synchronously in <head>,
// before the stylesheet, so data-theme is set before first paint and the page never flashes the wrong theme.
// A plain external script rather than Weave's inline one because Krater's CSP allows no inline scripts.
(() => {
  const root = document.documentElement;
  const media = matchMedia("(prefers-color-scheme: dark)");
  const saved = () => {
    try {
      return localStorage.getItem("theme");
    } catch {
      return null;
    }
  };
  const apply = () => {
    root.dataset.theme = saved() || (media.matches ? "dark" : "light");
  };
  apply();
  media.addEventListener("change", apply);
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-theme-toggle]")) return;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    try {
      localStorage.setItem("theme", next);
    } catch {
      // Storage blocked: the choice still applies to this page.
    }
    root.dataset.theme = next;
  });
})();
