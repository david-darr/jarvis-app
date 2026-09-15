const assert = require('node:assert/strict');

module.exports = async function checkAppearance({ js, win, waitFor, capture, base, delay }) {
  const open = async () => {
    await js("document.querySelector('.sidebar-settings-btn').click()");
    await waitFor("!!document.querySelector('.modal-backdrop:not(.hidden) .settings-nav, #view-content.settings-mobile-page .settings-nav')");
    await waitFor("!!document.querySelector('[data-section=appearance]')");
    await js("document.querySelector('[data-section=appearance]').click()");
    await waitFor("!!document.querySelector('.appearance-panel')");
  };
  const state = () => js("import('/static/js/appearance.js').then(m=>m.getAppearance())");
  const set = async value => {
    await js(`import('/static/js/appearance.js').then(m=>m.updateAppearance(${JSON.stringify(value)}))`);
    await js("if(document.querySelector('.appearance-panel')) document.querySelector('[data-section=appearance]').click()");
  };
  const preview = () => js("document.querySelector('.appearance-preview canvas').toDataURL()");
  await open();
  assert.equal(await js("document.querySelectorAll('.appearance-mode').length"), 4);
  await js("document.querySelector('[data-mode=color]').click()");
  await set({ mode: 'color', color: '#23302e' });
  assert.equal(await js("getComputedStyle(document.querySelector('#sidebar')).backgroundColor"), 'rgb(29, 39, 38)');
  // Light colors switch foregrounds, including the independently darker rail.
  await set({ color: '#e6dfd1' });
  const contrast = await js(`(() => {
    const c = getComputedStyle(document.querySelector('#sidebar'));
    const l = s => s.match(/[0-9.]+/g).slice(0,3).map(Number).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
    const a=l(c.backgroundColor), b=l(c.color); return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
  })()`);
  assert.ok(contrast >= 4.5, 'Light sidebar text stays readable');
  await capture('appearance-light');
  await set({ mode: 'shader', color: '#23302e', motion: false });
  await js("document.querySelector('[data-section=appearance]').click()");
  assert.equal((await state()).staticFallback, false, 'Shader compiles and runs');
  for (const key of ['distortion', 'swirl', 'grainMixer', 'grainOverlay']) {
    await set({ [key]: 0 }); const before = await preview();
    await js(`{ const input=document.querySelector('#appearance-${key}'); input.value='100'; input.dispatchEvent(new Event('input')); }`);
    assert.equal((await state())[key], 1);
    assert.notEqual(await preview(), before, key + ' changes rendered pixels');
    await set({ [key]: .25 });
  }
  await js("document.querySelector('#appearance-swirl').focus()");
  await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39 });
  await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39 });
  assert.equal((await state()).swirl, .26, 'Dial is keyboard accessible');
  // Real pointer input, not a synthetic dispatch: the dial calls
  // setPointerCapture(event.pointerId), which throws for a made-up id, so a
  // hand-built PointerEvent would test nothing and quietly report success.
  // CDP mouse events give Chromium's own pointer id.
  await set({ distortion: 0 });
  await js("document.querySelector('[data-section=appearance]').click()");
  const knobBox = await js("(() => { const r = document.querySelector('#appearance-distortion').getBoundingClientRect(); return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }; })()");
  const flat = await preview();
  const drag = (type, x, y) => win.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {
    type, x, y, button: 'left', buttons: type === 'mouseReleased' ? 0 : 1, clickCount: 1, pointerType: 'mouse',
  });
  await drag('mousePressed', knobBox.x, knobBox.y);
  await drag('mouseMoved', knobBox.x + 100, knobBox.y);
  await drag('mouseReleased', knobBox.x + 100, knobBox.y);
  const dragged = (await state()).distortion;
  // 100px right at the handler's 0.6/px equals 60 of 100, so ~0.6. Asserted
  // with tolerance because the press lands on a rounded centre pixel.
  assert.ok(Math.abs(dragged - .6) < .08, `Pointer drag moves the dial (got ${dragged})`);
  assert.notEqual(await preview(), flat, 'Pointer drag changes rendered pixels');
  // Releasing must end the drag; without pointer capture teardown the dial
  // would keep tracking the cursor across the whole panel.
  await drag('mouseMoved', knobBox.x + 200, knobBox.y);
  assert.equal((await state()).distortion, dragged, 'Dial stops tracking after release');
  await set({ distortion: .65, swirl: .65, grainMixer: .15, grainOverlay: .18 });
  await js("document.querySelector('[data-section=appearance]').click()");
  await capture('appearance-flow-settings');
  await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  await set({ motion: true }); const still = await preview();
  await delay(180); assert.equal(await preview(), still, 'Reduced motion holds the shader still');
  await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [] });
  await delay(180); assert.notEqual(await preview(), still, 'Shader motion resumes');
  await set({ motion: false });
  // WebGL contexts are lost for reasons outside the app's control (driver
  // reset, GPU process crash, too many live contexts). The shader must fall
  // back to the static gradient rather than leaving a dead black canvas
  // behind the whole UI, and recover if the context comes back.
  await set({ mode: 'shader' });
  await js("document.querySelector('[data-section=appearance]').click()");
  assert.equal((await state()).staticFallback, false, 'Shader is running before the loss test');
  // The extension handle is grabbed once and kept: getExtension() returns
  // null on an already-lost context, so re-fetching it to restore would fail
  // with "no WEBGL_lose_context" rather than actually restoring anything.
  assert.equal(await js(`(() => {
    const gl = [...document.querySelectorAll('canvas.app-background')].map(c => c.getContext('webgl')).find(Boolean);
    if (!gl) return 'no webgl canvas';
    window.__loseContextExt = gl.getExtension('WEBGL_lose_context');
    return window.__loseContextExt ? 'ok' : 'no WEBGL_lose_context';
  })()`), 'ok', 'WEBGL_lose_context is available to drive the test');
  const loseContext = (method) => js(`(() => { window.__loseContextExt.${method}(); return 'ok'; })()`);
  const shaderFrame = await preview();
  assert.equal(await loseContext('loseContext'), 'ok');
  await waitFor("import('/static/js/appearance.js').then(m=>m.getAppearance().staticFallback===true)");
  assert.equal((await state()).staticFallback, true, 'Lost context falls back to the static gradient');
  // Must actually repaint, not just flip a flag and leave the dead frame up.
  // Comparing against the live shader frame catches a blank canvas too, which
  // a data-URL length check would happily pass.
  const fallbackFrame = await preview();
  assert.notEqual(fallbackFrame, shaderFrame, 'Static fallback repaints instead of leaving the lost frame');
  assert.ok(
    await js("(() => { const c = document.querySelector('.appearance-preview canvas'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; for (let i = 0; i < d.length; i += 4) { if (d[i] || d[i+1] || d[i+2]) return true; } return false; })()"),
    'Static fallback paints real pixels rather than an empty canvas',
  );
  await capture('appearance-static-fallback');
  assert.equal(await loseContext('restoreContext'), 'ok');
  await waitFor("import('/static/js/appearance.js').then(m=>m.getAppearance().staticFallback===false)");
  assert.equal((await state()).staticFallback, false, 'Restored context returns to the shader');
  await js("document.querySelector(\".settings-titlebar-btn[title='Close']\").click()");
  await js("import('/static/js/app.js').then(m=>m.switchTab('chat'))");
  await capture('appearance-flow-workspace');
  await open();
  await js("document.querySelector('[data-mode=image]').click()");
  await js(`(async () => {
    const c=document.createElement('canvas'); c.width=640; c.height=360; const x=c.getContext('2d');
    const g=x.createLinearGradient(0,0,640,360); g.addColorStop(0,'#7e9294'); g.addColorStop(1,'#c29e80'); x.fillStyle=g; x.fillRect(0,0,640,360);
    x.fillStyle='#315659'; x.beginPath(); x.ellipse(360,420,450,230,-.25,0,7); x.fill();
    const blob=await new Promise(resolve=>c.toBlob(resolve,'image/png'));
    const transfer=new DataTransfer(); transfer.items.add(new File([blob],'fixture.png',{type:'image/png'}));
    const input=document.querySelector('#appearance-image'); input.files=transfer.files; input.dispatchEvent(new Event('change'));
  })()`);
  await waitFor("document.querySelector('.appearance-upload').textContent==='Replace image'");
  assert.equal((await state()).hasImage, true);
  await set({ tint: .25 }); const untinted = await preview();
  await set({ tint: .8 }); assert.notEqual(await preview(), untinted, 'Image tint changes pixels');
  await set({ tint: .35 });
  await capture('appearance-image-settings');
  await win.loadURL(base);
  await waitFor("document.querySelectorAll('.dashboard-stat').length===4");
  assert.equal((await state()).mode, 'image'); assert.equal((await state()).hasImage, true, 'Image survives reload');
  // A different signed-in user on this device receives their own preferences.
  await js("import('/static/js/appearance.js').then(m=>m.initAppearance('Other fixture user'))");
  assert.equal((await state()).mode, 'default'); assert.equal((await state()).hasImage, false);
  await js("import('/static/js/appearance.js').then(m=>m.initAppearance('Alex'))");
  assert.equal((await state()).hasImage, true);
  await open();
  // Invalid images are rejected without discarding the saved image.
  assert.match(await js("import('/static/js/appearance.js').then(m=>m.setAppearanceImage(new File(['<svg/>'],'bad.svg',{type:'image/svg+xml'}))).then(()=>'',e=>e.message)"), /PNG/);
  // Image writes are serialised through a promise queue and guarded by
  // revision counters. The invariant that matters is that memory and storage
  // never disagree: a lost race would either leave an orphaned blob that a
  // reload resurrects, or clear storage while memory still shows an image.
  // Both are only visible after a reload, so each case is checked across one.
  const fileExpr = (color) => `(async () => { const c=document.createElement('canvas'); c.width=64; c.height=64; const x=c.getContext('2d'); x.fillStyle='${color}'; x.fillRect(0,0,64,64); return new File([await new Promise(r=>c.toBlob(r,'image/png'))],'race.png',{type:'image/png'}); })()`;
  // Two writes started without awaiting the first: the later must win, and
  // storage must hold exactly what memory reports.
  assert.equal(await js(`(async () => {
    const mod = await import('/static/js/appearance.js');
    const [a, b] = await Promise.all([${fileExpr('#c2452f')}, ${fileExpr('#2f6cc2')}]);
    await Promise.allSettled([mod.setAppearanceImage(a), mod.setAppearanceImage(b)]);
    return 'done';
  })()`), 'done');
  assert.equal((await state()).hasImage, true, 'Concurrent image writes leave an image in memory');
  await win.loadURL(base);
  await waitFor("document.querySelectorAll('.dashboard-stat').length===4");
  assert.equal((await state()).hasImage, true, 'Concurrent image writes persist consistently');
  // A reset racing an in-flight write must win outright. If the write landed
  // after the delete, the image would come back on the next launch.
  assert.equal(await js(`(async () => {
    const mod = await import('/static/js/appearance.js');
    const pending = mod.setAppearanceImage(await ${fileExpr('#3f7f5f')});
    await Promise.allSettled([pending, mod.resetAppearance()]);
    return 'done';
  })()`), 'done');
  assert.equal((await state()).hasImage, false, 'Reset wins the race in memory');
  await win.loadURL(base);
  await waitFor("document.querySelectorAll('.dashboard-stat').length===4");
  assert.equal((await state()).hasImage, false, 'Reset racing a write does not resurrect the image');
  assert.equal((await state()).mode, 'default');
  await open();
  await set({ mode: 'shader' });
  win.setContentSize(390, 844); await delay(100);
  await js("document.querySelector(\".settings-titlebar-btn[title='Close']\").click()");
  await open();
  assert.ok(await js("document.querySelector('.appearance-panel').scrollWidth<=document.querySelector('.appearance-panel').clientWidth"), 'Appearance fits mobile');
  await capture('appearance-mobile');
  await js("import('/static/js/appearance.js').then(m=>m.resetAppearance())");
  assert.equal((await state()).hasImage, false);
  assert.equal((await state()).mode, 'default');
  await win.loadURL(base);
  await waitFor("document.querySelectorAll('.dashboard-stat').length===4");
  assert.equal((await state()).hasImage, false, 'Reset removes persisted image');
  win.setContentSize(1440, 900); await delay(350);
};
