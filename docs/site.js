const previews = {
  home: ["Your day, in focus.", "Home dashboard with a particle core, recent conversations, and upcoming events. Demo data."],
  chat: ["A little space to think.", "A focused conversation and floating composer with model controls. Demo data."],
  vault: ["A connected home for your knowledge.", "Vault graph with colored note triangles and linked folders. Demo data."],
  "sidebar-collapsed": ["More room. Everything still in reach.", "JARVIS Home with its sidebar collapsed to an icon rail. Demo data."],
};
const previewImage = document.getElementById("preview-image");
let selection = 0;
document.querySelectorAll("[data-preview]").forEach(button => {
  button.addEventListener("click", async () => {
    const key = button.dataset.preview;
    const version = ++selection;
    const source = "img/" + key + ".png";
    const image = new Image();
    image.src = source;
    try { await image.decode(); } catch (_) { return; }
    if (version !== selection) return;
    document.querySelectorAll("[data-preview]").forEach(item => {
      item.classList.toggle("active", item === button);
      item.setAttribute("aria-pressed", String(item === button));
    });
    previewImage.classList.remove("changing");
    previewImage.src = source;
    previewImage.alt = previews[key][1];
    document.getElementById("preview-link").href = source;
    document.getElementById("preview-caption").textContent = previews[key][0];
    requestAnimationFrame(() => previewImage.classList.add("changing"));
  });
});
