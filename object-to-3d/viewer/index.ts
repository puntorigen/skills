// Three.js + GaussianSplats3D orbit viewer for the object-to-3d skill.
//
// Loads the splat from public/scene.json (written by preview.sh) or ?url=,
// renders it on a studio pedestal with camera-synced lighting.
//
// Controls:
//   drag / arrows (WASD)  orbit around the object
//   wheel / +−            zoom
//   shift-drag / right-drag  pan
//   R                     reset camera

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import * as GaussianSplats3D from '@mkkellogg/gaussian-splats-3d';

// 'glb' is the repaired, watertight print mesh (Print Preview); the others are
// raw Gaussian-splat scan data (Splat Preview).
type SceneDesc = {
  file: string;
  type: 'ply' | 'splat' | 'spz' | 'ksplat' | 'glb';
  up?: number[];
};

const hud = document.getElementById('hud')!;
const errBox = document.getElementById('err')!;
const errMsg = document.getElementById('errmsg')!;
const container = document.getElementById('app')!;

function showError(message: string) {
  hud.style.display = 'none';
  errMsg.textContent = message;
  errBox.style.display = 'grid';
  console.error('[object-to-3d viewer]', message);
}

async function resolveScene(): Promise<SceneDesc> {
  const params = new URLSearchParams(location.search);
  const printMode = params.get('mode') === 'print';
  const url = params.get('url');
  if (url) {
    const ext = (url.split('.').pop() || 'ply').toLowerCase();
    let type = (['ply', 'splat', 'spz', 'ksplat', 'glb'].includes(ext)
      ? ext : 'ply') as SceneDesc['type'];
    if (printMode) type = 'glb';   // ?mode=print forces the mesh path
    return { file: url, type };
  }
  const resp = await fetch('/scene.json', { cache: 'no-store' });
  if (!resp.ok) {
    throw new Error(
      'No scene to show. Run preview.sh <splat> (it writes public/scene.json), ' +
      'or open the page with ?url=<splat url>.',
    );
  }
  const desc = (await resp.json()) as SceneDesc;
  if (printMode) desc.type = 'glb';
  return desc;
}

function sceneFormat(type: SceneDesc['type']): number {
  switch (type) {
    case 'ply': return GaussianSplats3D.SceneFormat.Ply;
    case 'splat': return GaussianSplats3D.SceneFormat.Splat;
    case 'spz': return GaussianSplats3D.SceneFormat.Spz;
    case 'ksplat': return GaussianSplats3D.SceneFormat.KSplat;
    default: return GaussianSplats3D.SceneFormat.Ply;
  }
}

/** Sample splat centers for geometry analysis. */
function sampleCenters(splatMesh: any, maxSamples = 12000): THREE.Vector3[] {
  const count = splatMesh.getSplatCount();
  const step = Math.max(1, Math.floor(count / maxSamples));
  const pts: THREE.Vector3[] = [];
  const p = new THREE.Vector3();
  for (let i = 0; i < count; i += step) {
    splatMesh.getSplatCenter(i, p);
    pts.push(p.clone());
  }
  return pts;
}

/** Fit the dominant flat contact face and return a rotation that puts it on the pedestal. */
function findPedestalOrientation(points: THREE.Vector3[]): THREE.Quaternion {
  const identity = new THREE.Quaternion();
  if (points.length < 30) return identity;

  const box = new THREE.Box3();
  const mean = new THREE.Vector3();
  for (const p of points) {
    box.expandByPoint(p);
    mean.add(p);
  }
  mean.divideScalar(points.length);
  const diag = box.getSize(new THREE.Vector3()).length();
  if (diag < 1e-6) return identity;

  const thresh = Math.max(diag * 0.012, 1e-4);
  const floorNormal = new THREE.Vector3(0, 1, 0);
  const rng = (n: number) => Math.floor(Math.random() * n);

  type Plane = { normal: THREE.Vector3; d: number };
  type Candidate = { normal: THREE.Vector3; score: number };

  const scorePlane = (plane: Plane): number => {
    const n = plane.normal.clone();
    let d = plane.d;
    if (n.y < 0) { n.negate(); d = -d; }
    const ref = Math.abs(n.y) < 0.9 ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(1, 0, 0);
    const tangent = ref.cross(n).normalize();
    const bitangent = n.clone().cross(tangent).normalize();

    let minU = Infinity, maxU = -Infinity, minV = Infinity, maxV = -Infinity;
    let inliers = 0;
    for (const p of points) {
      if (Math.abs(p.dot(n) + d) > thresh) continue;
      inliers++;
      const u = p.dot(tangent);
      const v = p.dot(bitangent);
      minU = Math.min(minU, u); maxU = Math.max(maxU, u);
      minV = Math.min(minV, v); maxV = Math.max(maxV, v);
    }
    if (inliers < Math.max(40, points.length * 0.04)) return 0;
    const area = (maxU - minU) * (maxV - minV);
    return inliers * Math.sqrt(Math.max(area, 0));
  };

  const fitPlane = (a: THREE.Vector3, b: THREE.Vector3, c: THREE.Vector3): Plane | null => {
    const ab = b.clone().sub(a);
    const ac = c.clone().sub(a);
    const n = ab.cross(ac);
    const len = n.length();
    if (len < 1e-8) return null;
    n.divideScalar(len);
    return { normal: n, d: -n.dot(a) };
  };

  let best: Candidate | null = null;
  for (let i = 0; i < 320; i++) {
    const plane = fitPlane(
      points[rng(points.length)],
      points[rng(points.length)],
      points[rng(points.length)],
    );
    if (!plane) continue;
    const score = scorePlane(plane);
    if (!best || score > best.score) best = { normal: plane.normal.clone(), score };
  }

  // Fallback: try the three AABB axes (±) as contact normals through the centroid.
  if (!best || best.score <= 0) {
    const size = box.getSize(new THREE.Vector3());
    const axes = [
      new THREE.Vector3(1, 0, 0),
      new THREE.Vector3(0, 1, 0),
      new THREE.Vector3(0, 0, 1),
    ];
    const order = [0, 1, 2].sort((a, b) => size.getComponent(a) - size.getComponent(b));
    for (const idx of order) {
      for (const sign of [1, -1]) {
        const n = axes[idx].clone().multiplyScalar(sign);
        const score = scorePlane({ normal: n, d: -n.dot(mean) });
        if (!best || score > best.score) best = { normal: n, score };
      }
    }
  }

  if (!best || best.score <= 0) return identity;

  const n = best.normal.clone();
  if (n.y < 0) n.negate();
  if (n.lengthSq() < 1e-8) return identity;
  return new THREE.Quaternion().setFromUnitVectors(n, floorNormal);
}

/** Rotate + shift the splat so its largest flat face rests on the pedestal. */
function orientForPedestal(splatMesh: any, viewer: GaussianSplats3D.Viewer, enabled: boolean) {
  if (!enabled) return;
  const points = sampleCenters(splatMesh);
  const q = findPedestalOrientation(points);

  splatMesh.quaternion.copy(q);
  splatMesh.updateMatrixWorld(true);

  const box = new THREE.Box3();
  const p = new THREE.Vector3();
  const count = splatMesh.getSplatCount();
  const step = Math.max(1, Math.floor(count / 50000));
  for (let i = 0; i < count; i += step) {
    splatMesh.getSplatCenter(i, p);
    p.applyMatrix4(splatMesh.matrixWorld);
    box.expandByPoint(p);
  }
  splatMesh.position.y -= box.max.y;
  splatMesh.updateMatrixWorld(true);

  (viewer as any).runSplatSort?.(true, true);
  viewer.forceRenderNextFrame?.();
}

/** Approximate splat bounds by sampling centers (robust to floaters). */
function computeBounds(
  splatMesh: any,
): { box: THREE.Box3; center: THREE.Vector3; radius: number } {
  const box = new THREE.Box3();
  const p = new THREE.Vector3();
  const count = splatMesh.getSplatCount();
  const step = Math.max(1, Math.floor(count / 50000));
  for (let i = 0; i < count; i += step) {
    splatMesh.getSplatCenter(i, p);
    // Include the object pivot transform (auto-orient places the sole on the pedestal).
    p.applyMatrix4(splatMesh.matrixWorld);
    box.expandByPoint(p);
  }
  if (box.isEmpty()) {
    box.set(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(1, 1, 1));
  }
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z) * 0.55;
  return { box, center, radius: Math.max(radius, 0.05) };
}

/** Circular studio pedestal sized to the object footprint. */
function buildPedestal(group: THREE.Group, box: THREE.Box3, radius: number) {
  group.clear();
  const cx = (box.min.x + box.max.x) * 0.5;
  const cz = (box.min.z + box.max.z) * 0.5;
  // OpenCV / COLMAP frames use +Y downward; camera up is (0,-1,0).
  const floorY = box.max.y;

  const baseH = radius * 0.07;
  const topH = radius * 0.012;

  const baseMat = new THREE.MeshStandardMaterial({
    color: 0x14141a,
    metalness: 0.55,
    roughness: 0.38,
  });
  const topMat = new THREE.MeshStandardMaterial({
    color: 0x22222e,
    metalness: 0.65,
    roughness: 0.28,
  });

  const base = new THREE.Mesh(
    new THREE.CylinderGeometry(radius * 1.02, radius * 1.12, baseH, 72),
    baseMat,
  );
  base.position.set(cx, floorY + baseH * 0.5, cz);
  base.receiveShadow = true;
  base.castShadow = true;

  const top = new THREE.Mesh(
    new THREE.CylinderGeometry(radius * 1.0, radius * 1.0, topH, 72),
    topMat,
  );
  top.position.set(cx, floorY + baseH + topH * 0.5, cz);
  top.receiveShadow = true;

  const shadow = new THREE.Mesh(
    new THREE.CircleGeometry(radius * 0.82, 64),
    new THREE.MeshBasicMaterial({
      color: 0x000000,
      transparent: true,
      opacity: 0.38,
      depthWrite: false,
    }),
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.set(cx, floorY + 0.002, cz);

  group.add(base, top, shadow);
}

function frameCamera(
  camera: THREE.PerspectiveCamera,
  controls: OrbitControls,
  center: THREE.Vector3,
  radius: number,
) {
  const dist = radius * 2.8;
  camera.position.set(center.x + dist * 0.55, center.y - dist * 0.38, center.z + dist * 0.75);
  camera.up.set(0, -1, 0);
  controls.target.copy(center);
  camera.lookAt(center);
  controls.update();
  return {
    position: camera.position.clone(),
    target: center.clone(),
  };
}

function installKeyboardOrbit(
  controls: OrbitControls,
  camera: THREE.PerspectiveCamera,
  up: THREE.Vector3,
  reset: { position: THREE.Vector3; target: THREE.Vector3 },
  onMove: () => void,
) {
  const orbitKeys = new Set([
    'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
    'KeyA', 'KeyD', 'KeyW', 'KeyS',
    'Equal', 'Minus', 'NumpadAdd', 'NumpadSubtract',
  ]);
  const pressed = new Set<string>();
  const axis = up.clone().normalize();

  const rotateAz = (delta: number) => {
    const off = camera.position.clone().sub(controls.target);
    off.applyAxisAngle(axis, delta);
    camera.position.copy(controls.target).add(off);
    camera.lookAt(controls.target);
    onMove();
  };

  const rotateEl = (delta: number) => {
    const off = camera.position.clone().sub(controls.target);
    const right = new THREE.Vector3().crossVectors(off, axis).normalize();
    if (right.lengthSq() < 1e-8) return;
    off.applyAxisAngle(right, delta);
    const minDot = Math.cos(1.45);
    if (off.clone().normalize().dot(axis) < minDot) return;
    camera.position.copy(controls.target).add(off);
    camera.lookAt(controls.target);
    onMove();
  };

  const dolly = (factor: number) => {
    const off = camera.position.clone().sub(controls.target);
    off.multiplyScalar(factor);
    camera.position.copy(controls.target).add(off);
    onMove();
  };

  window.addEventListener('keydown', (e) => {
    if (e.code === 'KeyR') {
      camera.position.copy(reset.position);
      controls.target.copy(reset.target);
      camera.lookAt(controls.target);
      onMove();
      return;
    }
    if (orbitKeys.has(e.code)) {
      pressed.add(e.code);
      e.preventDefault();
    }
  });
  window.addEventListener('keyup', (e) => pressed.delete(e.code));
  window.addEventListener('blur', () => pressed.clear());

  const ROT = 1.4;
  const ZOOM = 1.1;
  let lastT = performance.now();
  const tick = (now: number) => {
    const dt = Math.min(0.05, Math.max(0, (now - lastT) / 1000));
    lastT = now;
    if (pressed.size) {
      const has = (c: string) => pressed.has(c);
      if (has('ArrowLeft') || has('KeyA')) rotateAz(ROT * dt);
      if (has('ArrowRight') || has('KeyD')) rotateAz(-ROT * dt);
      if (has('ArrowUp') || has('KeyW')) rotateEl(ROT * dt);
      if (has('ArrowDown') || has('KeyS')) rotateEl(-ROT * dt);
      if (has('Equal') || has('NumpadAdd')) dolly(Math.exp(-ZOOM * dt));
      if (has('Minus') || has('NumpadSubtract')) dolly(Math.exp(ZOOM * dt));
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function syncAspect(camera: THREE.PerspectiveCamera, renderer: THREE.WebGLRenderer) {
  const w = container.clientWidth || window.innerWidth;
  const h = container.clientHeight || window.innerHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
}

/**
 * Print Preview: load the repaired, watertight GLB mesh (the actual print
 * geometry) instead of the raw splat. Reuses the studio pedestal + camera rig.
 */
async function runPrintPreview(sceneDesc: SceneDesc, params: URLSearchParams) {
  const dprOverride = parseFloat(params.get('dpr') || '');
  const pixelRatio = isFinite(dprOverride) && dprOverride > 0
    ? dprOverride
    : Math.min(Math.max(window.devicePixelRatio || 1, 1), 2);
  const stageOn = params.get('stage') !== '0';

  // Match the splat viewer's -Y-up convention so the pedestal/camera helpers are
  // reused. The mesh is exported +Z up (base at z=0); a +90 deg X rotation stands
  // it upright with the base resting on the pedestal.
  const cameraUp = [0, -1, 0];

  const threeScene = new THREE.Scene();
  threeScene.background = new THREE.Color(stageOn ? 0x0a0a0f : 0x0b0b0d);

  const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 500);
  camera.up.fromArray(cameraUp);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(pixelRatio);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  if (stageOn) {
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  }
  container.appendChild(renderer.domElement);
  syncAspect(camera, renderer);

  const stage = new THREE.Group();
  threeScene.add(stage);

  // Print Preview is for inspecting geometry, so light it evenly (hemisphere +
  // ambient) with a key light for form - not the moody studio rig of the splat.
  const lightTarget = new THREE.Object3D();
  threeScene.add(lightTarget);
  const lightRig = new THREE.Group();
  threeScene.add(lightRig);
  const keyLight = new THREE.DirectionalLight(0xffffff, 1.6);
  keyLight.castShadow = stageOn;
  keyLight.shadow.mapSize.set(2048, 2048);
  keyLight.target = lightTarget;
  const fillLight = new THREE.DirectionalLight(0xbfd0ff, 0.6);
  fillLight.target = lightTarget;
  const rimLight = new THREE.DirectionalLight(0xffeedd, 0.5);
  rimLight.target = lightTarget;
  lightRig.add(keyLight, fillLight, rimLight);
  threeScene.add(new THREE.HemisphereLight(0xcfe0ff, 0x30303a, 1.0));
  threeScene.add(new THREE.AmbientLight(0xffffff, 0.55));
  if (stageOn) {
    const pmrem = new THREE.PMREMGenerator(renderer);
    threeScene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
  }

  hud.textContent = 'loading print mesh ...';
  const loader = new GLTFLoader();
  let gltf: any;
  try {
    gltf = await loader.loadAsync(sceneDesc.file);
  } catch (e: any) {
    showError(
      `Failed to load print mesh "${sceneDesc.file}": ${e?.message || e}. ` +
      'Run splat_to_mesh.py first to produce mesh/object.glb.',
    );
    return;
  }

  const model = gltf.scene;
  model.rotation.x = Math.PI / 2;
  model.updateMatrixWorld(true);
  model.traverse((o: any) => {
    if (!o.isMesh) return;
    o.castShadow = true;
    o.receiveShadow = true;
    // Normalize float COLOR_0 written as 0..255 (some exporters); integer
    // (Uint8/Uint16) attributes carry normalized=true and are handled in-shader,
    // so leave those alone - dividing an integer array truncates it to black.
    const col = o.geometry?.attributes?.color;
    if (col && col.array && !col.normalized &&
        (col.array instanceof Float32Array || col.array instanceof Float64Array)) {
      let mx = 0;
      for (let i = 0; i < col.array.length; i++) {
        if (col.array[i] > mx) mx = col.array[i];
      }
      if (mx > 1.5) {
        for (let i = 0; i < col.array.length; i++) col.array[i] /= 255;
        col.needsUpdate = true;
      }
    }
    if (o.material) {
      o.material.vertexColors = !!col;
      o.material.metalness = 0.0;
      o.material.roughness = 0.9;
      o.material.side = THREE.DoubleSide;
      o.material.needsUpdate = true;
    }
  });
  threeScene.add(model);

  const box = new THREE.Box3().setFromObject(model);
  if (box.isEmpty()) {
    box.set(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(1, 1, 1));
  }
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(Math.max(size.x, size.y, size.z) * 0.55, 0.05);
  lightTarget.position.copy(center);
  if (stageOn) {
    buildPedestal(stage, box, radius);
    keyLight.shadow.camera.near = 0.01;
    keyLight.shadow.camera.far = radius * 30;
    const s = radius * 2.0;
    keyLight.shadow.camera.left = -s;
    keyLight.shadow.camera.right = s;
    keyLight.shadow.camera.top = s;
    keyLight.shadow.camera.bottom = -s;
    keyLight.shadow.camera.updateProjectionMatrix();
  }

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = true;
  controls.minDistance = Math.max(radius * 0.15, 0.01);
  controls.maxDistance = radius * 40;

  const reset = frameCamera(camera, controls, center, radius);

  const syncLights = () => {
    if (!stageOn) return;
    const target = controls.target;
    const az = Math.atan2(camera.position.x - target.x, camera.position.z - target.z);
    const r = 6;
    keyLight.position.set(
      target.x + Math.sin(az) * r, target.y - 2.5, target.z + Math.cos(az) * r,
    );
    fillLight.position.set(
      target.x - Math.sin(az) * r * 0.55, target.y - 1.2, target.z - Math.cos(az) * r * 0.55,
    );
    rimLight.position.set(target.x, target.y + 3, target.z);
  };
  syncLights();
  controls.addEventListener('change', syncLights);
  installKeyboardOrbit(controls, camera, new THREE.Vector3().fromArray(cameraUp), reset, syncLights);
  window.addEventListener('resize', () => syncAspect(camera, renderer));

  (window as any).__printModel = model;
  (window as any).__camera = camera;
  (window as any).__renderer = renderer;

  const animate = () => {
    requestAnimationFrame(animate);
    controls.update();
    syncLights();
    renderer.render(threeScene, camera);
  };
  animate();

  const dims = `${size.x.toFixed(1)} x ${size.z.toFixed(1)} x ${size.y.toFixed(1)}`;
  hud.textContent =
    `PRINT PREVIEW - repaired watertight mesh (${dims}) · orbit: drag / arrows (WASD) · ` +
    'zoom: wheel / +− · R: reset';
  setTimeout(() => { hud.style.opacity = '0.35'; }, 7000);
  hud.style.transition = 'opacity .6s';
}

async function main() {
  const params = new URLSearchParams(location.search);
  const dprOverride = parseFloat(params.get('dpr') || '');
  const pixelRatio = isFinite(dprOverride) && dprOverride > 0
    ? dprOverride
    : Math.min(Math.max(window.devicePixelRatio || 1, 1), 2);
  const stageOn = params.get('stage') !== '0';

  const sceneDesc = await resolveScene();

  if (sceneDesc.type === 'glb') {
    await runPrintPreview(sceneDesc, params);
    return;
  }

  hud.textContent = `loading ${sceneDesc.type.toUpperCase()} ...`;

  const cameraUp = sceneDesc.up ?? [0, -1, 0];

  const threeScene = new THREE.Scene();
  threeScene.background = new THREE.Color(stageOn ? 0x0a0a0f : 0x0b0b0d);

  const camera = new THREE.PerspectiveCamera(50, 1, 0.01, 500);
  camera.up.fromArray(cameraUp);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(pixelRatio);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  if (stageOn) {
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  }
  container.appendChild(renderer.domElement);
  syncAspect(camera, renderer);

  const stage = new THREE.Group();
  threeScene.add(stage);

  const lightRig = new THREE.Group();
  threeScene.add(lightRig);
  const keyLight = new THREE.DirectionalLight(0xffffff, stageOn ? 1.15 : 0.6);
  keyLight.castShadow = stageOn;
  keyLight.shadow.mapSize.set(1024, 1024);
  const fillLight = new THREE.DirectionalLight(0x8899bb, stageOn ? 0.35 : 0.2);
  const rimLight = new THREE.DirectionalLight(0xffeedd, stageOn ? 0.22 : 0.1);
  lightRig.add(keyLight, fillLight, rimLight);
  threeScene.add(new THREE.AmbientLight(0x303040, stageOn ? 0.45 : 0.25));

  if (stageOn) {
    const pmrem = new THREE.PMREMGenerator(renderer);
    threeScene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
  }

  const viewer = new GaussianSplats3D.Viewer({
    rootElement: container,
    threeScene,
    renderer,
    camera,
    cameraUp,
    selfDrivenMode: false,
    useBuiltInControls: false,
    antialiased: false,
    // Brush / COLMAP PLY exports are SH degree 0 (DC color only).
    sphericalHarmonicsDegree: 0,
    sharedMemoryForWorkers: false,
    gpuAcceleratedSort: false,
    integerBasedSort: true,
    sceneRevealMode: GaussianSplats3D.SceneRevealMode.Instant,
    renderMode: GaussianSplats3D.RenderMode.Always,
    logLevel: GaussianSplats3D.LogLevel.None,
  });

  try {
    await viewer.addSplatScene(sceneDesc.file, {
      format: sceneFormat(sceneDesc.type),
      showLoadingUI: false,
      splatAlphaRemovalThreshold: 1,
      progressiveLoad: false,
    });
  } catch (e: any) {
    showError(`Failed to load splat "${sceneDesc.file}": ${e?.message || e}`);
    return;
  }

  const splatMesh = (viewer as any).splatMesh;
  if (!splatMesh) {
    showError('Splat loaded but mesh is missing.');
    return;
  }

  const autoOrient = stageOn && params.get('orient') !== '0';
  if (autoOrient) orientForPedestal(splatMesh, viewer, true);

  const { box, center, radius } = computeBounds(splatMesh);
  if (stageOn) buildPedestal(stage, box, radius);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = true;
  controls.minDistance = Math.max(radius * 0.15, 0.01);
  controls.maxDistance = radius * 40;

  const reset = frameCamera(camera, controls, center, radius);

  const syncLights = () => {
    if (!stageOn) return;
    const target = controls.target;
    const az = Math.atan2(camera.position.x - target.x, camera.position.z - target.z);
    const r = 6;
    keyLight.position.set(
      target.x + Math.sin(az) * r, target.y - 2.5, target.z + Math.cos(az) * r,
    );
    fillLight.position.set(
      target.x - Math.sin(az) * r * 0.55, target.y - 1.2, target.z - Math.cos(az) * r * 0.55,
    );
    rimLight.position.set(target.x, target.y + 3, target.z);
  };

  syncLights();
  controls.addEventListener('change', syncLights);
  installKeyboardOrbit(controls, camera, new THREE.Vector3().fromArray(cameraUp), reset, syncLights);

  window.addEventListener('resize', () => syncAspect(camera, renderer));

  (window as any).__viewer = viewer;
  (window as any).__camera = camera;
  (window as any).__renderer = renderer;

  const animate = () => {
    requestAnimationFrame(animate);
    controls.update();
    syncLights();
    viewer.update();
    viewer.render();
  };
  animate();

  hud.textContent =
    'orbit: drag / arrows (WASD) · zoom: wheel / +− · pan: shift-drag / right-drag · R: reset';
  setTimeout(() => { hud.style.opacity = '0.35'; }, 6000);
  hud.style.transition = 'opacity .6s';
}

main().catch((e) => showError(e?.message || String(e)));
