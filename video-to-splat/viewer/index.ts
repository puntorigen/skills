// Minimal standalone Aholo viewer for the video-to-splat skill.
//
// Loads the splat that preview.sh copied into public/ (described by scene.json),
// or a ?url= override, and renders it with first-person walkthrough controls
// tuned for touring a building:
//   - WASD / arrow keys walk (arrows left/right turn, A/D strafe, shift = run)
//   - mouse drag looks around, wheel moves forward/back, Q/E moves down/up
//   - minimap of the active floor (from analyze_scene.py) with a live camera
//     marker; click it to teleport, use the floor buttons / number keys to
//     jump between floors
// The Aholo SDK ships camera *controls* only inside its website harness, so we
// implement the controller here against the public PerspectiveCamera API
// (position / up / lookAt) - no undocumented APIs.
//
// Requires Chrome/Edge 134+ (WebGPU).

import {
  createViewer,
  setViewerConfig,
  PerspectiveCamera,
  BackgroundMode,
  Vector3,
  Color,
  SplatLoader,
  SplatUtils,
} from '@manycore/aholo-viewer';

type Vec = { x: number; y: number; z: number };

type PlanTransform = {
  origin_xy: number[];   // plan-space xy of pixel offset corner
  px_per_unit: number;
  offset_px: number[];
  size_px: number;
};

type FloorDesc = {
  index: number;
  level: number;                       // height along `up`, scene units
  plan?: string;                       // floorplan image URL
  plan_transform?: PlanTransform | null;
  camera?: { position: number[]; forward: number[] } | null;
};

type SceneDesc = {
  file: string;
  type: 'sog' | 'spz' | 'ply';
  // optional starting pose exported by preview.sh from the COLMAP model:
  // camera position + viewing direction of a real capture frame
  camera?: { position: number[]; forward: number[] };
  // optional navigation metadata from analyze_scene.py (via preview.sh)
  nav?: {
    up: number[];                      // gravity up vector (world)
    plan_x?: number[] | null;          // world axes of the floorplan image
    plan_y?: number[] | null;
    eye_height?: number | null;        // camera height above floor, scene units
    floors: FloorDesc[];
  };
};

const hud = document.getElementById('hud')!;
const errBox = document.getElementById('err')!;
const errMsg = document.getElementById('errmsg')!;

function showError(message: string) {
  hud.style.display = 'none';
  errMsg.textContent = message;
  errBox.style.display = 'grid';
  // eslint-disable-next-line no-console
  console.error('[video-to-splat viewer]', message);
}

function fileTypeFor(type: string): number {
  const T = (SplatLoader as any).SplatFileType;
  switch (type) {
    case 'ply': return T.PLY;
    case 'spz': return T.SPZ;
    case 'sog': return T.SOG;
    default: return T.SOG;
  }
}

async function resolveScene(): Promise<SceneDesc> {
  const params = new URLSearchParams(location.search);
  const url = params.get('url');
  if (url) {
    const ext = (url.split('.').pop() || 'sog').toLowerCase();
    const type = (['sog', 'spz', 'ply'].includes(ext) ? ext : 'sog') as SceneDesc['type'];
    return { file: url, type };
  }
  const resp = await fetch('/scene.json', { cache: 'no-store' });
  if (!resp.ok) {
    throw new Error(
      'No scene to show. Run preview.sh <splat> (it writes public/scene.json), ' +
      'or open the page with ?url=<splat url>.',
    );
  }
  return (await resp.json()) as SceneDesc;
}

// --- small vector helpers (plain objects; Aholo's Vector3 only where needed) --
const v = (x = 0, y = 0, z = 0): Vec => ({ x, y, z });
const add = (a: Vec, b: Vec): Vec => v(a.x + b.x, a.y + b.y, a.z + b.z);
const sub = (a: Vec, b: Vec): Vec => v(a.x - b.x, a.y - b.y, a.z - b.z);
const mul = (a: Vec, s: number): Vec => v(a.x * s, a.y * s, a.z * s);
const dot = (a: Vec, b: Vec): number => a.x * b.x + a.y * b.y + a.z * b.z;
const cross = (a: Vec, b: Vec): Vec =>
  v(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x);
const len = (a: Vec): number => Math.hypot(a.x, a.y, a.z);
const norm = (a: Vec): Vec => { const l = len(a) || 1; return mul(a, 1 / l); };
const fromArr = (a: number[]): Vec => v(a[0], a[1], a[2]);

// --- first-person walkthrough controller --------------------------------------
// Works in an arbitrary world frame: `up` is the scene's gravity up vector
// (COLMAP frames are roughly up = -Y but tilted; analyze_scene.py measures it).
function installFirstPerson(
  camera: any,
  el: HTMLElement,
  opts: { up: Vec; e1: Vec; e2: Vec; eye: number },
  onChange: () => void,
) {
  const { up, e1, e2 } = opts;
  // handedness of the (e1, e2, up) frame decides which way yaw "turns"
  const hand = Math.sign(dot(cross(e1, e2), up)) || 1;

  let pos = v(0, 0, 0);
  let yaw = 0;              // angle in the e1/e2 horizontal plane
  let pitch = 0;            // + looks up (toward `up`)
  let home: { pos: Vec; yaw: number; pitch: number } | null = null;

  const clampPitch = (p: number) => Math.max(-1.45, Math.min(1.45, p));
  const dir = (): Vec => {
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const h = add(mul(e1, Math.cos(yaw)), mul(e2, Math.sin(yaw)));
    return add(mul(h, cp), mul(up, sp));
  };
  const horizFwd = (): Vec => add(mul(e1, Math.cos(yaw)), mul(e2, Math.sin(yaw)));
  const rightVec = (): Vec => norm(cross(horizFwd(), up));

  function apply() {
    camera.position.set(pos.x, pos.y, pos.z);
    camera.up.set(up.x, up.y, up.z);
    const t = add(pos, dir());
    camera.lookAt(new Vector3(t.x, t.y, t.z));
    onChange();
  }

  function lookAlong(f: Vec) {
    const fu = dot(f, up);
    pitch = clampPitch(Math.asin(Math.max(-1, Math.min(1, fu))));
    yaw = Math.atan2(dot(f, e2), dot(f, e1));
  }

  // --- mouse: drag looks around (grab-the-world), wheel walks forward/back ---
  let dragging: 'look' | 'pan' | null = null;
  let lastX = 0, lastY = 0;
  el.addEventListener('mousedown', (e) => {
    dragging = (e.button === 2 || e.shiftKey) ? 'pan' : 'look';
    lastX = e.clientX; lastY = e.clientY;
    e.preventDefault();
  });
  window.addEventListener('mouseup', () => { dragging = null; });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    if (dragging === 'look') {
      yaw += hand * dx * 0.0035;
      pitch = clampPitch(pitch + dy * 0.0035);
    } else {
      // pan slides the camera laterally/vertically (screen-space)
      const s = opts.eye * 0.004;
      pos = add(pos, add(mul(rightVec(), -dx * s), mul(up, dy * s)));
    }
    apply();
  });
  el.addEventListener('wheel', (e) => {
    pos = add(pos, mul(dir(), -e.deltaY * opts.eye * 0.0022));
    apply();
    e.preventDefault();
  }, { passive: false });
  el.addEventListener('contextmenu', (e) => e.preventDefault());

  // --- keyboard: held-key walking, integrated in the render loop -------------
  const held = new Set<string>();
  window.addEventListener('keydown', (e) => {
    if ((e.target as HTMLElement)?.tagName === 'INPUT') return;
    held.add(e.code);
    if (e.code === 'KeyR' && home) {
      pos = { ...home.pos }; yaw = home.yaw; pitch = home.pitch; apply();
    }
    if (e.code.startsWith('Arrow') || e.code === 'Space') e.preventDefault();
  });
  window.addEventListener('keyup', (e) => { held.delete(e.code); });
  window.addEventListener('blur', () => held.clear());

  function tick(dt: number) {
    if (!held.size) return false;
    const run = held.has('ShiftLeft') || held.has('ShiftRight') ? 2.6 : 1;
    const walk = opts.eye * 1.4 * run * dt;   // ~1.4 eye heights (~2 m) per sec
    const turn = 1.7 * dt;                    // rad/s
    let moved = false;
    const step = (d: Vec, s: number) => { pos = add(pos, mul(d, s)); moved = true; };

    if (held.has('KeyW') || held.has('ArrowUp')) step(horizFwd(), walk);
    if (held.has('KeyS') || held.has('ArrowDown')) step(horizFwd(), -walk);
    if (held.has('KeyA')) step(rightVec(), -walk);
    if (held.has('KeyD')) step(rightVec(), walk);
    if (held.has('KeyE') || held.has('PageUp')) step(up, walk * 0.8);
    if (held.has('KeyQ') || held.has('PageDown')) step(up, -walk * 0.8);
    if (held.has('ArrowLeft')) { yaw += hand * turn; moved = true; }
    if (held.has('ArrowRight')) { yaw -= hand * turn; moved = true; }
    if (moved) apply();
    return moved;
  }

  return {
    apply,
    tick,
    getPos: () => pos,
    getForward: () => dir(),
    // place the camera at a position looking along a direction
    setPose(p: number[], f: number[], remember = false) {
      pos = fromArr(p);
      lookAlong(norm(fromArr(f)));
      if (remember || !home) home = { pos: { ...pos }, yaw, pitch };
      apply();
    },
    lookAtPoint(p: Vec, target: Vec, remember = false) {
      pos = { ...p };
      lookAlong(norm(sub(target, p)));
      if (remember || !home) home = { pos: { ...pos }, yaw, pitch };
      apply();
    },
    teleport(p: Vec, keepView = true) {
      pos = { ...p };
      if (!keepView) pitch = 0;
      apply();
    },
  };
}

// --- minimap + floor switcher --------------------------------------------------
function installMinimap(
  nav: NonNullable<SceneDesc['nav']>,
  frame: { up: Vec; e1: Vec; e2: Vec; eye: number },
  controls: ReturnType<typeof installFirstPerson>,
) {
  const wrap = document.getElementById('minimap')!;
  const img = document.getElementById('planimg') as HTMLImageElement;
  const cv = document.getElementById('plancv') as HTMLCanvasElement;
  const label = document.getElementById('planlabel')!;
  const btns = document.getElementById('floorbtns')!;
  const ctx = cv.getContext('2d')!;
  const floors = nav.floors;
  if (!floors.length) return null;

  wrap.style.display = 'flex';
  let active = -1;          // floor array index currently displayed
  let manualHold = 0;       // suppress auto floor-follow right after a jump

  const planXY = (p: Vec): [number, number] =>
    [dot(p, frame.e1), dot(p, frame.e2)];

  function toCanvas(f: FloorDesc, p: Vec): [number, number] | null {
    const t = f.plan_transform;
    if (!t) return null;
    const [x, y] = planXY(p);
    const u = ((x - t.origin_xy[0]) * t.px_per_unit + t.offset_px[0]) / t.size_px;
    const vv = ((y - t.origin_xy[1]) * t.px_per_unit + t.offset_px[1]) / t.size_px;
    return [u * cv.width, vv * cv.height];
  }

  function show(i: number) {
    if (i === active || !floors[i]?.plan) return;
    active = i;
    img.src = floors[i].plan!;
    label.textContent = floors.length > 1 ? `floor ${floors[i].index}` : 'floorplan';
    Array.from(btns.children).forEach((b, j) =>
      (b as HTMLElement).classList.toggle('active', j === i));
  }

  function jumpTo(i: number) {
    const f = floors[i];
    if (!f) return;
    manualHold = performance.now() + 1500;
    show(i);
    if (f.camera?.position && f.camera?.forward) {
      controls.setPose(f.camera.position, f.camera.forward);
    } else {
      // no stored pose: keep xy, move to that floor's camera walking height
      // (floor "level" is the camera-height cluster, already at eye level)
      const p = controls.getPos();
      const h = dot(p, frame.up);
      controls.teleport(add(p, mul(frame.up, f.level - h)));
    }
  }

  // floor buttons (top = highest floor, like an elevator panel)
  const order = floors.map((_, i) => i).sort((a, b) => floors[b].level - floors[a].level);
  for (const i of order) {
    const b = document.createElement('button');
    b.textContent = String(floors[i].index);
    b.title = `go to floor ${floors[i].index}`;
    b.addEventListener('click', () => jumpTo(i));
    btns.appendChild(b);
  }
  window.addEventListener('keydown', (e) => {
    const n = Number(e.key);
    if (n >= 1 && n <= floors.length) jumpTo(floors.findIndex(f => f.index === n));
  });

  // click-to-teleport on the plan
  img.parentElement!.addEventListener('click', (e) => {
    const f = floors[active];
    const t = f?.plan_transform;
    if (!t) return;
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const u = (e.clientX - r.left) / r.width * t.size_px;
    const vv = (e.clientY - r.top) / r.height * t.size_px;
    const x = (u - t.offset_px[0]) / t.px_per_unit + t.origin_xy[0];
    const y = (vv - t.offset_px[1]) / t.px_per_unit + t.origin_xy[1];
    // floor "level" = camera-height cluster, i.e. already the walking height
    controls.teleport(add(add(mul(frame.e1, x), mul(frame.e2, y)), mul(frame.up, f.level)));
    manualHold = performance.now() + 1500;
  });

  show(0);

  return {
    update() {
      const p = controls.getPos();
      // follow the walker between floors (unless a jump was just requested)
      if (performance.now() > manualHold && floors.length > 1) {
        const h = dot(p, frame.up);
        let best = 0, bd = Infinity;
        floors.forEach((f, i) => {
          const d = Math.abs(h - f.level);
          if (d < bd) { bd = d; best = i; }
        });
        show(best);
      }
      // camera marker + heading wedge
      ctx.clearRect(0, 0, cv.width, cv.height);
      const f = floors[active];
      const c = f ? toCanvas(f, p) : null;
      if (!c) return;
      const fwd = controls.getForward();
      const [fx, fy] = planXY(fwd);
      const a = Math.atan2(fy, fx);
      ctx.save();
      ctx.translate(c[0], c[1]);
      ctx.rotate(a);
      ctx.fillStyle = 'rgba(37, 99, 235, 0.35)';   // view cone
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.arc(0, 0, 16, -0.5, 0.5);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = '#3b82f6';                    // camera dot
      ctx.strokeStyle = '#fff';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(0, 0, 4.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    },
  };
}

// Robust scene bounds straight from the splat centers (SplatData.fillCenters).
// Median center + percentile radius so stray far-away floaters (common in
// splats trained from inward-looking tours) don't blow up the framing.
function tryComputeBounds(data: any): { center: Vec; radius: number } | null {
  try {
    const n = data?.counts;
    if (!n || typeof data.fillCenters !== 'function') return null;
    const centers = new Float32Array(n * 3);
    data.fillCenters(centers);

    const sample = Math.min(n, 50000);
    const step = Math.max(1, Math.floor(n / sample));
    const xs: number[] = [], ys: number[] = [], zs: number[] = [];
    for (let i = 0; i < n; i += step) {
      xs.push(centers[i * 3]); ys.push(centers[i * 3 + 1]); zs.push(centers[i * 3 + 2]);
    }
    const med = (a: number[]) => {
      const s = [...a].sort((p, q) => p - q);
      return s[Math.floor(s.length / 2)];
    };
    const c = v(med(xs), med(ys), med(zs));
    const d2 = xs.map((x, i) =>
      (x - c.x) ** 2 + (ys[i] - c.y) ** 2 + (zs[i] - c.z) ** 2).sort((p, q) => p - q);
    // 80th percentile distance = the bulk of the scene, floaters excluded
    const r = Math.sqrt(d2[Math.floor(d2.length * 0.8)]);
    if (!isFinite(r) || r <= 0) return null;
    return { center: c, radius: r };
  } catch { /* ignore - fall back to defaults */ }
  return null;
}

async function main() {
  if (!(navigator as any).gpu) {
    showError('WebGPU is not available in this browser. Use Chrome or Edge 134+.');
    return;
  }

  const container = document.getElementById('app')!;
  const scene = await resolveScene();
  hud.textContent = `loading ${scene.type.toUpperCase()} ...`;

  const viewer: any = createViewer('video-to-splat', container, {});

  // reuse the viewer's camera if present, else make one
  let camera: any = viewer.getCamera?.();
  if (!camera) {
    const aspect = container.clientWidth / Math.max(1, container.clientHeight);
    camera = new PerspectiveCamera(60, aspect, 0.05, 4000);
    viewer.setCamera?.(camera);
  }
  // Aholo's default camera is tuned for millimeter-scale scenes (near=100).
  // COLMAP/Brush scenes are a few unitless "meters" across - near=100 clips
  // the entire scene and renders pure black.
  camera.near = 0.05;
  camera.far = 4000;
  camera.updateProjectionMatrix?.();

  // world frame for walking: gravity up + horizontal plan axes. Without nav
  // data fall back to the COLMAP convention (up ~= -Y).
  const up = norm(scene.nav?.up ? fromArr(scene.nav.up) : v(0, -1, 0));
  let e1: Vec, e2: Vec;
  if (scene.nav?.plan_x && scene.nav?.plan_y) {
    e1 = norm(fromArr(scene.nav.plan_x));
    e2 = norm(fromArr(scene.nav.plan_y));
  } else {
    const tmp = Math.abs(up.x) < 0.9 ? v(1, 0, 0) : v(0, 1, 0);
    e1 = norm(cross(up, tmp));
    e2 = cross(up, e1);
  }

  let data: any;
  try {
    // parseSplatData accepts (type, url) - fetches + decodes for us
    data = await (SplatLoader as any).parseSplatData(fileTypeFor(scene.type), scene.file);
  } catch (e: any) {
    showError(`Failed to load splat "${scene.file}": ${e?.message || e}`);
    return;
  }

  const splat = await (SplatUtils as any).createSplat(data);
  viewer.getScene().add(splat);
  // eslint-disable-next-line no-console
  console.log('[video-to-splat viewer] splats:', data?.counts);

  setViewerConfig(viewer, {
    pipeline: {
      Background: {
        background: { active: BackgroundMode.BasicBackground, basic: { color: new Color(0, 0, 0) } },
        ground: { enabled: false },
      },
      Splatting: { enabled: true },
      TAA: { enabled: false },
    },
  });

  const bounds = tryComputeBounds(data);
  // walking scale: eye height from the analysis when present, else a rough
  // guess from the scene size (indoor tours are a handful of units across)
  const eye = scene.nav?.eye_height || (bounds ? bounds.radius * 0.25 : 1.0);

  const render = () => viewer.render();
  const controls = installFirstPerson(camera, container, { up, e1, e2, eye }, render);

  if (scene.camera?.position && scene.camera?.forward) {
    // start exactly where the capture camera stood, looking the same way
    controls.setPose(scene.camera.position, scene.camera.forward, true);
  } else if (bounds) {
    const p = add(bounds.center, mul(add(e1, mul(up, 0.4)), bounds.radius * 1.6));
    controls.lookAtPoint(p, bounds.center, true);
  } else {
    controls.setPose([0, 0, -3], [0, 0, 1], true);
  }

  const minimap = scene.nav ? installMinimap(scene.nav, { up, e1, e2, eye }, controls) : null;

  // debug hooks: place the camera / inspect state from the devtools console
  (window as any).__setPose = (p: number[], f: number[]) => controls.setPose(p, f);
  (window as any).__viewer = viewer;
  (window as any).__camera = camera;
  (window as any).__splat = splat;
  (window as any).__controls = controls;

  // render loop: integrate held keys + refresh the minimap marker
  let last = performance.now();
  const loop = (now: number) => {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    controls.tick(dt);
    minimap?.update();
    viewer.render();
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);

  window.addEventListener('resize', () => {
    if (camera && typeof camera.aspect === 'number') {
      camera.aspect = container.clientWidth / Math.max(1, container.clientHeight);
      camera.updateProjectionMatrix?.();
    }
    viewer.resize?.();
    viewer.render();
  });

  hud.textContent = scene.nav
    ? 'walk: WASD/arrows (shift: run) · look: drag · wheel: forward · Q/E: down/up · R: reset · minimap: click to teleport, numbers switch floors'
    : 'walk: WASD/arrows (shift: run) · look: drag · wheel: forward · Q/E: down/up · R: reset';
  setTimeout(() => { hud.style.opacity = '0.35'; }, 6000);
  hud.style.transition = 'opacity .6s';
}

main().catch((e) => showError(e?.message || String(e)));
