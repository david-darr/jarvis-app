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
  assert.equal(await js("document.querySelector('[data-mode=default] strong').textContent"), 'Kairos');
  assert.equal(await js("document.querySelector('.appearance-preview-copy strong').textContent"), 'Not more time. The right time.');
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

  // Custom halftone pictures (Build spec, 2026-10-08): Home, New chat
  // (figure) and Conversation (sky) each take an optional picture, stored
  // and validated the same way as the Image background.
  // A corner-to-corner gradient, not a flat fill: cropping a different region
  // (the focus point) has to change what's sampled on either axis, so a
  // focus change is visible regardless of which way the banner is cropped.
  const halftoneFile = (color, bytes) => bytes
    ? `new File([new Uint8Array(${bytes})], 'big.png', { type: 'image/png' })`
    : `await (async () => {
        const c = document.createElement('canvas'); c.width = 64; c.height = 64;
        const ctx = c.getContext('2d');
        const g = ctx.createLinearGradient(0, 0, 64, 64);
        g.addColorStop(0, '${color}'); g.addColorStop(1, '#111111');
        ctx.fillStyle = g; ctx.fillRect(0, 0, 64, 64);
        return new File([await new Promise(r => c.toBlob(r, 'image/png'))], 'halftone.png', { type: 'image/png' });
      })()`;
  const halftoneCardSel = slot => `document.querySelector('#appearance-halftone-${slot}').closest('.appearance-halftone-card')`;
  const halftoneResetSel = slot => `${halftoneCardSel(slot)}.querySelector('.appearance-halftone-card-actions > button')`;
  const halftoneThumbSrc = (slot) => js(`${halftoneCardSel(slot)}.querySelector('img').src`);
  // Waits on the thumbnail's object URL actually changing, not just "Reset
  // is visible": a slot replaced a second time already has Reset showing,
  // and halftoneRevision moves the instant the call starts (so it can tell
  // a stale load from a current one) rather than once it has landed - either
  // alone would let the assertion run before the new picture is truly saved.
  const setHalftoneSlot = async (slot, color) => {
    const before = await halftoneThumbSrc(slot);
    await js(`(async () => {
      const file = ${halftoneFile(color)};
      const transfer = new DataTransfer(); transfer.items.add(file);
      const input = document.querySelector('#appearance-halftone-${slot}');
      input.files = transfer.files; input.dispatchEvent(new Event('change'));
    })()`);
    await waitFor(`!${halftoneResetSel(slot)}.hidden`);
    await waitFor(`${halftoneCardSel(slot)}.querySelector('img').src !== ${JSON.stringify(before)}`);
  };
  const bannerPixels = () => js("document.querySelector('.dashboard-core canvas:not(.dither-cover)').toDataURL()");
  const waitForBannerChange = async (before) => {
    for (let i = 0; i < 40; i++) { const now = await bannerPixels(); if (now !== before) return now; await delay(50); }
    throw new Error('Home banner did not redraw');
  };
  await setHalftoneSlot('home', '#2f6cc2');
  assert.ok(await js(`${halftoneCardSel('home')}.querySelector('img').src.startsWith('blob:')`), 'Home thumbnail shows the custom picture');
  await waitFor("document.querySelector('.dashboard-core')?.dataset.halftoneSrc?.startsWith('blob:')");
  const customBanner = await waitForBannerChange('');
  // Clicking the thumbnail moves the focus point (default centre) and the banner redraws from it.
  assert.deepEqual(await js("import('/static/js/appearance.js').then(m => m.halftoneFocus('home'))"), { x: .5, y: .5 });
  await js(`{ const btn = ${halftoneCardSel('home')}.querySelector('.appearance-halftone-thumb-btn'), box = btn.getBoundingClientRect();
    btn.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: box.left + box.width * 0.1, clientY: box.top + box.height * 0.9 })); }`);
  const focusedBanner = await waitForBannerChange(customBanner);
  const movedFocus = await js("import('/static/js/appearance.js').then(m => m.halftoneFocus('home'))");
  assert.ok(movedFocus.x < 0.3 && movedFocus.y > 0.7, 'Focus moves to where the thumbnail was clicked');
  // Reset restores the Kairos default picture.
  await js(`${halftoneResetSel('home')}.click()`);
  await waitFor(`${halftoneResetSel('home')}.hidden`);
  assert.ok(await js("!document.querySelector('.dashboard-core').dataset.halftoneSrc.startsWith('blob:')"), 'Reset restores the default picture');
  await waitForBannerChange(focusedBanner);
  // Wrong type and over-size pictures are refused; the saved picture is untouched.
  await setHalftoneSlot('home', '#2f6cc2');
  assert.match(await js("import('/static/js/appearance.js').then(m=>m.setHalftoneImage('home', new File(['<svg/>'],'bad.svg',{type:'image/svg+xml'}))).then(()=>'',e=>e.message)"), /PNG/);
  assert.match(await js(`import('/static/js/appearance.js').then(m=>m.setHalftoneImage('home', ${halftoneFile(null, 13 * 1024 * 1024)})).then(()=>'',e=>e.message)`), /12 MB/);
  assert.equal(await js("import('/static/js/appearance.js').then(m=>m.getAppearance().halftoneSlots.home.hasImage)"), true, 'A rejected picture leaves the saved one in place');
  await js(`${halftoneResetSel('home')}.click()`);
  await waitFor(`${halftoneResetSel('home')}.hidden`);

  // New chat and Conversation each draw their own custom picture (a direct
  // module check: the full chat UI is exercised elsewhere in ui-smoke.cjs).
  // The same picture in both skips the cross-dissolve between them. (Each
  // slot keeps its own object URL even when the bytes match, so "the same
  // picture" is judged by content - halftoneSharedChatPicture's hash - not
  // by comparing the two URL strings, which would never be equal.)
  const chatBackdropCheck = () => js(`(async () => {
    const mod = await import('/static/js/chatBackdrop.js');
    const host = document.createElement('div'); host.style.width = '300px'; host.style.height = '200px'; document.body.append(host);
    const backdrop = mod.mountChatBackdrop(host);
    await backdrop.show('figure'); await backdrop.ready;
    const figureIsBlob = host.querySelector('.chat-backdrop').dataset.halftoneSrc.startsWith('blob:');
    const landed = backdrop.show('sky', { animate: true });
    await new Promise(r => setTimeout(r, 60));
    const dissolving = host.querySelector('.chat-backdrop').classList.contains('is-dissolving');
    await landed;
    const skyIsBlob = host.querySelector('.chat-backdrop').dataset.halftoneSrc.startsWith('blob:');
    backdrop.dispose(); host.remove();
    return { figureIsBlob, skyIsBlob, dissolving };
  })()`);
  await setHalftoneSlot('figure', '#c2452f');
  await setHalftoneSlot('sky', '#3f9f5f');
  assert.deepEqual(await chatBackdropCheck(), { figureIsBlob: true, skyIsBlob: true, dissolving: true },
    'New chat and Conversation draw their own pictures and dissolve between different ones');
  // The identical picture in both slots: uploaded from the one File object,
  // so the two slots are guaranteed the same bytes (appearance.js hashes the
  // original upload, not its downsampled copy, since re-encoding the same
  // picture twice isn't guaranteed to produce identical bytes).
  await js(`(async () => { window.__halftoneShared = ${halftoneFile('#c2452f')}; })()`);
  const setHalftoneSlotFromWindowFile = async (slot) => {
    const before = await halftoneThumbSrc(slot);
    await js(`(async () => {
      const transfer = new DataTransfer(); transfer.items.add(window.__halftoneShared);
      const input = document.querySelector('#appearance-halftone-${slot}');
      input.files = transfer.files; input.dispatchEvent(new Event('change'));
    })()`);
    await waitFor(`!${halftoneResetSel(slot)}.hidden`);
    await waitFor(`${halftoneCardSel(slot)}.querySelector('img').src !== ${JSON.stringify(before)}`);
  };
  await setHalftoneSlotFromWindowFile('figure');
  await setHalftoneSlotFromWindowFile('sky');
  assert.equal(await js("import('/static/js/appearance.js').then(m=>m.halftoneSharedChatPicture())"), true, 'The same picture in both chat slots is recognised');
  assert.deepEqual(await chatBackdropCheck(), { figureIsBlob: true, skyIsBlob: true, dissolving: false },
    'The same picture in both chat slots skips the dissolve');
  await js(`${halftoneResetSel('figure')}.click(); ${halftoneResetSel('sky')}.click();`);
  await waitFor(`${halftoneResetSel('figure')}.hidden && ${halftoneResetSel('sky')}.hidden`);
  await capture('appearance-halftone-slots');
  // The phone-width capture of these cards happens at the very end of this
  // file instead of here: resizing the real window and back disturbs its
  // coordinates for the CDP mouse drag further down, so every other size
  // change in this file already waits until after that drag is done.

  await set({ mode: 'shader', color: '#f3eadb', motion: false });
  await capture('appearance-flow-parchment');
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
  assert.ok(!await js("getComputedStyle(document.querySelector('.chat-layout')).backgroundImage.includes('kairos-sky.jpg')"), 'Flow appearance keeps its chosen background');
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
  await waitFor("[...document.querySelectorAll('.dashboard-stat:not([data-stat=waiting]) .dashboard-stat-value')].filter(n => n.textContent !== '…').length === 4");
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
  await waitFor("[...document.querySelectorAll('.dashboard-stat:not([data-stat=waiting]) .dashboard-stat-value')].filter(n => n.textContent !== '…').length === 4");
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
  await waitFor("[...document.querySelectorAll('.dashboard-stat:not([data-stat=waiting]) .dashboard-stat-value')].filter(n => n.textContent !== '…').length === 4");
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
  await waitFor("[...document.querySelectorAll('.dashboard-stat:not([data-stat=waiting]) .dashboard-stat-value')].filter(n => n.textContent !== '…').length === 4");
  assert.equal((await state()).hasImage, false, 'Reset removes persisted image');
  // The halftone slot cards at phone width (mode is back to Kairos default,
  // which shows them): the window is already narrow here, with nothing left
  // that depends on real screen coordinates (the CDP mouse drag is done).
  await open();
  assert.ok(await js("document.querySelector('.appearance-panel').scrollWidth<=document.querySelector('.appearance-panel').clientWidth"), 'Halftone slot cards fit a phone');
  await capture('mobile-appearance-halftone-slots');
  win.setContentSize(1440, 900); await delay(350);
};
