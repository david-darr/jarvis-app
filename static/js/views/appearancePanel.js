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
  root.append(el('div', { class: 'appearance-heading' }, [el('div', {}, [el('h2', { text: 'Appearance' }),
    el('p', { class: 'meta', text: 'A workspace that feels like yours.' })]), reset]), previewFrame, modes, palette, imageOptions, shaderOptions, status);
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
