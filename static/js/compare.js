// Compare page: align scan B onto scan A, show cut/fill and both clouds overlaid.
import { CloudViewer, api, fmt } from "./viewer.js";

const $ = (s) => document.querySelector(s);
const form = $("#pick");
let viewer = null;

// default: "before" = oldest, "after" = newest (options are listed newest first)
if (form.a.options.length > 1) form.a.selectedIndex = form.a.options.length - 1;

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const a = form.a.value, b = form.b.value;
  if (a === b) return ($("#msg").textContent = "Choose two different scans.");
  if (form.a.selectedOptions[0].dataset.site !== form.b.selectedOptions[0].dataset.site)
    return ($("#msg").textContent = "Scans must belong to the same site.");
  $("#go").disabled = true;
  $("#msg").textContent = "Aligning scans (ICP) and computing elevation change…";
  try {
    const r = await api(`/api/compare?a=${a}&b=${b}`);
    show(r);
    $("#msg").textContent = "";
  } catch (err) {
    $("#msg").innerHTML = `<span class="error">${err.message}</span>`;
  }
  $("#go").disabled = false;
});

async function show(r) {
  $("#out").hidden = false;
  const card = (cls, k, v) => `<div class="card ${cls}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $("#cards").innerHTML =
    card("cut", "Cut (material removed)", `${fmt(r.cut_m3)} m³`) +
    card("fill", "Fill (material added)", `${fmt(r.fill_m3)} m³`) +
    card("", "Net change", `${r.net_m3 > 0 ? "+" : ""}${fmt(r.net_m3)} m³`) +
    card("", "Overlap compared", `${fmt(r.overlap_m2, 0)} m²`);
  $("#diff").src = `${r.image}?t=${Date.now()}`;
  $("#lo").textContent = `−${fmt(r.grid.limit_m)} m`;
  $("#hi").textContent = `+${fmt(r.grid.limit_m)} m`;
  $("#align").textContent = `Alignment: ${r.seeded_by_gps ? "GPS seed + " : ""}ICP, residual ${fmt(r.alignment_rms_m * 100, 1)} cm, ` +
    `rotation ${r.yaw_deg}°, shift (${r.shift_m.map((v) => fmt(v, 1)).join(", ")}) m. ` +
    `Changes smaller than ${fmt(r.noise_threshold_m * 100, 0)} cm are treated as noise.`;

  if (viewer) {
    viewer.clouds.forEach((c) => viewer.scene.remove(c));
    viewer.clouds = [];
  } else {
    viewer = new CloudViewer($("#viewer"));
    $("#show-a").addEventListener("change", (e) => viewer.setVisible(0, e.target.checked));
    $("#show-b").addEventListener("change", (e) => viewer.setVisible(1, e.target.checked));
  }
  await viewer.load(`/api/scans/${r.a}/files/pointcloud`, { tint: 0x3b7bbf });
  await viewer.load(`/api/scans/${r.b}/files/pointcloud`, { tint: 0xe0782a, matrix: r.matrix_b_to_a });
}
