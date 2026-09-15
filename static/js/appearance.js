// Device-local appearance. Images are decoded into canvas pixels, never
// injected as URLs or uploaded. IndexedDB keeps them out of small JSON storage.
const DEFAULTS = Object.freeze({ mode: 'default', color: '#23302e', tint: .65,
  distortion: .35, swirl: .3, grainMixer: .2, grainOverlay: .12, motion: true });
const motionQuery = matchMedia('(prefers-reduced-motion: reduce)');
const TOKENS = ['--bg', '--bg-panel', '--bg-panel-solid', '--surface-2', '--sidebar-bg',
  '--text', '--text-dim', '--text-faint', '--sidebar-text', '--sidebar-muted', '--sidebar-accent', '--border', '--border-strong', '--accent', '--accent-rgb'];
let settings = { ...DEFAULTS }, storageKey = '', imageBitmap = null;
let imageCanvas, shaderCanvas, previewCanvas, glScene, animation = 0, lastFrame = 0, time = 0;
let storageIssue = '', imageRevision = 0, identityRevision = 0, imageQueue = Promise.resolve();

function normalize(raw = {}) {
  const value = { ...DEFAULTS };
  if (['default', 'color', 'image', 'shader'].includes(raw.mode)) value.mode = raw.mode;
  if (/^#[\da-f]{6}$/i.test(raw.color)) value.color = raw.color;
  for (const key of ['tint', 'distortion', 'swirl', 'grainMixer', 'grainOverlay']) {
    if (typeof raw[key] === 'number' && Number.isFinite(raw[key])) value[key] = Math.max(0, Math.min(1, raw[key]));
  }
  if (typeof raw.motion === 'boolean') value.motion = raw.motion;
  return value;
}
const rgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
const css = values => `rgb(${values.map(Math.round).join(', ')})`;
const mix = (color, target, amount) => color.map(v => v * (1 - amount) + target * amount);
function luminance(color) {
  return color.map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
    .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
}
function readable(color, fade = 0) {
  const foreground = luminance(color) > .179 ? 0 : 255;
  for (let amount = fade; amount >= 0; amount -= .02) {
    const text = color.map(v => Math.round(v * amount + foreground * (1 - amount)));
    const a = luminance(color), b = luminance(text);
    if ((Math.max(a, b) + .05) / (Math.min(a, b) + .05) >= 4.5) return text;
  }
  return [foreground, foreground, foreground];
}

export function getAppearance() {
  return { ...settings, hasImage: !!imageBitmap, storageIssue,
    staticFallback: settings.mode === 'shader' && glScene === false };
}
function imageStore(action, value) {
  const key = storageKey;
  const operation = imageQueue.catch(() => {}).then(() => new Promise((resolve, reject) => {
    const request = indexedDB.open('jarvis-appearance', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('images');
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error('Image storage is busy. Close other JARVIS windows and retry.'));
    request.onsuccess = () => {
      const db = request.result;
      const tx = db.transaction('images', action === 'get' ? 'readonly' : 'readwrite');
      const store = tx.objectStore('images');
      const op = action === 'put' ? store.put(value, key) : store[action](key);
      tx.oncomplete = () => { db.close(); resolve(op.result); };
      tx.onabort = tx.onerror = () => { db.close(); reject(tx.error || new Error('Image could not be saved.')); };
    };
  }));
  imageQueue = operation;
  return operation;
}

export async function initAppearance(username) {
  const revision = ++identityRevision;
  ++imageRevision;
  imageBitmap?.close(); imageBitmap = null; storageIssue = '';
  storageKey = 'jarvis:appearance:v1:' + String(username || 'local');
  try { settings = normalize(JSON.parse(localStorage.getItem(storageKey) || '{}')); }
  catch { settings = { ...DEFAULTS }; }
  ensureCanvases();
  apply();
  try {
    const saved = await imageStore('get');
    if (saved instanceof Blob) {
      const bitmap = await createImageBitmap(saved);
      if (revision !== identityRevision) { bitmap.close(); return; }
      imageBitmap = bitmap;
    }
  } catch { if (revision === identityRevision) storageIssue = 'The saved image could not be loaded. Choose it again to retry.'; }
  if (revision !== identityRevision) return;
  apply();
}

export function updateAppearance(patch) {
  settings = normalize({ ...settings, ...patch });
  storageIssue = '';
  try { localStorage.setItem(storageKey, JSON.stringify(settings)); }
  catch { storageIssue = 'Preview applied, but preferences could not be saved on this device.'; }
  apply();
}

export async function setAppearanceImage(file) {
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('Choose a PNG, JPEG, or WebP image.');
  if (file.size > 12 * 1024 * 1024) throw new Error('Choose an image smaller than 12 MB.');
  const revision = ++imageRevision;
  let decoded;
  try { decoded = await createImageBitmap(file); } catch { throw new Error('This image could not be opened. Try another image.'); }
  if (decoded.width * decoded.height > 40000000) { decoded.close(); throw new Error('Choose an image below 40 megapixels.'); }
  const canvas = document.createElement('canvas');
  const scale = Math.min(1, 2560 / Math.max(decoded.width, decoded.height));
  canvas.width = Math.max(1, Math.round(decoded.width * scale));
  canvas.height = Math.max(1, Math.round(decoded.height * scale));
  canvas.getContext('2d').drawImage(decoded, 0, 0, canvas.width, canvas.height);
  decoded.close();
  const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/webp', .88));
  if (!blob) throw new Error('This image could not be prepared.');
  if (revision !== imageRevision) return;
  // Saving first means a quota failure leaves the previous image intact.
  try { await imageStore('put', blob); } catch { throw new Error('Image could not be saved. Free some device storage and retry.'); }
  const next = await createImageBitmap(blob);
  if (revision !== imageRevision) { next.close(); return; }
  imageBitmap?.close(); imageBitmap = next;
  updateAppearance({ mode: 'image' });
}

export async function removeAppearanceImage() {
  ++imageRevision;
  await imageStore('delete');
  imageBitmap?.close(); imageBitmap = null;
  updateAppearance({ mode: settings.mode === 'image' ? 'color' : settings.mode });
}
export async function resetAppearance() {
  await removeAppearanceImage();
  updateAppearance(DEFAULTS);
}
export function setAppearancePreview(canvas) { previewCanvas = canvas; draw(); }

function ensureCanvases() {
  if (imageCanvas) return;
  imageCanvas = document.createElement('canvas');
  shaderCanvas = document.createElement('canvas');
  for (const canvas of [imageCanvas, shaderCanvas]) {
    canvas.className = 'app-background'; canvas.setAttribute('aria-hidden', 'true');
    document.body.prepend(canvas);
  }
  shaderCanvas.addEventListener('webglcontextlost', event => {
    event.preventDefault(); glScene = false; apply();
  });
  shaderCanvas.addEventListener('webglcontextrestored', () => { glScene = undefined; apply(); });
  window.addEventListener('resize', draw);
  document.addEventListener('visibilitychange', animate);
  motionQuery.addEventListener('change', () => { draw(); animate(); });
}

function apply() {
  ensureCanvases();
  const root = document.documentElement;
  root.dataset.appearance = settings.mode;
  if (settings.mode === 'default') {
    TOKENS.forEach(key => root.style.removeProperty(key));
    root.style.removeProperty('color-scheme');
  } else {
    const color = rgb(settings.color), light = luminance(color) > .179;
    const sidebar = mix(color, 0, .18).map(Math.round);
    const accent = readable(color, .12);
    const values = {
      '--bg': css(color), '--sidebar-bg': css(sidebar),
      '--sidebar-text': css(readable(sidebar)), '--sidebar-muted': css(readable(sidebar, .28)),
      '--sidebar-accent': css(readable(sidebar, .12)),
      '--bg-panel': css(mix(color, light ? 255 : 0, light ? .24 : .16)),
      '--bg-panel-solid': css(mix(color, light ? 255 : 0, light ? .4 : .25)),
      '--surface-2': css(mix(color, light ? 0 : 255, light ? .08 : .1)),
      '--text': css(readable(color)),
      '--text-dim': css(readable(color, .22)),
      '--text-faint': css(readable(color, .35)),
      '--border': light ? 'rgba(0,0,0,.13)' : 'rgba(255,255,255,.12)',
      '--border-strong': light ? 'rgba(0,0,0,.25)' : 'rgba(255,255,255,.25)',
      '--accent': css(accent), '--accent-rgb': accent.join(', '),
    };
    for (const [key, value] of Object.entries(values)) root.style.setProperty(key, value);
    root.style.colorScheme = light ? 'light' : 'dark';
  }
  if (settings.mode === 'shader' && glScene === undefined) glScene = createShader();
  draw(); animate();
}

function fit(canvas, width, height) {
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
}
function cover(ctx, image, width, height) {
  const scale = Math.max(width / image.width, height / image.height);
  ctx.drawImage(image, (width - image.width * scale) / 2, (height - image.height * scale) / 2, image.width * scale, image.height * scale);
}
function draw() {
  if (!imageCanvas) return;
  // Limit the full-screen shader's pixel budget. Controls remain responsive
  // on integrated graphics and high-DPI displays.
  const scale = Math.min(1, 1280 / Math.max(innerWidth, innerHeight));
  const width = Math.max(1, Math.round(innerWidth * scale)), height = Math.max(1, Math.round(innerHeight * scale));
  const isShader = settings.mode === 'shader' && !!glScene;
  imageCanvas.hidden = settings.mode === 'default' || isShader;
  shaderCanvas.hidden = !isShader;
  fit(imageCanvas, width, height);
  if (isShader) {
    fit(shaderCanvas, width, height);
    glScene.draw(width, height, motionQuery.matches ? 0 : time, settings);
  } else {
    const ctx = imageCanvas.getContext('2d');
    ctx.fillStyle = settings.mode === 'default' ? '#101113' : settings.color;
    ctx.fillRect(0, 0, width, height);
    if (settings.mode === 'image' && imageBitmap) {
      cover(ctx, imageBitmap, width, height);
      ctx.globalAlpha = settings.tint; ctx.fillStyle = settings.color;
      ctx.fillRect(0, 0, width, height); ctx.globalAlpha = 1;
    } else if (settings.mode === 'shader') {
      const gradient = ctx.createLinearGradient(0, 0, width, height);
      gradient.addColorStop(0, settings.color); gradient.addColorStop(.5, css(mix(rgb(settings.color), 255, .18)));
      gradient.addColorStop(1, settings.color); ctx.fillStyle = gradient; ctx.fillRect(0, 0, width, height);
    }
  }
  if (previewCanvas?.isConnected) {
    const ctx = previewCanvas.getContext('2d');
    ctx.drawImage(isShader ? shaderCanvas : imageCanvas, 0, 0, previewCanvas.width, previewCanvas.height);
    ctx.fillStyle = settings.mode === 'default' ? '#0c0d0f' : css(mix(rgb(settings.color), 0, .18));
    ctx.fillRect(0, 0, previewCanvas.width * .16, previewCanvas.height);
  } else previewCanvas = null;
}
function animate() {
  cancelAnimationFrame(animation); animation = 0; lastFrame = 0;
  if (settings.mode !== 'shader' || !glScene || !settings.motion || motionQuery.matches || document.hidden) return;
  function frame(now) {
    if (!lastFrame || now - lastFrame >= 40) {
      time += lastFrame ? Math.min((now - lastFrame) / 1000, .1) : 0;
      lastFrame = now; draw();
    }
    animation = requestAnimationFrame(frame);
  }
  animation = requestAnimationFrame(frame);
}

function createShader() {
  const gl = shaderCanvas.getContext('webgl', { alpha: false, antialias: false, depth: false, powerPreference: 'low-power' });
  if (!gl) return false;
  let program, buffer;
  const shaders = [];
  try {
    const compile = (kind, source) => {
      const shader = gl.createShader(kind); shaders.push(shader);
      gl.shaderSource(shader, source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader));
      return shader;
    };
    program = gl.createProgram();
    gl.attachShader(program, compile(gl.VERTEX_SHADER, 'attribute vec2 position; void main(){gl_Position=vec4(position,0.,1.);}'));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, `
      precision highp float;
      uniform vec2 resolution; uniform vec3 base;
      uniform float clock, distortion, swirl, mixer, grain;
      float noise(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}
      void main(){
        vec2 p=(gl_FragCoord.xy-.5*resolution)/resolution.y;
        float angle=swirl*7.*exp(-length(p)*1.3)+clock*.045;
        p=mat2(cos(angle),-sin(angle),sin(angle),cos(angle))*p;
        p+=distortion*.4*vec2(sin(p.y*5.+clock*.12),cos(p.x*4.-clock*.1));
        float band=.5+.5*sin(p.x*4.+p.y*2.+sin(p.y*3.)+clock*.1);
        band=mix(band,noise(floor(p*180.)),mixer*.55);
        vec3 color=mix(base*.96,mix(base,vec3(1.),.22),smoothstep(.15,.9,band));
        color+=(noise(gl_FragCoord.xy)-.5)*grain*.16;
        gl_FragColor=vec4(clamp(color,0.,1.),1.);
      }`));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
    shaders.forEach(shader => gl.deleteShader(shader)); shaders.length = 0;
    buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]), gl.STATIC_DRAW);
    gl.useProgram(program);
    const position = gl.getAttribLocation(program, 'position');
    gl.enableVertexAttribArray(position); gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    const uniforms = Object.fromEntries(['resolution', 'base', 'clock', 'distortion', 'swirl', 'mixer', 'grain'].map(key => [key, gl.getUniformLocation(program, key)]));
    return { draw(width, height, elapsed, prefs) {
      gl.viewport(0, 0, width, height);
      gl.uniform2f(uniforms.resolution, width, height);
      gl.uniform3fv(uniforms.base, rgb(prefs.color).map(v => v / 255));
      gl.uniform1f(uniforms.clock, elapsed);
      gl.uniform1f(uniforms.distortion, prefs.distortion); gl.uniform1f(uniforms.swirl, prefs.swirl);
      gl.uniform1f(uniforms.mixer, prefs.grainMixer); gl.uniform1f(uniforms.grain, prefs.grainOverlay);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    } };
  } catch {
    shaders.forEach(shader => gl.deleteShader(shader));
    if (buffer) gl.deleteBuffer(buffer); if (program) gl.deleteProgram(program);
    return false;
  }
}
