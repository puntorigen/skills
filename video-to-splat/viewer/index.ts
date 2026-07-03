// Minimal standalone Aholo viewer for the video-to-splat skill.
//
// Loads the splat that preview.sh copied into public/ (described by scene.json),
// or a ?url= override, and renders it with simple mouse orbit / zoom / pan.
// The Aholo SDK ships camera *controls* only inside its website harness, so we
// implement a small orbit controller here against the public PerspectiveCamera
// API (position / up / lookAt) - no undocumented APIs.
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

type SceneDesc = { file: string; type: 'sog' | 'spz' | 'ply' };

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

// --- tiny orbit controller (up = -Y, matching Aholo/OpenCV splat convention) --
function installControls(
  camera: any,
  el: HTMLElement,
  onChange: () => void,
) {
  const target = new Vector3(0, 0, 0);
  let yaw = Math.PI;      // look toward -Z initially
  let pitch = 0.15;
  let radius = 3;
  let dragging: 'orbit' | 'pan' | null = null;
  let lastX = 0, lastY = 0;

  const clampPitch = (p: number) => Math.max(-1.5, Math.min(1.5, p));

  function apply() {
    // direction from target to camera (up is -Y, so invert the y term)
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const dir = {
      x: cp * Math.sin(yaw),
      y: -sp,
      z: cp * Math.cos(yaw),
    };
    camera.position.set(
      target.x + dir.x * radius,
      target.y + dir.y * radius,
      target.z + dir.z * radius,
    );
    camera.up.set(0, -1, 0);
    camera.lookAt(new Vector3(target.x, target.y, target.z));
    onChange();
  }

  function basis() {
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    // forward = target - position (unit)
    const fwd = { x: -cp * Math.sin(yaw), y: sp, z: -cp * Math.cos(yaw) };
    const up = { x: 0, y: -1, z: 0 };
    // right = normalize(cross(fwd, up))
    let rx = fwd.y * up.z - fwd.z * up.y;
    let ry = fwd.z * up.x - fwd.x * up.z;
    let rz = fwd.x * up.y - fwd.y * up.x;
    const rl = Math.hypot(rx, ry, rz) || 1;
    rx /= rl; ry /= rl; rz /= rl;
    // camUp = cross(right, fwd)
    const ux = ry * fwd.z - rz * fwd.y;
    const uy = rz * fwd.x - rx * fwd.z;
    const uz = rx * fwd.y - ry * fwd.x;
    return { right: { x: rx, y: ry, z: rz }, up: { x: ux, y: uy, z: uz } };
  }

  el.addEventListener('mousedown', (e) => {
    dragging = (e.button === 2 || e.shiftKey) ? 'pan' : 'orbit';
    lastX = e.clientX; lastY = e.clientY;
    e.preventDefault();
  });
  window.addEventListener('mouseup', () => { dragging = null; });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    if (dragging === 'orbit') {
      yaw -= dx * 0.005;
      pitch = clampPitch(pitch + dy * 0.005);
    } else {
      const { right, up } = basis();
      const s = radius * 0.0015;
      target.x += (-dx * right.x + dy * up.x) * s;
      target.y += (-dx * right.y + dy * up.y) * s;
      target.z += (-dx * right.z + dy * up.z) * s;
    }
    apply();
  });
  el.addEventListener('wheel', (e) => {
    radius *= Math.exp(e.deltaY * 0.001);
    radius = Math.max(0.05, Math.min(500, radius));
    apply();
    e.preventDefault();
  }, { passive: false });
  el.addEventListener('contextmenu', (e) => e.preventDefault());
  window.addEventListener('keydown', (e) => {
    if (e.key === 'r' || e.key === 'R') {
      target.set(0, 0, 0); yaw = Math.PI; pitch = 0.15; radius = 3; apply();
    }
  });

  // auto-frame from the splat bounds if available (best-effort)
  return {
    frameTo(center?: { x: number; y: number; z: number }, r?: number) {
      if (center) target.set(center.x, center.y, center.z);
      if (r && isFinite(r) && r > 0) radius = r * 2.2;
      apply();
    },
    apply,
  };
}

function tryComputeBounds(splat: any): { center: any; radius: number } | null {
  try {
    const box = (SplatUtils as any).computeDenseBox?.(splat);
    if (box && box.min && box.max) {
      const c = {
        x: (box.min.x + box.max.x) / 2,
        y: (box.min.y + box.max.y) / 2,
        z: (box.min.z + box.max.z) / 2,
      };
      const r = Math.hypot(
        box.max.x - box.min.x,
        box.max.y - box.min.y,
        box.max.z - box.min.z,
      ) / 2;
      return { center: c, radius: r };
    }
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

  const render = () => viewer.render();
  const controls = installControls(camera, container, render);

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

  const bounds = tryComputeBounds(splat);
  if (bounds) controls.frameTo(bounds.center, bounds.radius);
  else controls.apply();

  // continuous render loop (simple + robust for a preview)
  const loop = () => { viewer.render(); requestAnimationFrame(loop); };
  requestAnimationFrame(loop);

  window.addEventListener('resize', () => {
    if (camera && typeof camera.aspect === 'number') {
      camera.aspect = container.clientWidth / Math.max(1, container.clientHeight);
      camera.updateProjectionMatrix?.();
    }
    viewer.resize?.();
    viewer.render();
  });

  hud.textContent = 'drag: orbit  ·  shift/right-drag: pan  ·  wheel: zoom  ·  R: reset';
  setTimeout(() => { hud.style.opacity = '0.35'; }, 4000);
  hud.style.transition = 'opacity .6s';
}

main().catch((e) => showError(e?.message || String(e)));
