document.getElementById("cancel").onclick = () => window.screenGrab.cancel();
document.getElementById("region").onclick = () => window.screenGrab.region(document.getElementById("region-display").value);
document.addEventListener("keydown", event => { if (event.key === "Escape") window.screenGrab.cancel(); });
window.screenGrab.onError(message => { document.getElementById("error").textContent = message; });
let allSources = [];
let filter = "all";
const search = document.getElementById("search");
const filterButtons = [...document.querySelectorAll("[data-filter]")];
filterButtons.forEach(button => button.onclick = () => {
  filter = button.dataset.filter;
  filterButtons.forEach(item => {
    item.classList.toggle("active", item === button);
    item.setAttribute("aria-pressed", String(item === button));
  });
  renderSources();
});
search.oninput = renderSources;

function renderSources() {
  const host = document.getElementById("sources");
  host.replaceChildren();
  const query = search.value.trim().toLowerCase();
  const visible = allSources.filter(source =>
    (filter === "all" || source.kind === filter) && source.name.toLowerCase().includes(query));
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.id = "empty";
    empty.style.display = "block";
    empty.textContent = filter === "tab" ? "Open a page in JARVIS's side browser to capture it directly." :
      "No sources match this view.";
    host.append(empty);
  }
  for (const source of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "source";
    const image = document.createElement("img");
    image.src = source.image;
    image.alt = "";
    const meta = document.createElement("div");
    meta.className = "source-meta";
    const label = document.createElement("span");
    label.textContent = source.name;
    label.title = source.name;
    const kind = document.createElement("small");
    kind.textContent = source.kind === "screen" ? "Display" : source.kind === "tab" ? "Tab" : "Window";
    meta.append(label, kind);
    button.append(image, meta);
    button.onclick = () => window.screenGrab.choose(source.id);
    host.append(button);
  }
}
window.screenGrab.onSources(sources => {
  allSources = sources;
  const displays = document.getElementById("region-display");
  displays.replaceChildren();
  for (const source of sources.filter(source => source.kind === "screen")) {
    displays.add(new Option(source.name, source.id));
  }
  document.getElementById("region").disabled = displays.options.length === 0;
  renderSources();
});
