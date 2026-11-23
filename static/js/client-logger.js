// static/js/client-logger.js
(function () {
  "use strict";

  var script = document.currentScript;
  var ENDPOINT   = (script && script.dataset.logEndpoint) || "/api/client-log";
  var REQUEST_ID = (script && script.dataset.requestId) || "";

  var sent = 0, MAX = 20;
  var recent = {}, DEDUPE_MS = 5000;

  function report(level, message) {
    if (sent >= MAX) return;
    var key = level + "|" + message, now = Date.now();
    if (recent[key] && now - recent[key] < DEDUPE_MS) return;
    recent[key] = now; sent++;

    var payload = {
      level: level,
      message: String(message || "").slice(0, 500),
      page: location.pathname + location.search,
      requestId: REQUEST_ID
    };

    try {
      var body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "application/json" }));
      } else {
        fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: body, keepalive: true
        }).catch(function () {});
      }
    } catch (e) { /* logging-ն ինքը երբեք չպիտի error նետի */ }
  }

  window.addEventListener("error", function (e) {
    if (e.message) {
      var loc = (e.filename || "") + ":" + (e.lineno || "") + ":" + (e.colno || "");
      report("Error", e.message + " @ " + loc);
    } else if (e.target && (e.target.src || e.target.href)) {
      report("Warning", "Resource load failed: " + (e.target.src || e.target.href));
    }
  }, true);

  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    report("Error", "Unhandled promise: " + ((r && (r.stack || r.message)) || String(r)));
  });

  window.clientLog = report;
})();