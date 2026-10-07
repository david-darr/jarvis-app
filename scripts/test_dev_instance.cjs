// Which copy of JARVIS a launch is: electron/instance.js. Plain Node, no
// Electron. Run: node scripts/test_dev_instance.cjs
const assert = require("node:assert/strict");
const path = require("path");
const { resolveInstance, DEFAULT_PORT, INSTANCE_PORT } = require("../electron/instance");

const appData = path.join("C:", "Users", "someone", "AppData", "Roaming");
let failures = 0;
function check(name, fn) {
  try { fn(); console.log("ok   " + name); } catch (e) { failures += 1; console.log("FAIL " + name + "\n     " + e.message); }
}

check("an ordinary launch is the real app, in its existing JARVIS folder", () => {
  const real = resolveInstance({ argv: ["electron", "."], env: {}, appData });
  assert.deepEqual(real, { name: "", port: DEFAULT_PORT, label: "Kairos", userData: path.join(appData, "JARVIS"), machineWide: true });
  // The Kairos rename must never move anyone's data: Electron's default would follow productName.
  assert.notEqual(real.userData, path.join(appData, "Kairos"));
});

check("a named instance gets its own folder, port and no machine-wide hooks", () => {
  const dev = resolveInstance({ argv: ["electron", ".", "--instance=dev"], env: {}, appData });
  assert.equal(dev.userData, path.join(appData, "JARVIS-dev"));
  assert.notEqual(dev.userData, path.join(appData, "JARVIS"), "never the real app's folder (or its lock)");
  assert.equal(dev.port, INSTANCE_PORT);
  assert.notEqual(dev.port, DEFAULT_PORT);
  assert.equal(dev.machineWide, false);
  assert.equal(dev.label, "Kairos (dev)");
});

check("an explicit user-data folder stays isolated from the pinned real profile", () => {
  const profile = path.join(appData, "test-profile");
  const isolated = resolveInstance({ argv: ["Kairos.exe", `--user-data-dir=${profile}`], env: {}, appData });
  assert.equal(isolated.userData, profile);
  assert.equal(isolated.port, DEFAULT_PORT);
  assert.equal(isolated.machineWide, false);
  assert.throws(() => resolveInstance({ argv: ["Kairos.exe", "--user-data-dir=relative"], env: {}, appData }), /absolute folder/);
});

check("the environment variable names it too; the flag wins", () => {
  assert.equal(resolveInstance({ argv: [], env: { JARVIS_INSTANCE: "qa" }, appData }).name, "qa");
  assert.equal(resolveInstance({ argv: ["--instance=dev"], env: { JARVIS_INSTANCE: "qa" }, appData }).name, "dev");
});

check("names that could escape the folder or confuse it are refused", () => {
  for (const bad of ["../JARVIS", "Dev", "a b", "x".repeat(21), "-dev", "dev/../.."]) {
    assert.throws(() => resolveInstance({ argv: [`--instance=${bad}`], env: {}, appData }), /Instance names/, bad);
  }
});

check("a named instance can never take the real app's port", () => {
  assert.throws(() => resolveInstance({ argv: ["--instance=dev"], env: { JARVIS_PORT: "8420" }, appData }), /real app's port/);
  assert.equal(resolveInstance({ argv: ["--instance=dev"], env: { JARVIS_PORT: "8455" }, appData }).port, 8455);
  assert.throws(() => resolveInstance({ argv: [], env: { JARVIS_PORT: "80" }, appData }), /1024-65535/);
});

if (failures) { console.log(`${failures} failed`); process.exit(1); }
console.log("all passed");
