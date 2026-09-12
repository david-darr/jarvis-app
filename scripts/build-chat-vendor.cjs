// Reproducible, offline-at-runtime browser bundle. Keep third-party licenses.
const { build } = require('esbuild');
const fs = require('node:fs');
const path = require('node:path');
const licenses = ['marked', 'dompurify', 'highlight.js', 'pdfjs-dist'].map(name => {
  const dir = path.join(__dirname, '..', 'node_modules', name);
  const file = fs.readdirSync(dir).find(f => /^license(\.md|\.txt)?$/i.test(f));
  return name + '\n' + fs.readFileSync(path.join(dir, file), 'utf8');
}).join('\n\n');
build({ stdin: { contents: "export { marked } from 'marked'; export { default as DOMPurify } from 'dompurify'; export { default as hljs } from 'highlight.js/lib/common';", resolveDir: path.join(__dirname, '..') }, bundle: true, minify: true, format: 'esm', outfile: 'static/js/vendor/chat-vendor.js', banner: { js: '/*! Third-party notices: chat-vendor.LICENSE.txt */' } }).then(() => {
  fs.writeFileSync('static/js/vendor/chat-vendor.LICENSE.txt', licenses);
  const pdf = path.join(__dirname, '..', 'node_modules', 'pdfjs-dist');
  // Electron's bundled Chromium can trail current browsers; legacy includes
  // the supported compatibility polyfills in both the page and worker.
  for (const file of ['pdf.mjs', 'pdf.worker.mjs']) fs.copyFileSync(path.join(pdf, 'legacy', 'build', file), path.join('static/js/vendor', file));
  for (const dir of ['cmaps', 'standard_fonts', 'wasm']) fs.cpSync(path.join(pdf, dir), path.join('static/js/vendor/pdf-assets', dir), { recursive: true });
}).catch(() => process.exit(1));
