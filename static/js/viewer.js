// Three.js point cloud viewer with point picking (local ENU metres, z up).
import * as THREE from "three";
import { OrbitControls } from "three/OrbitControls";

const RAMP = [[47, 79, 140], [52, 146, 168], [118, 186, 110], [222, 205, 96], [205, 120, 58], [140, 70, 50]];

function rampColor(t) {
  t = Math.min(Math.max(t, 0), 1) * (RAMP.length - 1);
  const i = Math.min(Math.floor(t), RAMP.length - 2), f = t - i;
  return RAMP[i].map((c, k) => c * (1 - f) + RAMP[i + 1][k] * f);
}

export class CloudViewer {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    el.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x1b2420);
    this.camera = new THREE.PerspectiveCamera(55, 1, 0.05, 5000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.addEventListener("change", () => this.render());
    this.clouds = [];
    this.overlay = new THREE.Group();
    this.scene.add(this.overlay);
    this.pointSize = 2;
    this.extent = 10;
    this.onPick = null;

    let down = null;
    const canvas = this.renderer.domElement;
    canvas.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
    canvas.addEventListener("pointerup", (e) => {
      if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 4 && e.button === 0) this.pick(e);
      down = null;
    });
    new ResizeObserver(() => this.resize()).observe(el);
    this.resize();
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  render() {
    if (this.pending) return;
    this.pending = requestAnimationFrame(() => {
      this.pending = null;
      this.renderer.render(this.scene, this.camera);
    });
  }

  // url -> binary file: N*3 float32 xyz followed by N*3 uint8 rgb
  async load(url, { tint = null, matrix = null } = {}) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`could not load point cloud (${res.status})`);
    const buf = await res.arrayBuffer();
    const n = buf.byteLength / 15;
    const pos = new Float32Array(buf, 0, n * 3);
    const rgb = new Uint8Array(buf, n * 12, n * 3);
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geom.setAttribute("color", new THREE.BufferAttribute(rgb, 3, true));
    geom.userData.rgb = rgb.slice(); // the attribute shares rgb's buffer, so keep a copy
    const mat = new THREE.PointsMaterial({ size: this.pointSize, sizeAttenuation: false, vertexColors: !tint });
    if (tint) mat.color = new THREE.Color(tint);
    const pts = new THREE.Points(geom, mat);
    if (matrix) {
      const m = new THREE.Matrix4();
      m.set(...matrix.flat());
      pts.applyMatrix4(m);
    }
    this.scene.add(pts);
    this.clouds.push(pts);
    if (this.clouds.length === 1) this.fit(pts);
    this.render();
    return { points: n, object: pts };
  }

  fit(obj) {
    obj.geometry.computeBoundingBox();
    const box = obj.geometry.boundingBox.clone().applyMatrix4(obj.matrixWorld);
    const c = box.getCenter(new THREE.Vector3()), s = box.getSize(new THREE.Vector3());
    this.extent = Math.max(s.x, s.y, 1);
    this.camera.position.set(c.x - this.extent * 0.15, c.y - this.extent * 0.85, c.z + this.extent * 0.6);
    this.controls.target.copy(c);
    this.camera.near = this.extent / 2000;
    this.camera.far = this.extent * 20;
    this.camera.updateProjectionMatrix();
    this.controls.update();
  }

  setPointSize(px) {
    this.pointSize = px;
    this.clouds.forEach((c) => (c.material.size = px));
    this.render();
  }

  setVisible(i, on) {
    if (this.clouds[i]) this.clouds[i].visible = on;
    this.render();
  }

  // "rgb" (scanner colour) or "elevation"
  colorBy(mode) {
    for (const c of this.clouds) {
      if (!c.material.vertexColors) continue;
      const attr = c.geometry.getAttribute("color");
      if (mode === "rgb") {
        attr.array.set(c.geometry.userData.rgb);
      } else {
        const p = c.geometry.getAttribute("position").array;
        let lo = Infinity, hi = -Infinity;
        for (let i = 2; i < p.length; i += 3) { lo = Math.min(lo, p[i]); hi = Math.max(hi, p[i]); }
        const out = new Uint8Array(attr.array.length);
        for (let i = 0, j = 0; i < p.length; i += 3, j += 3) {
          const col = rampColor((p[i + 2] - lo) / (hi - lo || 1));
          out[j] = col[0]; out[j + 1] = col[1]; out[j + 2] = col[2];
        }
        attr.array.set(out);
      }
      attr.needsUpdate = true;
    }
    this.render();
  }

  pick(event) {
    if (!this.onPick || !this.clouds.length) return;
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, this.camera);
    const dist = this.camera.position.distanceTo(this.controls.target);
    ray.params.Points.threshold = dist * 0.006;
    const hits = ray.intersectObjects(this.clouds.filter((c) => c.visible), false);
    if (!hits.length) return;
    // closest to the ray among the nearest few hits, then snap to the actual LiDAR point
    const near = hits.slice(0, 30).sort((a, b) => a.distanceToRay - b.distanceToRay)[0];
    const p = near.object.geometry.getAttribute("position");
    const v = new THREE.Vector3().fromBufferAttribute(p, near.index).applyMatrix4(near.object.matrixWorld);
    this.onPick([v.x, v.y, v.z]);
  }

  // Draw picked points and the connecting polyline (closed for polygons).
  drawShape(points, closed, color = 0xffb347) {
    this.clearShape();
    const r = this.extent * 0.004;
    const sphere = new THREE.SphereGeometry(r, 12, 8);
    const mat = new THREE.MeshBasicMaterial({ color, depthTest: false });
    for (const p of points) {
      const m = new THREE.Mesh(sphere, mat);
      m.position.set(...p);
      m.renderOrder = 10;
      this.overlay.add(m);
    }
    if (points.length > 1) {
      const pts = points.map((p) => new THREE.Vector3(...p));
      if (closed && points.length > 2) pts.push(pts[0]);
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
        new THREE.LineBasicMaterial({ color, depthTest: false }));
      line.renderOrder = 10;
      this.overlay.add(line);
    }
    this.render();
  }

  clearShape() {
    this.overlay.clear();
    this.render();
  }
}

export async function api(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", "X-CSRFToken": window.CSRF, ...(options.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

export const fmt = (v, d = 2) => Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
