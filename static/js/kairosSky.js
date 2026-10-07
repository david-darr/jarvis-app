// A quiet Home scene: clouds drift slowly, and a small gold point carries
// the working/attention/offline state. Motion stops when paused or reduced.
export function mount(container) {
  const scene = document.createElement("div");
  scene.className = "kairos-sky-scene";
  scene.setAttribute("aria-hidden", "true");
  const sky = document.createElement("div");
  sky.className = "kairos-sky-image";
  const point = document.createElement("div");
  point.className = "kairos-sky-point";
  scene.append(sky, point);
  container.append(scene);

  const dispose = () => scene.remove();
  dispose.setState = (state) => { scene.dataset.state = state; };
  dispose.setPaused = (paused) => { scene.classList.toggle("is-paused", !!paused); };
  return dispose;
}
