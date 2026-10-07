// electron/instance.js — which copy of JARVIS this process is.
//
// The ordinary app is the unnamed instance: %APPDATA%\JARVIS, port 8420,
// exactly as before. A named instance ("npm run start:dev", or
// --instance=dev / JARVIS_INSTANCE=dev) is a separate copy for development
// (2026-10-04): its own data folder (and with it its own single-instance
// lock, which Electron keys on the user-data folder), its own port, and none
// of the machine-wide hooks the real app owns. It runs beside the installed
// JARVIS without touching it. Pure, so scripts/test_dev_instance.cjs can
// check it without Electron.

const path = require("path");

const DEFAULT_PORT = 8420;
// One port per named instance, fixed rather than picked at random: the page's
// own storage (appearance, collapsed panels) is kept per origin, and the
// origin includes the port, so a new port each launch would forget them.
const INSTANCE_PORT = 8430;
const NAME = /^[a-z0-9][a-z0-9-]{0,19}$/;

function instanceName(argv, env) {
  const flag = (argv || []).find((arg) => arg.startsWith("--instance="));
  const raw = flag ? flag.slice("--instance=".length) : (env && env.JARVIS_INSTANCE) || "";
  if (!raw) return "";
  if (!NAME.test(raw)) {
    throw new Error(`Instance names are 1 to 20 lowercase letters, digits or dashes; got ${JSON.stringify(raw)}.`);
  }
  return raw;
}

function portFor(name, env) {
  const raw = env && env.JARVIS_PORT;
  if (raw) {
    const port = Number(raw);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error(`JARVIS_PORT must be 1024-65535; got ${raw}.`);
    if (name && port === DEFAULT_PORT) throw new Error(`A named instance cannot use the real app's port ${DEFAULT_PORT}.`);
    return port;
  }
  return name ? INSTANCE_PORT : DEFAULT_PORT;
}

// Everything main.js needs to know about this copy. appData is Electron's
// app.getPath("appData") (%APPDATA% on Windows).
function resolveInstance({ argv, env, appData }) {
  const name = instanceName(argv, env);
  const port = portFor(name, env);
  // Electron's --user-data-dir is used by the packaged verifier (and by
  // isolated launches). Honor it before pinning the ordinary profile below.
  const profileFlag = (argv || []).find((arg) => arg.startsWith("--user-data-dir="));
  const profile = profileFlag && profileFlag.slice("--user-data-dir=".length);
  if (profileFlag && (!profile || !path.isAbsolute(profile))) {
    throw new Error("--user-data-dir must name an absolute folder.");
  }
  return {
    name,
    port,
    label: name ? `Kairos (${name})` : "Kairos",
    // Pinned, never Electron's default: that default follows productName, so
    // the rename to Kairos (2026-10-06) would otherwise have opened an empty
    // %APPDATA%\Kairos and left everyone's chats, notes and keys behind.
    // The folder keeps its JARVIS name; nothing is moved.
    userData: profile || path.join(appData, name ? `JARVIS-${name}` : "JARVIS"),
    // The real app's alone: a second copy would race it for the global
    // shortcut and install releases over a development checkout.
    machineWide: !name && !profile,
  };
}

module.exports = { resolveInstance, instanceName, portFor, DEFAULT_PORT, INSTANCE_PORT };
