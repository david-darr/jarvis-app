// Real-time 3D JARVIS core — David's ask (2026-08-31): "a 3d version of
// jarvis-pic.jpg" (a dense, tangled wireframe sphere covered in scribbly
// circuit-trace lines, with a few bright crossing orbital bands). Built
// with Three.js, vendored locally from voice-visualizer's own copy
// (matching this project's "vendor, don't CDN" convention — see
// voice-visualizer's own core3d module for the same pattern applied to The
// Bridge's existing Core view, which this is the spiritual successor of).
//
// Revised 2026-08-31 (David's follow-up: "bigger and more complex, just
// like the reference image"). The first pass used a clean IcosahedronGeometry
// wireframe, which reads as geometric/faceted, not the reference's organic,
// hand-scribbled circuit texture. Replaced with procedurally generated
// "trace" polylines: each one is a random walk across the sphere surface
// (a sequence of small tangent-plane steps re-projected back onto the
// sphere, with slight per-point radius jitter for the reference's bumpy,
// hand-drawn look), batched into one LineSegments draw call for performance
// despite the much higher line-segment count.
//
// Revised again 2026-09-01 (David's ask: "even more complex and bigger...
// truly look like the jarvis/ultron visualizer from the MCU") — first pass
// added a gyroscope of orbiting ring shells around the circuit sphere, plus
// a pulsing emissive-looking inner core.
//
// Revised again same day (David's follow-up: "instead of adding more rings
// make the spheres more complex, with spheres inside broken spheres within
// more spheres") — replaced the ring shells with nested "broken" wireframe
// spheres instead: each is an icosahedron's edge-wireframe with a random
// fraction of its edge segments dropped, so it reads as fractured/
// incomplete rather than a clean solid shell, layered concentrically
// (Russian-doll style) around the original circuit-trace sphere, each layer
// spinning independently on its own axis/speed.

import * as THREE from "./vendor/three.module.js";

const SPHERE_RADIUS = 4.4;

// Developer Mode turns the whole app red (`:root.dev-mode` in style.css), but
// the core is WebGL — CSS custom properties can't reach a THREE material, so
// it stayed defiantly blue while everything around it went red (David's ask
// 2026-09-04). Two hand-picked palettes rather than a programmatic hue
// rotation: these colors aren't one hue, they're a graded set from deep rim
// to near-white center, and rotating them mechanically muddies that ramp.
const PALETTES = {
  blue: {
    trace: 0x00d4ff, base: 0x00d4ff, points: 0x8fe8ff,
    band: 0x00eaff, bandHalo1: 0x00c8ff, bandHalo2: 0x0090ff,
    glow: 0x00d4ff, coreCenter: 0xdfffff, coreHalo: 0x00eaff,
    brokenInner: 0x00d4ff, brokenOuter: 0xbdf3ff, ambient: 0x223344,
  },
  red: {
    trace: 0xff3b3b, base: 0xff3b3b, points: 0xffa8a8,
    band: 0xff5252, bandHalo1: 0xff4040, bandHalo2: 0xd01818,
    glow: 0xff3b3b, coreCenter: 0xffe6e6, coreHalo: 0xff5252,
    brokenInner: 0xff3b3b, brokenOuter: 0xffc9c9, ambient: 0x442222,
  },
};

function activePalette() {
  return document.documentElement.classList.contains("dev-mode") ? PALETTES.red : PALETTES.blue;
}

export function mount(container) {
  let palette = activePalette();
  // Every material gets registered against its palette key so the whole scene
  // can be recolored in place when Developer Mode is toggled — the toggle
  // lives in the sidebar and is reachable from any tab, so waiting for a
  // remount would leave the core the wrong color until you navigated away.
  const tinted = [];
  const tint = (material, key) => { tinted.push([material, key]); return material; };
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 300);
  // Pulled back further than the sphere/band radius needs (a naive z ~=
  // radius * 1.3 clipped at non-square container aspects last time) to
  // leave real margin now that everything is bigger, and further still now
  // that the outer gyroscope rings extend well past the sphere itself.
  camera.position.z = SPHERE_RADIUS * 5.2;

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  const core = new THREE.Group();
  scene.add(core);

  // -- dense scribbled wireframe: many short random-walk traces on the
  // sphere surface, matching the reference's organic circuit-trace texture
  // far more closely than a faceted polyhedron wireframe would.
  const TRACE_COUNT = 200;
  const POINTS_PER_TRACE = 45;
  const traceSegPositions = [];

  for (let t = 0; t < TRACE_COUNT; t++) {
    let pos = new THREE.Vector3().randomDirection().multiplyScalar(SPHERE_RADIUS);
    // Two arbitrary tangent basis vectors at the start point, re-derived
    // each step so the walk can wander in any direction along the surface.
    let prev = pos;
    for (let i = 1; i < POINTS_PER_TRACE; i++) {
      const normal = pos.clone().normalize();
      // Any vector not parallel to normal, then cross twice to get an
      // orthonormal tangent pair.
      const helper = Math.abs(normal.y) < 0.9 ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(1, 0, 0);
      const tangentA = new THREE.Vector3().crossVectors(normal, helper).normalize();
      const tangentB = new THREE.Vector3().crossVectors(normal, tangentA).normalize();
      const angle = Math.random() * Math.PI * 2;
      const stepSize = 0.14 + Math.random() * 0.1;
      const step = tangentA.multiplyScalar(Math.cos(angle) * stepSize)
        .add(tangentB.multiplyScalar(Math.sin(angle) * stepSize));
      const jitter = 1 + (Math.random() - 0.5) * 0.03;
      const next = pos.clone().add(step).normalize().multiplyScalar(SPHERE_RADIUS * jitter);
      traceSegPositions.push(prev.x, prev.y, prev.z, next.x, next.y, next.z);
      prev = next;
      pos = next;
    }
  }
  const traceGeo = new THREE.BufferGeometry();
  traceGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(traceSegPositions), 3));
  const traceMat = tint(new THREE.LineBasicMaterial({ color: palette.trace, transparent: true, opacity: 0.4 }), "trace");
  core.add(new THREE.LineSegments(traceGeo, traceMat));

  // -- faint base sphere wireframe underneath the traces, low detail, for
  // a subtle structural hint (reference has soft latitude/longitude ghosting
  // beneath the scribble texture).
  const baseGeo = new THREE.IcosahedronGeometry(SPHERE_RADIUS, 2);
  const baseEdges = new THREE.EdgesGeometry(baseGeo, 1);
  const baseMat = tint(new THREE.LineBasicMaterial({ color: palette.base, transparent: true, opacity: 0.08 }), "base");
  core.add(new THREE.LineSegments(baseEdges, baseMat));

  // -- scattered "circuit node" points along the surface, denser and
  // brighter than the first pass to read at the larger size.
  const pointCount = 3200;
  const positions = new Float32Array(pointCount * 3);
  for (let i = 0; i < pointCount; i++) {
    const v = new THREE.Vector3().randomDirection().multiplyScalar(SPHERE_RADIUS * (1 + (Math.random() - 0.5) * 0.02));
    positions.set([v.x, v.y, v.z], i * 3);
  }
  const pointsGeo = new THREE.BufferGeometry();
  pointsGeo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const pointsMat = tint(new THREE.PointsMaterial({ color: palette.points, size: 0.022, transparent: true, opacity: 0.65 }), "points");
  core.add(new THREE.Points(pointsGeo, pointsMat));

  // -- bright crossing orbital bands (the reference image's big diagonal
  // streaks) — more of them, thinner and brighter, so they read as the
  // focal "orbit" lines against the denser scribble field. Given a visible
  // blue-aura glow (David's ask 2026-09-01, "make them more noticeable by
  // having them glow with a blue aura") via two wider, fainter, additive-
  // blended halo tori nested as children of each sharp band — additive
  // blending is the actual mechanism that makes overlapping transparent
  // layers read as a soft bloom instead of just a wider flat ring, since
  // this renderer has no real post-processing bloom pass. Halos are
  // children of the band mesh, so they inherit its rotation for free.
  const bandMat = tint(new THREE.MeshBasicMaterial({ color: palette.band, transparent: true, opacity: 0.7 }), "band");
  const bandHaloMat1 = tint(new THREE.MeshBasicMaterial({
    color: palette.bandHalo1, transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending, depthWrite: false,
  }), "bandHalo1");
  const bandHaloMat2 = tint(new THREE.MeshBasicMaterial({
    color: palette.bandHalo2, transparent: true, opacity: 0.1, blending: THREE.AdditiveBlending, depthWrite: false,
  }), "bandHalo2");
  const bandRotations = [
    [Math.PI / 2, 0, 0],
    [Math.PI / 2.3, Math.PI / 2.4, 0],
    [Math.PI / 3, -Math.PI / 3, Math.PI / 6],
    [Math.PI / 5, Math.PI / 1.7, -Math.PI / 5],
    [-Math.PI / 4, Math.PI / 5, Math.PI / 3],
  ];
  const bandHaloGeos = [];
  const bands = bandRotations.map(([rx, ry, rz]) => {
    const geo = new THREE.TorusGeometry(SPHERE_RADIUS * 1.18, 0.012, 8, 128);
    const band = new THREE.Mesh(geo, bandMat);
    band.rotation.set(rx, ry, rz);

    const halo1Geo = new THREE.TorusGeometry(SPHERE_RADIUS * 1.18, 0.04, 8, 128);
    band.add(new THREE.Mesh(halo1Geo, bandHaloMat1));
    const halo2Geo = new THREE.TorusGeometry(SPHERE_RADIUS * 1.18, 0.08, 8, 128);
    band.add(new THREE.Mesh(halo2Geo, bandHaloMat2));
    bandHaloGeos.push(halo1Geo, halo2Geo);

    core.add(band);
    return band;
  });

  // -- soft inner glow — an additive-blended sprite-like point behind everything.
  const glowGeo = new THREE.SphereGeometry(SPHERE_RADIUS * 0.65, 24, 24);
  const glowMat = tint(new THREE.MeshBasicMaterial({ color: palette.glow, transparent: true, opacity: 0.06 }), "glow");
  core.add(new THREE.Mesh(glowGeo, glowMat));

  // -- pulsing bright core — the "arc reactor" heart of the MCU look, a
  // small solid-looking sphere that breathes in scale/opacity rather than
  // sitting static, so the whole thing reads as alive, not just spinning
  // geometry. Two nested layers (a tight bright center + a softer halo just
  // outside it) instead of one flat sphere, since a single MeshBasicMaterial
  // sphere reads as a flat disc from most angles without real lighting.
  const coreCenterGeo = new THREE.SphereGeometry(SPHERE_RADIUS * 0.16, 24, 24);
  const coreCenterMat = tint(new THREE.MeshBasicMaterial({ color: palette.coreCenter, transparent: true, opacity: 0.95 }), "coreCenter");
  const coreCenter = new THREE.Mesh(coreCenterGeo, coreCenterMat);
  core.add(coreCenter);
  const coreHaloGeo = new THREE.SphereGeometry(SPHERE_RADIUS * 0.3, 24, 24);
  const coreHaloMat = tint(new THREE.MeshBasicMaterial({ color: palette.coreHalo, transparent: true, opacity: 0.25 }), "coreHalo");
  const coreHalo = new THREE.Mesh(coreHaloGeo, coreHaloMat);
  core.add(coreHalo);

  // -- nested broken spheres (David's ask 2026-09-01: "spheres inside
  // broken spheres within more spheres", replacing the earlier ring-shell
  // gyroscope) — each layer is an icosahedron wireframe with a random
  // fraction of its edge segments dropped, so it reads as fractured/
  // incomplete shell fragments rather than a clean solid, layered
  // concentrically around the circuit-trace sphere. Built from
  // EdgesGeometry's own output (pairs of points per line segment) filtered
  // down, not a custom edge walk, so the break pattern still follows real
  // icosahedron geometry instead of random noise.
  function buildBrokenSphere(radius, detail, keepFraction, color, opacity) {
    const srcGeo = new THREE.IcosahedronGeometry(radius, detail);
    const edges = new THREE.EdgesGeometry(srcGeo, 1);
    const src = edges.attributes.position.array;
    const kept = [];
    for (let i = 0; i < src.length; i += 6) { // 6 floats = one segment's two endpoints
      if (Math.random() < keepFraction) {
        for (let k = 0; k < 6; k++) kept.push(src[i + k]);
      }
    }
    srcGeo.dispose();
    edges.dispose();
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(kept), 3));
    const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity });
    const mesh = new THREE.LineSegments(geo, mat);
    return { mesh, geo, mat };
  }

  // Trimmed from four nested layers to two (David's ask 2026-09-01: "get
  // rid of some of the cracked outer spheres, while still keeping some") —
  // keeps the innermost (dense, close to the core) and outermost (sparse,
  // far out) for depth contrast without the middle two layers' visual
  // clutter.
  const brokenSpecs = [
    { radius: SPHERE_RADIUS * 1.45, detail: 3, keep: 0.72, key: "brokenInner", opacity: 0.4, speed: 0.05, axis: "y" },
    { radius: SPHERE_RADIUS * 2.75, detail: 1, keep: 0.6, key: "brokenOuter", opacity: 0.15, speed: -0.018, axis: "y" },
  ];
  const brokenSpheres = brokenSpecs.map((spec) => {
    const built = buildBrokenSphere(spec.radius, spec.detail, spec.keep, palette[spec.key], spec.opacity);
    tint(built.mat, spec.key);
    core.add(built.mesh);
    return { ...built, ...spec };
  });

  const ambient = new THREE.AmbientLight(palette.ambient);
  tint(ambient, "ambient");
  scene.add(ambient);

  let raf = null;
  let disposed = false;
  const clock = new THREE.Clock();

  function resize() {
    const { clientWidth: w, clientHeight: h } = container;
    if (w === 0 || h === 0) return;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  function animate() {
    if (disposed) return;
    const t = clock.getElapsedTime();
    core.rotation.y = t * 0.1;
    core.rotation.x = Math.sin(t * 0.06) * 0.15;
    bands.forEach((band, i) => { band.rotation.z += 0.0007 * (i + 1); });
    brokenSpheres.forEach((b) => { b.mesh.rotation[b.axis] += b.speed * 0.01; });
    const pulse = 1 + Math.sin(t * 1.6) * 0.08;
    coreCenter.scale.setScalar(pulse);
    coreCenterMat.opacity = 0.85 + Math.sin(t * 1.6) * 0.1;
    coreHalo.scale.setScalar(1 + Math.sin(t * 1.6 + 0.5) * 0.12);
    renderer.render(scene, camera);
    raf = requestAnimationFrame(animate);
  }

  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);

  // Recolor in place when Developer Mode flips. Materials are shared by
  // reference across meshes, so setting each one's color once repaints
  // everything that uses it on the next frame — no rebuild, no reallocation.
  function applyPalette() {
    const next = activePalette();
    if (next === palette) return;
    palette = next;
    for (const [material, key] of tinted) material.color.setHex(palette[key]);
  }
  const themeObserver = new MutationObserver(applyPalette);
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });

  resize();
  animate();

  return function dispose() {
    disposed = true;
    if (raf) cancelAnimationFrame(raf);
    resizeObserver.disconnect();
    themeObserver.disconnect();
    traceGeo.dispose(); traceMat.dispose();
    baseGeo.dispose(); baseEdges.dispose(); baseMat.dispose();
    pointsGeo.dispose(); pointsMat.dispose();
    bands.forEach((b) => b.geometry.dispose());
    bandMat.dispose();
    bandHaloGeos.forEach((g) => g.dispose());
    bandHaloMat1.dispose(); bandHaloMat2.dispose();
    glowGeo.dispose(); glowMat.dispose();
    coreCenterGeo.dispose(); coreCenterMat.dispose();
    coreHaloGeo.dispose(); coreHaloMat.dispose();
    brokenSpheres.forEach((b) => { b.geo.dispose(); b.mat.dispose(); });
    renderer.dispose();
    if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
  };
}
