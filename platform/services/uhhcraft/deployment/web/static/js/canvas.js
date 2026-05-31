/**
 * UhhCraft 3D Canvas
 *
 * Loads a GLB mesh (3D prints) or applies a PNG texture to a flat sticker
 * mesh (stickers) inside a Three.js viewport.
 *
 * Dependencies: Three.js r166+ (loaded from /static/js/three.module.min.js)
 *
 * To download Three.js:
 *   https://github.com/mrdoob/three.js/releases
 *   Copy build/three.module.min.js → web/static/js/three.module.min.js
 *   Copy examples/jsm/controls/OrbitControls.js → web/static/js/OrbitControls.js
 *   Copy examples/jsm/loaders/GLTFLoader.js → web/static/js/GLTFLoader.js
 */

import * as THREE from '/static/js/three.module.min.js';
import { OrbitControls } from '/static/js/OrbitControls.js';
import { GLTFLoader } from '/static/js/GLTFLoader.js';

const canvas   = document.getElementById('three-canvas');
const assetURL = canvas?.dataset.assetUrl;
const assetType = canvas?.dataset.assetType;   // 'glb' | 'sticker-png'
const productType = canvas?.dataset.productType; // 'sticker' | 'print'

if (!canvas || !assetURL) {
  // Asset not ready yet (generation still pending) — canvas.js will re-run
  // when the page refreshes after HTMX polling detects completion.
  console.info('[canvas] Waiting for asset URL...');
} else {
  initCanvas();
}

function initCanvas() {
  // ── Renderer ──────────────────────────────────────────────────────────────
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.2;

  // ── Scene ─────────────────────────────────────────────────────────────────
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x1A1714); // dark-page token

  // ── Camera ────────────────────────────────────────────────────────────────
  const camera = new THREE.PerspectiveCamera(45, canvas.clientWidth / canvas.clientHeight, 0.1, 100);
  camera.position.set(0, 0, 3);

  // Size the renderer + camera now that both exist (resize() dereferences
  // camera.aspect, so it must run after the camera is constructed).
  resize();

  // ── Lighting ─────────────────────────────────────────────────────────────
  scene.add(new THREE.AmbientLight(0xffffff, 0.6));
  const key = new THREE.DirectionalLight(0xffffff, 1.2);
  key.position.set(2, 4, 3);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xE8732A, 0.3); // warm orange fill
  fill.position.set(-3, -1, -2);
  scene.add(fill);

  // ── Controls ──────────────────────────────────────────────────────────────
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.minDistance = 1;
  controls.maxDistance = 8;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.8;

  // Stop auto-rotate on first user interaction
  controls.addEventListener('start', () => { controls.autoRotate = false; });

  // ── Asset loading ─────────────────────────────────────────────────────────
  if (assetType === 'glb') {
    loadGLB(scene, camera, controls);
  } else {
    loadStickerPNG(scene, camera, controls);
  }

  // ── Reduced motion ────────────────────────────────────────────────────────
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    controls.autoRotate = false;
  }

  // ── Render loop ───────────────────────────────────────────────────────────
  function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  // ── Resize ────────────────────────────────────────────────────────────────
  window.addEventListener('resize', () => {
    resize();
    camera.aspect = canvas.clientWidth / canvas.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(canvas.clientWidth, canvas.clientHeight, false);
  });

  function resize() {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (canvas.width !== w || canvas.height !== h) {
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
  }
}

// ── GLB loader (3D prints) ────────────────────────────────────────────────────

function loadGLB(scene, camera, controls) {
  const loader = new GLTFLoader();
  showLoading(true);
  loader.load(
    assetURL,
    (gltf) => {
      // Centre and scale model
      const box = new THREE.Box3().setFromObject(gltf.scene);
      const centre = box.getCenter(new THREE.Vector3());
      const size   = box.getSize(new THREE.Vector3());
      const maxDim = Math.max(size.x, size.y, size.z);
      gltf.scene.scale.multiplyScalar(2.0 / maxDim);
      gltf.scene.position.sub(centre.multiplyScalar(2.0 / maxDim));

      scene.add(gltf.scene);
      controls.target.set(0, 0, 0);
      controls.update();
      showLoading(false);
    },
    undefined,
    (err) => {
      console.error('[canvas] GLB load error', err);
      showError();
    }
  );
}

// ── PNG-to-sticker mesh (stickers) ────────────────────────────────────────────
// Loads the generated PNG, applies it as a texture to a slightly-rounded
// quad, simulating a physical sticker.

function loadStickerPNG(scene, camera, controls) {
  showLoading(true);
  const loader = new THREE.TextureLoader();
  loader.load(
    assetURL,
    (texture) => {
      texture.colorSpace = THREE.SRGBColorSpace;
      // Maintain aspect ratio of the image
      const aspect = texture.image.width / texture.image.height;
      const geo = new THREE.PlaneGeometry(aspect * 2, 2, 1, 1);
      const mat = new THREE.MeshStandardMaterial({
        map: texture,
        transparent: true,
        side: THREE.DoubleSide,
        roughness: 0.4,
        metalness: 0.0,
      });
      const mesh = new THREE.Mesh(geo, mat);

      // Slight tilt for a natural feel
      mesh.rotation.x = 0.15;
      scene.add(mesh);

      // Add a subtle drop shadow plane below
      const shadowGeo = new THREE.PlaneGeometry(aspect * 2.2, 2.2);
      const shadowMat = new THREE.MeshBasicMaterial({
        color: 0x000000, transparent: true, opacity: 0.12,
        side: THREE.DoubleSide,
      });
      const shadow = new THREE.Mesh(shadowGeo, shadowMat);
      shadow.rotation.x = 0.15;
      shadow.position.z = -0.02;
      scene.add(shadow);

      controls.target.set(0, 0, 0);
      controls.update();
      showLoading(false);
    },
    undefined,
    (err) => {
      console.error('[canvas] PNG load error', err);
      showError();
    }
  );
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function showLoading(visible) {
  const el = document.getElementById('canvas-loading');
  if (el) el.style.display = visible ? '' : 'none';
}

function showError() {
  showLoading(false);
  const el = document.getElementById('canvas-error');
  if (el) el.style.display = '';
}
