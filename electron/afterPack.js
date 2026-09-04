// Ad-hoc sign the macOS app before the DMG is built.
//
// Apple Silicon requires every executable to carry a signature — even an
// ad-hoc one with no certificate behind it. A completely unsigned arm64 app
// isn't merely "untrusted", it's refused outright, which macOS reports as
// "Malware Blocked" rather than the usual unidentified-developer prompt. No
// amount of Privacy & Security fiddling gets past that, because there's
// nothing valid to approve.
//
// electron-builder skips signing entirely when mac.identity is null, which
// is what produced the broken 1.4.1 DMG. This signs with the ad-hoc identity
// ("-") instead: no certificate, no Apple account, but a structurally valid
// signature that the kernel will load. Gatekeeper still warns on first open
// (that needs real notarisation), but the app can actually run.
//
// --deep is deprecated for real signing, but it's the pragmatic way to cover
// the bundled Electron frameworks and the Python runtime's own binaries in
// one pass for an ad-hoc signature.
const { execFileSync } = require("child_process");
const path = require("path");

exports.default = async function afterPack(context) {
  if (context.electronPlatformName !== "darwin") return;

  const appName = context.packager.appInfo.productFilename;
  const appPath = path.join(context.appOutDir, `${appName}.app`);

  console.log(`[afterPack] ad-hoc signing ${appPath}`);
  try {
    execFileSync("codesign", [
      "--force",
      "--deep",
      "--sign", "-",              // "-" is the ad-hoc identity
      "--timestamp=none",
      appPath,
    ], { stdio: "inherit" });

    // Prove it took, rather than assuming the command's exit code is enough.
    execFileSync("codesign", ["--verify", "--verbose=2", appPath], { stdio: "inherit" });
    console.log("[afterPack] ad-hoc signature verified");
  } catch (err) {
    // Fail the build: shipping an unsigned arm64 app produces a DMG that
    // cannot be opened at all, which is worse than no build.
    throw new Error(`ad-hoc signing failed: ${err.message}`);
  }
};
