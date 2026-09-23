// desktop.log: what the Electron shell itself says - the backend starting and
// exiting, update checks, startup failures. In a packaged install that went to
// a console nobody sees. Written in the backend's own line format
// ("YYYY-MM-DD HH:MM:SS,mmm - name - LEVEL - message") so core/logs.py reads it
// the same way and Settings > Admin > Logs can show it beside backend.log.
// Capped: one file plus one rotated copy of about 1 MB each.

const fs = require("fs");
const path = require("path");
const util = require("util");

const MAX_BYTES = 1_000_000;
const BACKEND_TAIL_LINES = 60;

function stamp(date = new Date()) {
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())},${pad(date.getMilliseconds(), 3)}`;
}

function createDesktopLog(logDir) {
  const file = path.join(logDir, "desktop.log");
  const backendTail = [];

  function write(level, message, name = "desktop") {
    // Logging must never be the thing that breaks the app.
    try {
      fs.mkdirSync(logDir, { recursive: true });
      try {
        if (fs.statSync(file).size > MAX_BYTES) fs.renameSync(file, `${file}.1`);
      } catch (e) { /* no file yet */ }
      fs.appendFileSync(file, `${stamp()} - ${name} - ${level} - ${String(message).replace(/\r?\n$/, "")}\n`, "utf8");
    } catch (e) { /* nowhere left to report it */ }
  }

  // Everything main.js already prints goes to the file as well, at a level
  // matching the console method.
  function attachConsole(target = console) {
    for (const [method, level] of [["log", "INFO"], ["info", "INFO"], ["warn", "WARNING"], ["error", "ERROR"]]) {
      const original = target[method].bind(target);
      target[method] = (...args) => {
        original(...args);
        write(level, util.format(...args));
      };
    }
  }

  // The backend writes its own logs once it is running, so its output is not
  // copied here line by line. But a backend that dies before its logging is
  // set up (a failed import, say) leaves its only trace on stderr, so the last
  // lines are kept and written out if it exits unexpectedly.
  function backendOutput(chunk) {
    for (const line of String(chunk).split(/\r?\n/)) {
      if (!line.trim()) continue;
      backendTail.push(line);
      if (backendTail.length > BACKEND_TAIL_LINES) backendTail.shift();
    }
  }

  function backendExited(code, expected) {
    if (expected) {
      write("INFO", `backend stopped (exit code ${code})`);
    } else {
      write("ERROR", `backend exited unexpectedly with code ${code}. Its last output:\n${backendTail.join("\n")}`);
    }
    backendTail.length = 0;
  }

  return { file, write, attachConsole, backendOutput, backendExited };
}

module.exports = { createDesktopLog, stamp };
