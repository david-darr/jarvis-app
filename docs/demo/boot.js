// Starts the live demo (docs/demo): the interface only loads once the demo's
// service worker (sw.js) controls this page, since that worker is what serves
// the app's files and answers its requests. The first visit installs it and
// reloads once.
(function () {
  function start() {
    const script = document.createElement("script");
    script.type = "module";
    script.src = "/static/js/app.js";
    document.body.append(script);
    const badge = document.createElement("div");
    badge.className = "demo-badge";
    badge.textContent = "Demo · sample data · nothing leaves your browser";
    document.body.append(badge);
  }
  function unsupported() {
    document.body.innerHTML = '<p class="demo-unsupported">This demo needs a current browser. Download Kairos to try it.</p>';
  }
  if (!("serviceWorker" in navigator)) { document.addEventListener("DOMContentLoaded", unsupported); return; }
  if (navigator.serviceWorker.controller) { document.addEventListener("DOMContentLoaded", start); return; }
  navigator.serviceWorker.register("sw.js").then(() => {
    navigator.serviceWorker.addEventListener("controllerchange", () => location.reload(), { once: true });
  }).catch(() => document.addEventListener("DOMContentLoaded", unsupported));
})();
