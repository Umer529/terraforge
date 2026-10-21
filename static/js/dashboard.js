// Dashboard: scan list status, 3D viewer, map, measurement tools, scan details.
import { CloudViewer, api, fmt } from "./viewer.js";

const scanId = window.SCAN_ID;
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// ------------------------------------------------------------------ map
const map = L.map("map", { zoomControl: true, attributionControl: true });
// Satellite basemap (OSM's volunteer tile servers block localhost apps).
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 22, maxNativeZoom: 19, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" }).addTo(map);
map.setView([31.4815, 74.303], 16);

async function showSites() {
  const { sites } = await api("/api/sites");
  const pts = sites.filter((s) => s.centre[0] != null).map((s) => L.marker(s.centre).addTo(map).bindPopup(esc(s.name)).getLatLng());
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.5), { maxZoom: 17 });
}

function showScanOnMap(scan) {
  const preview = scan.outputs.preview;
  if (preview && preview.meta.bounds_latlon) {
    const b = preview.meta.bounds_latlon;
    L.imageOverlay(preview.url, b, { opacity: 0.85 }).addTo(map).bindTooltip("Elevation map (GeoTIFF preview)");
    map.fitBounds(b, { padding: [20, 20], maxZoom: 21 });
  }
  if (scan.track && scan.track.length) {
    L.polyline(scan.track, { color: "#d9822b", weight: 2, dashArray: "4 4" }).addTo(map).bindTooltip("GPS track of the walk");
    L.circleMarker(scan.track[0], { radius: 5, color: "#fff", fillColor: "#20302a", fillOpacity: 1 }).addTo(map).bindTooltip("Capture start");
    if (!preview?.meta.bounds_latlon) map.fitBounds(L.latLngBounds(scan.track).pad(0.2));
  }
  if (!scan.origin) {
    const note = L.control({ position: "bottomleft" });
    note.onAdd = () => Object.assign(document.createElement("div"), { className: "chip", textContent: "Scan has no GPS: not georeferenced" });
    note.addTo(map);
  }
}

// ------------------------------------------------------------------ details
function renderDetails(scan) {
  const pc = scan.outputs.pointcloud, dem = scan.outputs.dem, geo = pc?.meta.georef || {};
  const rows = [
    ["Site", scan.site], ["Captured", new Date(scan.captured_at).toLocaleString()], ["By", scan.captured_by],
    ["Device", scan.device_model || "—"], ["Status", `<span class="badge ${scan.status}">${scan.status}</span>`],
    ["Walk length", scan.path_length_m ? `${fmt(scan.path_length_m, 1)} m` : "—"],
    ["GPS fixes", scan.gps_fixes ?? "—"],
  ];
  if (pc) rows.push(["Points", Number(pc.meta.count).toLocaleString()], ["Frame", esc(pc.crs)]);
  if (dem) rows.push(["Grid", `${fmt(dem.meta.cell * 100, 0)} cm cells, ${fmt(dem.meta.covered_m2, 0)} m² covered`]);
  if (geo.method && geo.method !== "none") {
    const acc = geo.rms_m != null ? `±${geo.rms_m} m (GPS), heading ${geo.rotation_deg}°` : "manual location, accuracy unknown";
    rows.push(["Georeference", `${esc(geo.method)}<br><span class="muted">${acc}</span>`]);
  }
  if (scan.outputs.geotiff) rows.push(["Map CRS", scan.outputs.geotiff.crs]);
  const dl = [];
  if (scan.outputs.geotiff) dl.push(`<a href="${scan.outputs.geotiff.url}">GeoTIFF</a>`);
  if (pc) dl.push(`<a href="/api/scans/${scan.id}/files/ply">Point cloud (PLY)</a>`);
  dl.push(`<a href="/api/scans/${scan.id}/files/package">Original package</a>`);
  rows.push(["Download", dl.join(" · ")]);
  const job = scan.job;
  $("#details").innerHTML = `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
    ${job ? `<details ${scan.status !== "ready" ? "open" : ""}><summary>Processing log (job ${esc(job.state)})</summary><pre class="log" id="log">${esc(job.log)}</pre></details>` : ""}`;
  const log = $("#log");
  if (log) log.scrollTop = log.scrollHeight;
}

// ------------------------------------------------------------------ measurement tools
let viewer = null, tool = null, picked = [];
const UNIT = { m: "m", m2: "m²", m3: "m³" };

function setTool(name) {
  tool = tool === name ? null : name;
  picked = [];
  viewer?.clearShape();
  document.querySelectorAll("[data-tool]").forEach((b) => b.classList.toggle("on", b.dataset.tool === tool));
  $("#finish-wrap").hidden = !(tool === "area" || tool === "volume");
  $("#reference").hidden = tool !== "volume";
  const help = { distance: "Click two points.", area: "Click the corners of the area, then Finish.", volume: "Click around the base of the pile or pit, then Finish." };
  $("#result").innerHTML = `<span class="sub">${tool ? help[tool] : "Pick a tool, then click points on the model."}</span>`;
}

function showResult(m) {
  const d = m.details || {};
  let main = `<span class="big">${fmt(m.value)} ${UNIT[m.unit]}</span>`, extra = [];
  if (m.kind === "distance") {
    main += ` <span class="sub">± ${fmt(d.error_m)} m</span>`;
    extra = [`horizontal ${fmt(d.horizontal_m)} m`, `vertical ${fmt(d.vertical_m)} m`];
  } else if (m.kind === "area") {
    main += ` <span class="sub">± ${fmt(d.error_m2)} m²</span>`;
    extra = [`${m.reference_surface}`, `perimeter ${fmt(d.perimeter_m)} m`];
  } else {
    main += ` <span class="sub">± ${fmt(d.error_m3)} m³ · ${esc(d.direction)}</span>`;
    extra = [`Reference surface: ${esc(m.reference_surface)}`, `above ${fmt(d.above_m3)} m³ / below ${fmt(d.below_m3)} m³`,
      `plan area ${fmt(d.plan_area_m2)} m²`, `coverage ${fmt(d.coverage * 100, 0)}%`];
  }
  $("#result").innerHTML = `<div>${main}</div>${extra.map((e) => `<span class="sub">${e}</span>`).join("")}`;
}

async function submit() {
  try {
    const m = await api(`/api/scans/${scanId}/measurements`, {
      method: "POST",
      body: JSON.stringify({ kind: tool, points: picked, reference: $("#reference").value }),
    });
    showResult(m);
    loadSaved();
  } catch (e) {
    $("#result").innerHTML = `<span class="error">${esc(e.message)}</span>`;
  }
  picked = [];
}

async function loadSaved() {
  const { measurements } = await api(`/api/scans/${scanId}/measurements`);
  $("#saved").innerHTML = measurements.length ? `<tr><th>Saved</th><th>Value</th><th>By</th><th>When</th><th></th></tr>` +
    measurements.map((m) => `<tr class="clickable" data-id="${m.id}"><td>${m.kind}</td><td>${fmt(m.value)} ${UNIT[m.unit]}</td>
      <td>${esc(m.created_by)}</td><td>${new Date(m.created_at).toLocaleTimeString()}</td>
      <td><button class="x" data-del="${m.id}" title="Delete">✕</button></td></tr>`).join("") : "";
  $("#saved").querySelectorAll("tr.clickable").forEach((tr) => tr.addEventListener("click", (e) => {
    const m = measurements.find((x) => x.id == tr.dataset.id);
    if (e.target.dataset.del) {
      api(`/api/measurements/${m.id}`, { method: "DELETE" }).then(loadSaved);
      return;
    }
    viewer.drawShape(m.points, m.kind !== "distance", 0x7fd3ff);
    showResult(m);
  }));
}

function wireTools() {
  document.querySelectorAll("[data-tool]").forEach((b) => b.addEventListener("click", () => setTool(b.dataset.tool)));
  $("#clear").addEventListener("click", () => setTool(null));
  $("#finish").addEventListener("click", () => {
    if (picked.length < 3) return ($("#result").innerHTML = `<span class="error">Click at least 3 points first.</span>`);
    viewer.drawShape(picked, true);
    submit();
  });
  document.addEventListener("keydown", (e) => e.key === "Escape" && setTool(null));
  viewer.onPick = (p) => {
    if (!tool) {
      viewer.drawShape([p], false);
      $("#result").innerHTML = `<span class="sub">Point E ${fmt(p[0])} m, N ${fmt(p[1])} m, elevation ${fmt(p[2])} m</span>`;
      return;
    }
    picked.push(p);
    viewer.drawShape(picked, tool !== "distance");
    if (tool === "distance" && picked.length === 2) submit();
    else if (tool !== "distance") $("#result").innerHTML = `<span class="sub">${picked.length} point(s) — click Finish when done.</span>`;
  };
}

// ------------------------------------------------------------------ live status
async function pollList() {
  const pending = [...document.querySelectorAll(".scan-item")].filter((a) => !/ready|failed/.test(a.querySelector(".badge").textContent));
  if (!pending.length) return;
  const { scans } = await api("/api/scans");
  for (const s of scans) {
    const badge = document.querySelector(`.scan-item[data-scan="${s.id}"] .badge`);
    if (badge && badge.textContent !== s.status) {
      badge.textContent = s.status;
      badge.className = `badge ${s.status}`;
    }
  }
  setTimeout(pollList, 2000);
}

async function main() {
  pollList();
  if (!scanId) {
    showSites();
    document.querySelectorAll("[data-tool], #clear").forEach((b) => (b.disabled = true));
    return;
  }
  let scan = await api(`/api/scans/${scanId}`);
  renderDetails(scan);
  showScanOnMap(scan);
  if (scan.status !== "ready") {
    $("#cloud-info").textContent = `Scan is ${scan.status}…`;
    document.querySelectorAll("[data-tool], #clear").forEach((b) => (b.disabled = true));
    while (!["ready", "failed"].includes(scan.status)) {
      await new Promise((r) => setTimeout(r, 1200));
      scan = await api(`/api/scans/${scanId}`);
      renderDetails(scan);
      $("#cloud-info").textContent = `Scan is ${scan.status}…`;
    }
    if (scan.status === "ready") location.reload();
    return;
  }
  viewer = new CloudViewer($("#viewer"));
  const { points } = await viewer.load(scan.outputs.pointcloud.url);
  $("#cloud-info").textContent = `${points.toLocaleString()} points · ${scan.title}`;
  if (scan.outputs.model) {
    await viewer.loadModel(scan.outputs.model.url, scan.outputs.model.meta.matrix);
    viewer.showLayer("model");
    $("#layer").hidden = false;
    $("#layer").addEventListener("change", (e) => viewer.showLayer(e.target.value));
  }
  $("#point-size").addEventListener("input", (e) => viewer.setPointSize(+e.target.value));
  $("#color-mode").addEventListener("change", (e) => viewer.colorBy(e.target.value));
  wireTools();
  loadSaved();
}

main().catch((e) => {
  console.error(e);
  const info = $("#cloud-info");
  if (info) info.textContent = e.message;
});
