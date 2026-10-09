// Offline, reproducible Forge assets and notices for every bundled dependency.
const { build } = require('esbuild');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
async function main() {
  const out = path.join(root, 'static/js/vendor');
  fs.mkdirSync(out, { recursive: true });
  const metadata = process.argv.indexOf('--notices-from');
  const result = metadata >= 0 ? { metafile: JSON.parse(fs.readFileSync(process.argv[metadata + 1], 'utf8')) } : await build({ absWorkingDir: root, entryPoints: ['scripts/forge-vendor-entry.mjs'], bundle: true, minify: true,
    format: 'esm', target: 'es2022', metafile: true, outfile: path.join(out, 'forge-vendor.js'),
    banner: { js: '/*! Third-party notices: forge-vendor.LICENSE.txt */' } });
  const packages = new Map();
  for (const input of Object.keys(result.metafile.inputs)) {
    if (!input.includes('node_modules/')) continue;
    let dir = path.dirname(path.resolve(root, input));
    while (!fs.existsSync(path.join(dir, 'package.json')) && dir !== root) dir = path.dirname(dir);
    const pkg = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
    packages.set(pkg.name, { dir, pkg });
  }
  const notices = [...packages].sort(([a], [b]) => a.localeCompare(b)).map(([name, { dir, pkg }]) => {
    const file = fs.readdirSync(dir).find(f => /^licen[sc]e(?:\.md|\.txt)?$/i.test(f));
    if (!file) throw new Error(`License missing for ${name}`);
    return `${name} ${pkg.version} (${pkg.license})\n${fs.readFileSync(path.join(dir, file), 'utf8')}`;
  });
  fs.writeFileSync(path.join(out, 'forge-vendor.LICENSE.txt'), notices.join('\n\n') + '\n');
  fs.copyFileSync(path.join(root, 'node_modules/@xterm/xterm/css/xterm.css'), path.join(out, 'xterm.css'));
  console.log(`Forge bundle built with ${packages.size} package licenses.`);
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
