import * as THREE from "./vendor/three.module.js";

// A breathing constellation: individual triangular fragments, never a solid
// neon shell. All geometry is local and batched into one GPU draw call.
export function mount(container) {
  let renderer;
  try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true }); }
  catch (_) {
    const fallback = document.createElement("div");
    fallback.className = "core-fallback";
    fallback.setAttribute("aria-hidden", "true");
    container.appendChild(fallback);
    const dispose = () => fallback.remove();
    dispose.setState = () => {};
    dispose.setPaused = () => {};
    return dispose;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  container.appendChild(renderer.domElement);
  renderer.domElement.setAttribute("aria-hidden", "true");
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, .1, 100);
  camera.position.z = 10;
  const count = 4200;
  const positions = new Float32Array(count * 3);
  const colors = new Float32Array(count * 3);
  const sizes = new Float32Array(count);
  const phases = new Float32Array(count);
  const palette = [0x9c83f5, 0xc1a7ff, 0x769bd8, 0x69b7ae, 0xd5ae74, 0xce80aa];
  let seed = 271828;
  const random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; };
  for (let i = 0; i < count; i++) {
    const y = random() * 2 - 1;
    const angle = random() * Math.PI * 2;
    const ring = Math.sqrt(1 - y * y);
    const depth = i % 4 === 0 ? .3 + random() * .65 : .91 + random() * .12;
    const radius = 2.15 * depth * (1 + .10 * Math.sin(angle * 5 + y * 6) + .05 * Math.cos(y * 15));
    positions.set([Math.cos(angle) * ring * radius * 1.1, y * radius, Math.sin(angle) * ring * radius], i * 3);
    const color = new THREE.Color(palette[Math.floor(random() * palette.length)]);
    colors.set([color.r, color.g, color.b], i * 3);
    sizes[i] = 2.4 + random() * 3.2;
    phases[i] = random() * Math.PI * 2;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  geometry.setAttribute("size", new THREE.BufferAttribute(sizes, 1));
  geometry.setAttribute("phase", new THREE.BufferAttribute(phases, 1));
  const material = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, vertexColors: true,
    uniforms: { time: { value: 0 }, energy: { value: .25 }, pixelRatio: { value: renderer.getPixelRatio() } },
    vertexShader: `
      attribute float size;
      attribute float phase;
      uniform float time;
      uniform float energy;
      uniform float pixelRatio;
      varying vec3 vColor;
      varying float vAlpha;
      void main() {
        vec3 p = position;
        float wave = sin(p.y * 3.0 + time * .6 + phase) * .025;
        p *= 1.0 + wave + sin(time * .8) * .015 * energy;
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = size * pixelRatio * (8.0 / -mv.z);
        vColor = color;
        vAlpha = (.72 + .20 * sin(phase + time * .35)) * (.85 + energy * .15);
      }`,
    fragmentShader: `
      varying vec3 vColor;
      varying float vAlpha;
      void main() {
        vec2 p = gl_PointCoord * 2.0 - 1.0;
        float d = max(abs(p.x) * .866 + p.y * .5, -p.y) - .58;
        float outer = 1.0 - smoothstep(-.08, .08, d);
        float inner = 1.0 - smoothstep(-.33, -.18, d);
        float alpha = outer * (1.0 - inner * .65) * vAlpha;
        if (alpha < .015) discard;
        gl_FragColor = vec4(vColor, alpha);
        #include <colorspace_fragment>
      }`,
  });
  const points = new THREE.Points(geometry, material);
  points.rotation.z = -.22;
  scene.add(points);
  let raf = null, disposed = false, paused = false, elapsed = 0, last = 0;
  let targetEnergy = .25;
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  function draw(timestamp = 0) {
    raf = null;
    if (disposed || document.hidden) return;
    const delta = last ? Math.min((timestamp - last) / 1000, .05) : 0;
    last = timestamp;
    if (!paused && !motion.matches) elapsed += delta;
    material.uniforms.time.value = elapsed;
    material.uniforms.energy.value += (targetEnergy - material.uniforms.energy.value) * .05;
    points.rotation.y = elapsed * .065;
    points.rotation.x = Math.sin(elapsed * .1) * .1;
    renderer.render(scene, camera);
    if (!paused && !motion.matches) raf = requestAnimationFrame(draw);
  }
  function requestDraw() {
    if (!disposed && !document.hidden && raf === null) raf = requestAnimationFrame(draw);
  }
  function resize() {
    const { clientWidth: w, clientHeight: h } = container;
    if (!w || !h) return;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.position.z = camera.aspect < 1 ? 10 / camera.aspect : 10;
    camera.updateProjectionMatrix();
    requestDraw();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  function onVisibility() {
    last = 0;
    if (document.hidden && raf !== null) { cancelAnimationFrame(raf); raf = null; }
    else requestDraw();
  }
  document.addEventListener("visibilitychange", onVisibility);
  motion.addEventListener("change", requestDraw);
  resize();
  const dispose = () => {
    disposed = true;
    if (raf !== null) cancelAnimationFrame(raf);
    observer.disconnect();
    document.removeEventListener("visibilitychange", onVisibility);
    motion.removeEventListener("change", requestDraw);
    geometry.dispose(); material.dispose(); renderer.dispose();
    renderer.domElement.remove();
  };
  dispose.setState = (state) => { targetEnergy = state === "working" ? 1 : state === "offline" ? .05 : .25; requestDraw(); };
  dispose.setPaused = (value) => { paused = value; last = 0; requestDraw(); };
  return dispose;
}
