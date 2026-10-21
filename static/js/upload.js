// Browser client for the resumable chunked upload protocol (same API as the iOS app).
import { api } from "./viewer.js";

const $ = (s) => document.querySelector(s);
const form = $("#form");
let file = null, upload = null, paused = false, running = false;

async function sha256(buf) {
  if (!crypto.subtle) return ""; // only on http://localhost or https
  const h = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function drawChunks(received, sending = -1) {
  const total = upload.total_chunks, set = new Set(received);
  if (total <= 400) {
    $("#chunks").innerHTML = Array.from({ length: total }, (_, i) =>
      `<span class="${set.has(i) ? "done" : i === sending ? "sending" : ""}" title="chunk ${i}"></span>`).join("");
  }
  const pct = Math.round((100 * set.size) / total);
  $("#pct").textContent = `${pct}%`;
  $("#bar").style.width = `${pct}%`;
  $("#ptext").textContent = `${set.size} / ${total} chunks · ${(file.size / 1e6).toFixed(1)} MB`;
}

async function sendChunks() {
  if (running) return;
  running = true;
  paused = false;
  $("#pause").hidden = false;
  $("#resume").hidden = true;
  // Ask the server what it already has: this is the resume point.
  const info = await api(`/api/uploads/${upload.upload_id}`);
  const received = new Set(info.received);
  if (received.size) $("#status").textContent = `Resuming: server already has ${received.size} chunk(s).`;
  drawChunks([...received]);
  for (let i = 0; i < upload.total_chunks; i++) {
    if (received.has(i)) continue;
    if (paused) {
      $("#status").textContent = `Connection lost (simulated) after ${received.size} chunks. Press Resume.`;
      running = false;
      return;
    }
    const blob = file.slice(i * upload.chunk_size, Math.min(file.size, (i + 1) * upload.chunk_size));
    const buf = await blob.arrayBuffer();
    drawChunks([...received], i);
    for (let attempt = 0; ; attempt++) {
      const res = await fetch(`/api/uploads/${upload.upload_id}/chunks/${i}`, {
        method: "PUT", body: buf,
        headers: { "X-CSRFToken": window.CSRF, "X-Chunk-SHA256": await sha256(buf), "Content-Type": "application/octet-stream" },
      }).catch(() => null);
      if (res && res.ok) break;
      if (attempt >= 4) throw new Error(`chunk ${i} failed`);
      $("#status").textContent = `Chunk ${i} failed, retrying…`;
      await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
    }
    received.add(i);
    drawChunks([...received]);
  }
  running = false;
  $("#pause").hidden = $("#resume").hidden = true;
  $("#status").textContent = "All chunks received. Assembling and verifying package…";
  const done = await api(`/api/uploads/${upload.upload_id}/complete`, { method: "POST" });
  followProcessing(done.scan_id);
}

async function followProcessing(scanId) {
  $("#log").hidden = false;
  for (;;) {
    const scan = await api(`/api/scans/${scanId}`);
    $("#status").innerHTML = `Scan #${scanId}: <span class="badge ${scan.status}">${scan.status}</span>` +
      (scan.status === "queued" ? " — waiting for the worker" : "");
    $("#log").textContent = scan.job?.log || "";
    $("#log").scrollTop = $("#log").scrollHeight;
    if (scan.status === "ready" || scan.status === "failed") {
      $("#open").hidden = false;
      $("#open-link").href = `/scans/${scanId}/`;
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(form);
  file = fd.get("file");
  if (!file || !file.size) return;
  $("#start").disabled = true;
  $("#idle").hidden = true;
  $("#progress").hidden = false;
  try {
    upload = await api("/api/uploads", {
      method: "POST",
      body: JSON.stringify({
        site_id: +fd.get("site_id"), filename: file.name, size: file.size, chunk_size: 1024 * 1024,
        title: fd.get("title"), notes: fd.get("notes"), latitude: fd.get("latitude"), longitude: fd.get("longitude"),
        up_axis: fd.get("up_axis"), device_model: "Browser upload",
        captured_at: new Date(file.lastModified).toISOString(),
      }),
    });
    $("#status").textContent = `Upload session ${upload.upload_id.slice(0, 8)} created for scan #${upload.scan_id}.`;
    await sendChunks();
  } catch (err) {
    $("#status").innerHTML = `<span class="error">${err.message}</span>`;
    $("#start").disabled = false;
  }
});

$("#pause").addEventListener("click", () => {
  if (!running) return;
  paused = true;
  $("#pause").hidden = true;
  $("#resume").hidden = false;
});
$("#resume").addEventListener("click", () => sendChunks().catch((err) => ($("#status").textContent = err.message)));

$("#use-site").addEventListener("click", () => {
  const opt = form.site_id.selectedOptions[0];
  form.latitude.value = opt.dataset.lat;
  form.longitude.value = opt.dataset.lon;
});
$("#use-gps").addEventListener("click", () => navigator.geolocation?.getCurrentPosition((p) => {
  form.latitude.value = p.coords.latitude.toFixed(6);
  form.longitude.value = p.coords.longitude.toFixed(6);
}));
