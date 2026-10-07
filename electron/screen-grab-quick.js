const message = document.getElementById("message");
const preview = document.getElementById("preview");
const previewCard = document.getElementById("preview-card");
const remove = document.getElementById("remove");
const status = document.getElementById("status");
let image = null;
let modelInfo = [];
let sending = false;
const modelSelect = document.getElementById("model");
async function refreshModels() {
  try {
    const models = await window.screenGrab.models();
    modelInfo = models;
    let preferred = null;
    try { preferred = localStorage.getItem("jarvis:quick-model"); } catch { /* Optional. */ }
    modelSelect.replaceChildren(new Option("Choose model", ""));
    for (const model of models) modelSelect.add(new Option(model.label, model.id));
    if (models.some(model => model.id === preferred)) modelSelect.value = preferred;
    else if (models.length === 1) modelSelect.value = models[0].id;
  } catch { status.textContent = "Open Kairos to choose a model."; }
}
refreshModels();
window.screenGrab.onShown(refreshModels);
modelSelect.onchange = () => {
  try { localStorage.setItem("jarvis:quick-model", modelSelect.value); } catch { /* Optional. */ }
};

document.getElementById("close").onclick = () => window.screenGrab.closeQuick();
document.getElementById("capture").onclick = async () => {
  status.textContent = "";
  try {
    const captured = await window.screenGrab.capture();
    if (!captured) return;
    image = captured;
    preview.src = captured;
    previewCard.style.display = "block";
    updateSize();
  } catch (error) { status.textContent = error.message; }
};
remove.onclick = () => {
  image = null;
  preview.removeAttribute("src");
  previewCard.style.display = "none";
  updateSize();
};
document.getElementById("send").onclick = () => {
  if (sending) return;
  if (!message.value.trim() && !image) return;
  if (!modelSelect.value) { status.textContent = "Choose a model before sending."; modelSelect.focus(); return; }
  if (image && !modelInfo.find(model => model.id === modelSelect.value)?.supportsImages) {
    status.textContent = "This model isn't marked for image input. Choose a vision model.";
    modelSelect.focus();
    return;
  }
  window.screenGrab.sendQuick({ text: message.value, image, modelEndpointId: modelSelect.value });
  sending = true;
  document.getElementById("send").disabled = true;
  status.textContent = "Sending…";
};
window.screenGrab.onQuickResult(result => {
  sending = false;
  document.getElementById("send").disabled = false;
  if (result?.ok) {
    message.value = "";
    remove.click();
    status.textContent = "";
  } else {
    status.textContent = result?.error || "The draft stayed here. Try again.";
  }
});
document.getElementById("settings-toggle").onclick = async () => {
  const settings = document.getElementById("settings");
  settings.style.display = settings.style.display === "block" ? "none" : "block";
  updateSize();
  if (settings.style.display === "block") document.getElementById("shortcut").value = await window.screenGrab.shortcut();
};
function updateSize() {
  const settingsOpen = document.getElementById("settings").style.display === "block";
  window.screenGrab.quickSize(338 + (image ? 180 : 0) + (settingsOpen ? 82 : 0));
}
document.getElementById("save-shortcut").onclick = async () => {
  const result = await window.screenGrab.setShortcut(document.getElementById("shortcut").value.trim());
  status.textContent = result.ok ? "Shortcut saved." : result.error;
};
document.addEventListener("keydown", event => {
  if (event.key === "Escape") window.screenGrab.closeQuick();
  if (event.key === "Enter" && !event.shiftKey && document.activeElement === message) {
    event.preventDefault();
    document.getElementById("send").click();
  }
});
message.focus();
