// One person-initiated still from the browser running Kairos. The browser's
// picker owns source choice; Kairos never receives an ongoing screen stream.
export function openWebCapture({ onAttach, onClose }) {
  const previousFocus = document.activeElement;
  // Only offer browser picking when it can keep the capturing tab focused.
  // Other browsers can still attach a screenshot through the upload choice.
  const pickerAvailable = !!navigator.mediaDevices?.getDisplayMedia && window.isSecureContext
    && typeof window.CaptureController === "function"
    && typeof window.CaptureController.prototype?.setFocusBehavior === "function";
  const dialog = document.createElement("div");
  dialog.className = "screen-capture-dialog";
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-label", "Capture from this device");
  dialog.innerHTML = `
    <section class="screen-capture-panel">
      <header class="screen-capture-header">
        <div><span class="screen-capture-eyebrow">ADD AN IMAGE</span><h2>Capture from this device</h2></div>
        <button type="button" class="screen-capture-close" aria-label="Close capture">×</button>
      </header>
      <div class="screen-capture-choices">
        <button type="button" class="screen-capture-choice" data-action="screen">
          <span class="screen-capture-choice-icon" aria-hidden="true">▣</span>
          <span><strong>Choose a tab, window, or screen</strong><small>Your browser will ask what to share. Kairos takes one still.</small></span>
          <span class="screen-capture-arrow" aria-hidden="true">→</span>
        </button>
        <button type="button" class="screen-capture-choice" data-action="upload">
          <span class="screen-capture-choice-icon" aria-hidden="true">↑</span>
          <span><strong>Upload a screenshot</strong><small>Use a screenshot already saved on this device.</small></span>
          <span class="screen-capture-arrow" aria-hidden="true">→</span>
        </button>
      </div>
      <input class="screen-capture-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>
      <div class="screen-capture-review" hidden>
        <p>Drag on the image to crop it, or attach the full capture.</p>
        <div class="screen-capture-image-wrap"><img alt="Captured screen preview" draggable="false"><div class="screen-capture-selection" hidden></div></div>
        <div class="screen-capture-actions">
          <button type="button" data-action="retake">Retake</button>
          <button type="button" data-action="full">Attach full image</button>
          <button type="button" data-action="crop" disabled>Attach crop</button>
        </div>
      </div>
      <p class="screen-capture-status" role="status"></p>
      <footer>Only the image you attach joins the chat. Send stays in your control.</footer>
    </section>`;
  const choiceView = dialog.querySelector(".screen-capture-choices");
  const review = dialog.querySelector(".screen-capture-review");
  const status = dialog.querySelector(".screen-capture-status");
  const fileInput = dialog.querySelector(".screen-capture-file");
  const image = review.querySelector("img");
  const imageWrap = review.querySelector(".screen-capture-image-wrap");
  const selection = review.querySelector(".screen-capture-selection");
  const cropButton = review.querySelector('[data-action="crop"]');
  const screenButton = dialog.querySelector('[data-action="screen"]');
  let closed = false;
  let screenshot = null;
  let screenshotBlob = null;
  let screenshotUrl = null;
  let start = null;
  let crop = null;
  let attaching = false;

  function setStatus(message) { status.textContent = message || ""; }
  function clearScreenshot() {
    if (screenshotUrl) URL.revokeObjectURL(screenshotUrl);
    screenshotUrl = null;
    screenshot = screenshotBlob = crop = null;
    image.removeAttribute("src");
    selection.hidden = true;
    cropButton.disabled = true;
  }
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    clearScreenshot();
    dialog.remove();
    previousFocus?.focus?.();
    onClose?.();
  }
  function onKey(event) {
    if (event.key === "Escape") close();
    if (event.key !== "Tab") return;
    const controls = [...dialog.querySelectorAll("button:not([disabled]):not([hidden])")]
      .filter(button => button.getClientRects().length);
    if (!controls.length) return;
    if (event.shiftKey && document.activeElement === controls[0]) {
      event.preventDefault(); controls.at(-1).focus();
    } else if (!event.shiftKey && document.activeElement === controls.at(-1)) {
      event.preventDefault(); controls[0].focus();
    }
  }
  function showChoices() {
    clearScreenshot();
    review.hidden = true;
    choiceView.hidden = false;
    setStatus("");
    (pickerAvailable && !screenButton.disabled ? screenButton : dialog.querySelector('[data-action="upload"]')).focus();
  }
  function showReview(canvas, blob) {
    clearScreenshot();
    screenshot = canvas;
    screenshotBlob = blob;
    screenshotUrl = URL.createObjectURL(blob);
    image.src = screenshotUrl;
    choiceView.hidden = true;
    review.hidden = false;
    dialog.hidden = false;
    setStatus("");
    review.querySelector('[data-action="full"]').focus();
  }
  async function capture() {
    if (!pickerAvailable || closed) return;
    let stream = null;
    try {
      const controller = new window.CaptureController();
      try {
        // The current value is no-focus-change; older Chromium used the
        // original focus-capturing-application spelling.
        try { controller.setFocusBehavior("no-focus-change"); }
        catch { controller.setFocusBehavior("focus-capturing-application"); }
      } catch {
        screenButton.disabled = true;
        screenButton.querySelector("small").textContent = "This browser cannot keep Kairos in front. Upload a screenshot instead.";
        setStatus("This browser cannot keep Kairos in front. Upload a screenshot instead.");
        return;
      }
      // This call stays inside the click handler, before any await. Permission
      // cannot be cached or requested after an awaited UI transition.
      const request = navigator.mediaDevices.getDisplayMedia({ video: true, audio: false, controller });
      dialog.hidden = true;
      stream = await request;
      if (closed) return;
      const video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.srcObject = stream;
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("The selected source did not produce a frame.")), 10000);
        video.play().then(() => { clearTimeout(timer); resolve(); }, error => { clearTimeout(timer); reject(error); });
      });
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      if (closed) return;
      if (!video.videoWidth || !video.videoHeight) throw new Error("The selected source did not produce an image.");
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      video.srcObject = null;
      stream.getTracks().forEach(track => track.stop());
      stream = null;
      const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.86));
      if (!blob) throw new Error("The screenshot could not be prepared.");
      if (!closed) showReview(canvas, blob);
    } catch (error) {
      if (!closed) {
        dialog.hidden = false;
        if (error.name !== "NotAllowedError") setStatus(error.message || "Capture was cancelled.");
      }
    } finally {
      stream?.getTracks().forEach(track => track.stop());
      if (!closed) dialog.hidden = false;
    }
  }
  async function attach(blob) {
    if (!blob || closed || attaching) return;
    attaching = true;
    try {
      const file = new File([blob], `Screenshot-${Date.now()}.jpg`, { type: "image/jpeg" });
      const added = await onAttach(file);
      if (added) close();
      else setStatus("The screenshot could not be attached. Try again.");
    } catch (error) {
      if (!closed) setStatus(error.message || "The screenshot could not be attached.");
    } finally {
      attaching = false;
    }
  }
  function point(event) {
    const box = image.getBoundingClientRect();
    return { x: Math.max(0, Math.min(box.width, event.clientX - box.left)),
      y: Math.max(0, Math.min(box.height, event.clientY - box.top)) };
  }
  function updateSelection(end) {
    if (!start) return;
    crop = { x: Math.min(start.x, end.x), y: Math.min(start.y, end.y),
      width: Math.abs(end.x - start.x), height: Math.abs(end.y - start.y) };
    selection.style.left = `${crop.x}px`;
    selection.style.top = `${crop.y}px`;
    selection.style.width = `${crop.width}px`;
    selection.style.height = `${crop.height}px`;
    selection.hidden = false;
    cropButton.disabled = crop.width < 8 || crop.height < 8;
  }
  imageWrap.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !screenshot) return;
    start = point(event);
    imageWrap.setPointerCapture(event.pointerId);
    updateSelection(start);
  });
  imageWrap.addEventListener("pointermove", event => { if (start) updateSelection(point(event)); });
  imageWrap.addEventListener("pointerup", event => {
    if (start) updateSelection(point(event));
    start = null;
  });
  dialog.querySelector(".screen-capture-close").onclick = close;
  dialog.addEventListener("click", event => { if (event.target === dialog) close(); });
  screenButton.onclick = capture;
  if (!pickerAvailable) {
    screenButton.disabled = true;
    screenButton.querySelector("small").textContent = "This browser cannot keep Kairos in front. Upload a screenshot instead.";
  }
  dialog.querySelector('[data-action="upload"]').onclick = () => fileInput.click();
  fileInput.onchange = async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    try {
      const added = await onAttach(file);
      if (added) close();
      else setStatus("The screenshot could not be attached. Try again.");
    } catch (error) {
      if (!closed) setStatus(error.message || "The screenshot could not be attached.");
    } finally {
      fileInput.value = "";
    }
  };
  dialog.querySelector('[data-action="retake"]').onclick = showChoices;
  dialog.querySelector('[data-action="full"]').onclick = () => attach(screenshotBlob);
  cropButton.onclick = async () => {
    if (!crop || !screenshot || cropButton.disabled) return;
    const box = image.getBoundingClientRect();
    const scaleX = screenshot.width / box.width;
    const scaleY = screenshot.height / box.height;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(crop.width * scaleX));
    canvas.height = Math.max(1, Math.round(crop.height * scaleY));
    canvas.getContext("2d").drawImage(screenshot,
      Math.round(crop.x * scaleX), Math.round(crop.y * scaleY), canvas.width, canvas.height,
      0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.86));
    await attach(blob);
  };
  document.body.append(dialog);
  document.addEventListener("keydown", onKey);
  (pickerAvailable ? screenButton : dialog.querySelector('[data-action="upload"]')).focus();
  return close;
}
