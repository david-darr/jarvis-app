import { el } from '../api.js';
import { getAppearance, updateAppearance, setAppearanceImage, removeAppearanceImage,
  resetAppearance, setAppearancePreview } from '../appearance.js';

export function renderAppearancePanel(content) {
  const root = el('section', { class: 'appearance-panel' });
  const status = el('p', { class: 'appearance-status meta', role: 'status', 'aria-live': 'polite' });
  const preview = el('canvas', { width: '640', height: '280', 'aria-hidden': 'true' });
  const previewFrame = el('div', { class: 'appearance-preview' }, [preview,
    el('div', { class: 'appearance-preview-copy' }, [el('span', { text: 'YOUR WORKSPACE' }), el('strong', { text: 'Make room for your own style.' })]),
    el('span', { class: 'appearance-preview-badge', text: 'Live preview' }),
  ]);
  const modeButtons = new Map();
  const modes = el('div', { class: 'appearance-modes', role: 'group', 'aria-label': 'Background style' });
  for (const [mode, title, subtitle] of [['default', 'Original', 'Quiet & familiar'], ['color', 'Color', 'A clean canvas'], ['image', 'Image', 'Your perspective'], ['shader', 'Flow', 'Color in motion']]) {
    const button = el('button', { type: 'button', class: 'appearance-mode', 'data-mode': mode, onclick: () => {
      updateAppearance({ mode }); sync();
    } }, [el('span', { class: `appearance-mode-art art-${mode}`, 'aria-hidden': 'true' }), el('strong', { text: title }), el('span', { text: subtitle })]);
    modeButtons.set(mode, button); modes.append(button);
  }
  const color = el('input', { type: 'color', id: 'appearance-color', 'aria-label': 'Custom base color' });
  const hex = el('span', { class: 'appearance-color-value' });
  const swatches = el('div', { class: 'appearance-swatches', role: 'group', 'aria-label': 'Base colors' });
  for (const [name, value] of [['Graphite', '#202127'], ['Forest', '#23302e'], ['Ocean', '#233447'], ['Plum', '#3c2943'], ['Clay', '#533c32'], ['Stone', '#a7aca5'], ['Cream', '#e6dfd1']]) {
    swatches.append(el('button', { type: 'button', title: name, 'aria-label': name, 'data-color': value,
      style: `--swatch:${value}`, onclick: () => { updateAppearance({ color: value }); sync(); } }));
  }
  color.addEventListener('input', () => { updateAppearance({ color: color.value }); sync(); });
  const palette = el('div', { class: 'appearance-palette' }, [
    el('label', { for: 'appearance-color', class: 'title', text: 'Base color' }),
    el('div', { class: 'appearance-color-row' }, [swatches, color, hex]),
    el('p', { class: 'meta', text: 'Your sidebar follows in a slightly darker shade.' }),
  ]);

  const controls = new Map();
  function dial(key, title, description) {
    const output = el('output', { for: `appearance-${key}` });
    const input = el('input', { type: 'range', id: `appearance-${key}`, min: '0', max: '100', step: '1', 'aria-label': title });
    const knob = el('div', { class: 'appearance-knob' }, [el('span', { 'aria-hidden': 'true' }), input]);
    input.addEventListener('input', () => { updateAppearance({ [key]: Number(input.value) / 100 }); sync(); });
    let drag = null;
    input.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault(); input.focus(); input.setPointerCapture(event.pointerId);
      drag = { x: event.clientX, y: event.clientY, value: Number(input.value) };
    });
    input.addEventListener('pointermove', event => {
      if (!drag) return;
      input.value = String(Math.max(0, Math.min(100, drag.value + (event.clientX - drag.x) * .6 + (drag.y - event.clientY) * .6)));
      input.dispatchEvent(new Event('input'));
    });
    input.addEventListener('pointerup', () => { drag = null; });
    input.addEventListener('lostpointercapture', () => { drag = null; });
    input.addEventListener('pointercancel', () => { drag = null; });
    const card = el('div', { class: 'appearance-dial' }, [knob,
      el('div', {}, [el('label', { for: input.id, text: title }), el('p', { text: description })]), output]);
    controls.set(key, { input, output, knob });
    return card;
  }
  const upload = el('input', { type: 'file', id: 'appearance-image', accept: 'image/png,image/jpeg,image/webp' });
  const imageLabel = el('label', { for: upload.id, class: 'appearance-upload', text: 'Choose an image' });
  const remove = el('button', { type: 'button', class: 'btn quiet', text: 'Remove image', onclick: async () => {
    try { await removeAppearanceImage(); sync(); } catch { status.textContent = 'Image could not be removed. Try again.'; }
  } });
  upload.addEventListener('change', async () => {
    const file = upload.files[0]; if (!file) return;
    upload.disabled = true; status.textContent = 'Preparing your image…';
    try { await setAppearanceImage(file); sync(); }
    catch (error) { status.textContent = error.message; }
    finally { upload.disabled = false; upload.value = ''; }
  });
  const imageOptions = el('div', { class: 'appearance-image-options' }, [
    el('div', { class: 'appearance-upload-row' }, [imageLabel, upload, remove]),
    el('p', { class: 'meta', text: 'PNG, JPEG or WebP · Up to 12 MB · Stored only on this device' }),
    dial('tint', 'Tint', 'Blend your base color over the image.'),
  ]);
  const motion = el('input', { type: 'checkbox', id: 'appearance-motion' });
  motion.addEventListener('change', () => { updateAppearance({ motion: motion.checked }); sync(); });
  const shaderOptions = el('div', { class: 'appearance-shader-options' }, [
    el('div', { class: 'appearance-dials' }, [
      dial('distortion', 'Distortion', 'Bend the color field.'), dial('swirl', 'Swirl', 'Twist the flow.'),
      dial('grainMixer', 'Grain mixer', 'Break up the color blend.'), dial('grainOverlay', 'Grain overlay', 'Add a fine texture.'),
    ]), el('label', { class: 'appearance-motion', for: motion.id }, [motion, el('span', { text: 'Animate background' })]),
    el('p', { class: 'meta', text: 'Motion pauses in the background and follows your reduced-motion preference.' }),
  ]);
  const reset = el('button', { type: 'button', class: 'btn quiet', text: 'Reset appearance', onclick: async () => {
    try { await resetAppearance(); sync(); } catch { status.textContent = 'Appearance could not be reset. Try again.'; }
  } });
  const overlay = window.jarvis?.usageOverlay;
  const overlayToggle = el('input', { type: 'checkbox', id: 'usage-overlay-toggle' });
  const overlayNote = el('p', { class: 'meta', text: 'A notch at the screen edge showing your Claude Code and Codex account limits. Hidden by default.' });
  // The notch's edge and fold (design from CodeNotch; see static/css/usage-overlay.css)
  const overlayEdge = el('select', { id: 'usage-overlay-edge' }, [
    el('option', { value: 'right', text: 'Right edge' }), el('option', { value: 'left', text: 'Left edge' })]);
  const overlayFold = el('input', { type: 'checkbox', id: 'usage-overlay-fold' });
  const overlaySection = el('section', { class: 'appearance-overlay-setting' }, [
    el('h3', { text: 'Desktop usage overlay' }),
    el('label', { for: overlayToggle.id, class: 'appearance-motion' }, [overlayToggle, el('span', { text: 'Show above other windows' })]),
    el('label', { for: overlayEdge.id, class: 'appearance-motion' }, [el('span', { text: 'Screen edge' }), overlayEdge]),
    el('label', { for: overlayFold.id, class: 'appearance-motion' }, [overlayFold, el('span', { text: 'Fold to a sliver until the pointer reaches it' })]),
    overlayNote,
  ]);
  if (overlay) {
    const syncOverlay = state => {
      overlayToggle.disabled = !state.supported;
      overlayToggle.checked = !!state.visible;
      overlayEdge.disabled = overlayFold.disabled = !state.supported;
      overlayEdge.value = state.edge === 'left' ? 'left' : 'right';
      overlayFold.checked = state.foldOnHover !== false;
      if (!state.supported) overlayNote.textContent = 'The desktop overlay is currently available on Windows.';
    };
    overlay.state().then(syncOverlay).catch(() => { overlayNote.textContent = 'Overlay setting unavailable.'; });
    const unsubscribe = overlay.onState(syncOverlay);
    const observer = new MutationObserver(() => {
      if (!root.isConnected) { unsubscribe(); observer.disconnect(); }
    });
    observer.observe(content, { childList: true });
    overlayToggle.addEventListener('change', async () => {
      overlayToggle.disabled = true;
      try { syncOverlay(await overlay.setVisible(overlayToggle.checked)); }
      catch { overlayNote.textContent = 'Could not change the overlay. Try again.'; overlayToggle.disabled = false; }
    });
    const saveConfig = async (changes) => {
      try { syncOverlay(await overlay.setConfig(changes)); }
      catch { overlayNote.textContent = 'Could not change the overlay. Try again.'; }
    };
    overlayEdge.addEventListener('change', () => saveConfig({ edge: overlayEdge.value }));
    overlayFold.addEventListener('change', () => saveConfig({ foldOnHover: overlayFold.checked }));
  } else {
    overlayToggle.disabled = overlayEdge.disabled = overlayFold.disabled = true;
    overlayNote.textContent = 'Open the Windows desktop app to use the overlay.';
  }
  root.append(el('div', { class: 'appearance-heading' }, [el('div', {}, [el('h2', { text: 'Appearance' }),
    el('p', { class: 'meta', text: 'A workspace that feels like yours.' })]), reset]), previewFrame, modes, palette, imageOptions, shaderOptions, overlaySection, status);
  content.replaceChildren(root);
  function sync() {
    const value = getAppearance();
    for (const [mode, button] of modeButtons) button.setAttribute('aria-pressed', String(mode === value.mode));
    palette.hidden = value.mode === 'default'; imageOptions.hidden = value.mode !== 'image'; shaderOptions.hidden = value.mode !== 'shader';
    color.value = value.color; hex.textContent = value.color.toUpperCase();
    for (const button of swatches.children) button.setAttribute('aria-pressed', String(button.dataset.color === value.color));
    for (const [key, control] of controls) {
      const number = Math.round(value[key] * 100);
      control.input.value = number; control.output.textContent = number + '%';
      control.knob.style.setProperty('--dial-angle', `${-135 + number * 2.7}deg`);
      control.knob.style.setProperty('--dial-fill', `${number * 2.7}deg`);
    }
    motion.checked = value.motion;
    remove.hidden = !value.hasImage;
    imageLabel.textContent = value.hasImage ? 'Replace image' : 'Choose an image';
    status.textContent = value.storageIssue || (value.staticFallback ? 'Motion is unavailable on this device. A static gradient is active.' :
      value.mode === 'image' && !value.hasImage ? 'Choose an image to finish your background.' : 'Saved for this user on this device.');
    setAppearancePreview(preview);
  }
  sync();
}
