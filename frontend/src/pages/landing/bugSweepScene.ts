/**
 * The landing page's hero effect: a field of syntax-highlighted "code lines" with bugs
 * crawling over them. Bugs infect the lines around them (red) and breed if left alone. A
 * scanner beam sweeps the codebase; every bug it touches bursts into particles and its
 * line flashes green as it heals.
 *
 * Plain three.js (no React): `mountBugSweep` builds everything into a container and
 * returns a dispose function. Throws if WebGL is unavailable, so callers can fall back.
 */
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";


// Code layout: side-by-side "files", each a column of indented lines made of tokens.
const PANES = 5;
const ROWS = 74;
const PANE_WIDTH = 5.2;
const PANE_GAP = 0.9;
const ROW_STEP = 0.3;
const TOKEN_HEIGHT = 0.05;
const TOKEN_DEPTH = 0.13;
const FIELD_WIDTH = PANES * PANE_WIDTH + (PANES - 1) * PANE_GAP;
const FIELD_DEPTH = ROWS * ROW_STEP;
// The field runs from just in front of the camera's focus back into the fog.
const FIELD_NEAR_Z = 4;
const FIELD_CENTER_Z = FIELD_NEAR_Z - FIELD_DEPTH / 2;

const MAX_BUGS = 64;
const START_BUGS = 14;
const BREED_EVERY = 3.5; // seconds between breeding checks per bug
const SWEEP_SECONDS = 5.5; // one pass of the scanner across the field
const PARTICLES = 3000;
const PARTICLES_PER_BURST = 46;
const PARTICLE_LIFE = 1.3;
const BUG_SCALE = 1.45;

export type SceneTheme = "light" | "dark";

type Palette = {
  background: number;
  plate: number;
  syntax: THREE.Color[];
  infected: THREE.Color;
  healed: THREE.Color;
  scan: THREE.Color;
  bugBody: number;
  bugGlow: number; // emissive intensity
  bloom: number; // bloom strength; 0 skips the pass (a pale background would bloom everywhere)
  // Additive light vanishes on a pale background, so light mode blends normally.
  blending: THREE.Blending;
};

const PALETTES: Record<SceneTheme, Palette> = {
  dark: {
    background: 0x120a0c,
    plate: 0x1c1014,
    // Dim syntax colors (kept below the bloom threshold so healthy code doesn't glow).
    syntax: [0x5a2e2a, 0x4a2640, 0x5a3c28, 0x40283a, 0x584428, 0x4a2a30].map((hex) => new THREE.Color(hex)),
    infected: new THREE.Color(1.4, 0.15, 0.9),
    healed: new THREE.Color(0.35, 1.5, 0.7),
    scan: new THREE.Color(1.4, 0.55, 0.25),
    bugBody: 0x3a0528,
    bugGlow: 1.3,
    bloom: 1.05,
    blending: THREE.AdditiveBlending,
  },
  light: {
    background: 0xefe9e8,
    plate: 0xf8f4f3,
    syntax: [0xb88a80, 0xae8aa3, 0xbd9a74, 0xa88c9c, 0xb8a276, 0xb48a8f].map((hex) => new THREE.Color(hex)),
    infected: new THREE.Color(0.85, 0.08, 0.55),
    healed: new THREE.Color(0.1, 0.62, 0.36),
    scan: new THREE.Color(0.9, 0.38, 0.2),
    bugBody: 0x8a0f5a,
    bugGlow: 0.35,
    bloom: 0,
    blending: THREE.NormalBlending,
  },
};

type Line = {
  pane: number;
  row: number;
  x0: number;
  x1: number;
  z: number;
  infection: number; // 0..1, pulled up while a bug sits on (or next to) the line
  heal: number; // 1 right after a fix, decays to 0
};

type Bug = {
  line: number;
  x: number;
  dir: 1 | -1;
  speed: number;
  phase: number;
  born: number;
  alive: boolean;
  nextBreed: number;
};

export type BugSweepOptions = {
  theme?: SceneTheme;
  reducedMotion?: boolean;
};

function rand(min: number, max: number) {
  return min + Math.random() * (max - min);
}

function buildCode(syntax: THREE.Color[]) {
  const lines: Line[] = [];
  const tokens: { line: number; x: number; width: number; color: THREE.Color }[] = [];
  for (let pane = 0; pane < PANES; pane++) {
    const paneX = -FIELD_WIDTH / 2 + pane * (PANE_WIDTH + PANE_GAP);
    let indent = 0;
    for (let row = 0; row < ROWS; row++) {
      // Blank lines and indentation changes make it read like real code.
      if (Math.random() < 0.12) continue;
      const r = Math.random();
      if (r < 0.2) indent = Math.min(indent + 1, 4);
      else if (r < 0.38) indent = Math.max(indent - 1, 0);
      const z = FIELD_NEAR_Z - FIELD_DEPTH + row * ROW_STEP;
      const start = paneX + 0.2 + indent * 0.42;
      const maxWidth = paneX + PANE_WIDTH - start - 0.1;
      const lineIndex = lines.length;
      let x = start;
      const count = 1 + Math.floor(Math.random() * 5);
      for (let t = 0; t < count && x < start + maxWidth - 0.3; t++) {
        const width = Math.min(rand(0.25, 1.4), start + maxWidth - x);
        tokens.push({
          line: lineIndex,
          x: x + width / 2,
          width,
          color: syntax[Math.floor(Math.random() * syntax.length)],
        });
        x += width + 0.12;
      }
      lines.push({ pane, row, x0: start, x1: x - 0.12, z, infection: 0, heal: 0 });
    }
  }
  return { lines, tokens };
}

function buildBugGeometry() {
  const parts: THREE.BufferGeometry[] = [];
  const body = new THREE.SphereGeometry(0.15, 14, 10);
  body.scale(1, 0.55, 1.4);
  parts.push(body);
  const head = new THREE.SphereGeometry(0.075, 10, 8);
  head.translate(0, 0.01, 0.22);
  parts.push(head);
  // Six legs, splayed and slightly bent back, plus two antennae.
  for (const side of [-1, 1]) {
    for (const [i, z] of [-0.09, 0.0, 0.09].entries()) {
      const leg = new THREE.BoxGeometry(0.2, 0.018, 0.018);
      leg.rotateY(side * (0.35 - i * 0.35));
      leg.rotateZ(side * -0.35);
      leg.translate(side * 0.17, -0.03, z);
      parts.push(leg);
    }
    const antenna = new THREE.BoxGeometry(0.012, 0.012, 0.16);
    antenna.rotateY(side * 0.45);
    antenna.rotateX(-0.4);
    antenna.translate(side * 0.05, 0.05, 0.32);
    parts.push(antenna);
  }
  const merged = mergeGeometries(parts.map((p) => p.toNonIndexed()));
  parts.forEach((p) => p.dispose());
  return merged;
}

function buildParticles(blending: THREE.Blending) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(PARTICLES * 3), 3));
  geometry.setAttribute("velocity", new THREE.BufferAttribute(new Float32Array(PARTICLES * 3), 3));
  geometry.setAttribute("aColor", new THREE.BufferAttribute(new Float32Array(PARTICLES * 3), 3));
  geometry.setAttribute(
    "birth",
    new THREE.BufferAttribute(new Float32Array(PARTICLES).fill(-100), 1),
  );
  const material = new THREE.ShaderMaterial({
    uniforms: { uTime: { value: 0 }, uPixelRatio: { value: 1 } },
    vertexShader: /* glsl */ `
      uniform float uTime;
      uniform float uPixelRatio;
      attribute vec3 velocity;
      attribute vec3 aColor;
      attribute float birth;
      varying vec3 vColor;
      varying float vAlpha;
      void main() {
        float age = uTime - birth;
        float life = ${PARTICLE_LIFE.toFixed(2)};
        vColor = aColor;
        if (age < 0.0 || age > life) {
          vAlpha = 0.0;
          gl_PointSize = 0.0;
          gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
          return;
        }
        vec3 p = position + velocity * age + vec3(0.0, -3.2, 0.0) * age * age * 0.5;
        p.y = max(p.y, 0.03);
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        float fade = 1.0 - age / life;
        vAlpha = fade * fade;
        gl_PointSize = (9.0 + 14.0 * fade) * uPixelRatio / -mv.z * 4.0;
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: /* glsl */ `
      varying vec3 vColor;
      varying float vAlpha;
      void main() {
        float d = length(gl_PointCoord - 0.5);
        float glow = 1.0 - smoothstep(0.0, 0.5, d);
        gl_FragColor = vec4(vColor * glow * 1.6, glow * vAlpha);
      }
    `,
    transparent: true,
    depthWrite: false,
    blending,
  });
  const points = new THREE.Points(geometry, material);
  points.frustumCulled = false;
  return { points, geometry, material };
}

function buildBeam(scan: THREE.Color, blending: THREE.Blending) {
  const material = new THREE.ShaderMaterial({
    uniforms: { uTime: { value: 0 }, uColor: { value: scan } },
    vertexShader: /* glsl */ `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime;
      uniform vec3 uColor;
      varying vec2 vUv;
      void main() {
        // Interpolated UVs can land just outside 0..1, and pow() of a negative base is NaN, which
        // the bloom pass would smear across the whole frame as a one-frame blackout.
        float rise = pow(clamp(1.0 - vUv.y, 0.0, 1.0), 2.2);
        float edge = smoothstep(0.0, 0.12, vUv.x) * (1.0 - smoothstep(0.88, 1.0, vUv.x));
        float scan = 0.75 + 0.25 * sin(vUv.y * 60.0 - uTime * 14.0);
        gl_FragColor = vec4(uColor, rise * edge * scan * 0.55);
      }
    `,
    transparent: true,
    depthWrite: false,
    side: THREE.DoubleSide,
    blending,
  });
  const curtain = new THREE.Mesh(new THREE.PlaneGeometry(FIELD_DEPTH + 1.5, 3.2), material);
  curtain.rotation.y = Math.PI / 2;
  curtain.position.y = 1.6;
  const floorLine = new THREE.Mesh(
    new THREE.BoxGeometry(0.06, 0.03, FIELD_DEPTH + 1.5),
    new THREE.MeshBasicMaterial({ color: scan.clone().multiplyScalar(1.4), toneMapped: false }),
  );
  curtain.position.z = floorLine.position.z = FIELD_CENTER_Z;
  const beam = new THREE.Group();
  beam.add(curtain, floorLine);
  return { beam, material, curtain, floorLine };
}

export function mountBugSweep(container: HTMLElement, options: BugSweepOptions = {}) {
  const { theme = "dark", reducedMotion = false } = options;
  const palette = PALETTES[theme];
  const { infected: INFECTED, healed: HEALED, scan: SCAN } = palette;

  // Throws when WebGL isn't available (old devices, jsdom); the caller falls back.
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(palette.background, 1);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.domElement.style.display = "block";
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(palette.background, 0.034);

  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
  const cameraHome = new THREE.Vector3(0, 6.2, 10.5);
  camera.position.copy(cameraHome);
  const lookTarget = new THREE.Vector3(0, -0.4, -4.2);

  scene.add(new THREE.AmbientLight(0xffffff, 0.35));
  const key = new THREE.DirectionalLight(0xffffff, 1.2);
  key.position.set(4, 10, 6);
  scene.add(key);

  // --- code tokens -------------------------------------------------------------------
  const { lines, tokens } = buildCode(palette.syntax);
  const tokenGeometry = new THREE.BoxGeometry(1, TOKEN_HEIGHT, TOKEN_DEPTH);
  const tokenMaterial = new THREE.MeshBasicMaterial({ toneMapped: false });
  const tokenMesh = new THREE.InstancedMesh(tokenGeometry, tokenMaterial, tokens.length);
  const matrix = new THREE.Matrix4();
  tokens.forEach((token, i) => {
    matrix.makeScale(token.width, 1, 1);
    matrix.setPosition(token.x, 0, lines[token.line].z);
    tokenMesh.setMatrixAt(i, matrix);
    tokenMesh.setColorAt(i, token.color);
  });
  scene.add(tokenMesh);

  // Faint pane backplates so the lines read as files.
  const plateMaterial = new THREE.MeshBasicMaterial({
    color: palette.plate,
    transparent: true,
    opacity: 0.85,
  });
  const plateGeometry = new THREE.PlaneGeometry(PANE_WIDTH, FIELD_DEPTH + 0.6);
  for (let pane = 0; pane < PANES; pane++) {
    const plate = new THREE.Mesh(plateGeometry, plateMaterial);
    plate.rotation.x = -Math.PI / 2;
    plate.position.set(
      -FIELD_WIDTH / 2 + pane * (PANE_WIDTH + PANE_GAP) + PANE_WIDTH / 2,
      -0.04,
      FIELD_CENTER_Z - ROW_STEP / 2,
    );
    scene.add(plate);
  }

  // Index lines by (pane, row) so infection can spread to neighbors.
  const lineAt = new Map<string, number>();
  lines.forEach((line, i) => lineAt.set(`${line.pane}:${line.row}`, i));
  const neighbors = lines.map((line) =>
    [-1, 1]
      .map((d) => lineAt.get(`${line.pane}:${line.row + d}`))
      .filter((n): n is number => n !== undefined),
  );

  // --- bugs --------------------------------------------------------------------------
  const bugGeometry = buildBugGeometry();
  const bugMaterial = new THREE.MeshStandardMaterial({
    color: palette.bugBody,
    emissive: new THREE.Color(1.0, 0.1, 0.65),
    emissiveIntensity: palette.bugGlow,
    roughness: 0.4,
    metalness: 0.2,
  });
  const bugMesh = new THREE.InstancedMesh(bugGeometry, bugMaterial, MAX_BUGS);
  bugMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  scene.add(bugMesh);
  const bugs: Bug[] = [];

  function spawnBug(now: number, onLine?: number) {
    if (bugs.filter((b) => b.alive).length >= MAX_BUGS) return;
    const line = onLine ?? Math.floor(Math.random() * lines.length);
    const { x0, x1 } = lines[line];
    const bug: Bug = {
      line,
      x: rand(x0, Math.max(x0, x1)),
      dir: Math.random() < 0.5 ? 1 : -1,
      speed: rand(0.25, 0.7),
      phase: Math.random() * Math.PI * 2,
      born: now,
      alive: true,
      nextBreed: now + BREED_EVERY * rand(0.8, 1.6),
    };
    const free = bugs.findIndex((b) => !b.alive);
    if (free >= 0) bugs[free] = bug;
    else bugs.push(bug);
  }

  // --- particles & beam --------------------------------------------------------------
  const particles = buildParticles(palette.blending);
  scene.add(particles.points);
  let nextParticle = 0;
  const burstColors = [INFECTED, HEALED, SCAN];

  function burst(position: THREE.Vector3, now: number) {
    const pos = particles.geometry.getAttribute("position") as THREE.BufferAttribute;
    const vel = particles.geometry.getAttribute("velocity") as THREE.BufferAttribute;
    const col = particles.geometry.getAttribute("aColor") as THREE.BufferAttribute;
    const birth = particles.geometry.getAttribute("birth") as THREE.BufferAttribute;
    for (let n = 0; n < PARTICLES_PER_BURST; n++) {
      const i = nextParticle;
      nextParticle = (nextParticle + 1) % PARTICLES;
      const angle = Math.random() * Math.PI * 2;
      const speed = rand(0.6, 2.6);
      pos.setXYZ(i, position.x, position.y, position.z);
      vel.setXYZ(i, Math.cos(angle) * speed, rand(1.2, 3.6), Math.sin(angle) * speed);
      const c = burstColors[n % burstColors.length];
      col.setXYZ(i, c.r, c.g, c.b);
      birth.setX(i, now + Math.random() * 0.06);
    }
    pos.needsUpdate = vel.needsUpdate = col.needsUpdate = birth.needsUpdate = true;
  }

  const beam = buildBeam(SCAN, palette.blending);
  scene.add(beam.beam);

  // --- post-processing ---------------------------------------------------------------
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), palette.bloom, 0.55, 0.62);
  if (palette.bloom > 0) composer.addPass(bloom);
  composer.addPass(new OutputPass());

  // --- sizing & input ----------------------------------------------------------------
  function resize() {
    const width = Math.max(container.clientWidth, 1);
    const height = Math.max(container.clientHeight, 1);
    renderer.setSize(width, height, false);
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    composer.setSize(width, height);
    bloom.setSize(width, height);
    camera.aspect = width / height;
    // Narrow screens: back off so the whole codebase stays in view.
    camera.position.z = cameraHome.z + (camera.aspect < 1 ? 3.5 : 0);
    camera.updateProjectionMatrix();
    particles.material.uniforms.uPixelRatio.value = renderer.getPixelRatio();
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);
  resize();

  const pointer = new THREE.Vector2();
  const onPointerMove = (event: PointerEvent) => {
    const rect = container.getBoundingClientRect();
    pointer.set(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      ((event.clientY - rect.top) / rect.height) * 2 - 1,
    );
  };
  if (!reducedMotion) window.addEventListener("pointermove", onPointerMove);

  // --- simulation --------------------------------------------------------------------
  const start = performance.now() / 1000;
  let last = 0;
  let frame = 0;
  const color = new THREE.Color();
  const bugPosition = new THREE.Vector3();
  const quaternion = new THREE.Quaternion();
  const scale = new THREE.Vector3();
  const up = new THREE.Vector3(0, 1, 0);
  const speedFactor = reducedMotion ? 0.35 : 1;

  for (let i = 0; i < START_BUGS; i++) spawnBug(-rand(0, 2));

  function tick() {
    frame = requestAnimationFrame(tick);
    const now = performance.now() / 1000 - start;
    const dt = Math.min(now - last, 0.05);
    last = now;
    const t = now * speedFactor;

    // Scanner ping-pongs across the field.
    const phase = (t / SWEEP_SECONDS) % 2;
    const sweep = phase < 1 ? phase : 2 - phase;
    const eased = sweep * sweep * (3 - 2 * sweep);
    const beamX = -FIELD_WIDTH / 2 - 0.6 + eased * (FIELD_WIDTH + 1.2);
    beam.beam.position.x = beamX;
    beam.material.uniforms.uTime.value = now;

    // Bugs: crawl, bob, breed, and get removed by the beam.
    for (const bug of bugs) {
      if (!bug.alive) continue;
      const line = lines[bug.line];
      bug.x += bug.dir * bug.speed * dt * speedFactor;
      if (bug.x > line.x1 || bug.x < line.x0) {
        bug.dir = bug.x > line.x1 ? -1 : 1;
        bug.x = THREE.MathUtils.clamp(bug.x, line.x0, line.x1);
        // Sometimes hop to the neighboring line instead of turning around.
        const hop = neighbors[bug.line];
        if (hop.length && Math.random() < 0.35) {
          bug.line = hop[Math.floor(Math.random() * hop.length)];
          const next = lines[bug.line];
          bug.x = THREE.MathUtils.clamp(bug.x, next.x0, next.x1);
        }
      }
      if (now > bug.nextBreed) {
        bug.nextBreed = now + BREED_EVERY * rand(0.8, 1.6);
        const hop = neighbors[bug.line];
        if (hop.length && Math.random() < 0.45) {
          spawnBug(now, hop[Math.floor(Math.random() * hop.length)]);
        }
      }
      if (now - bug.born > 0.35 && Math.abs(bug.x - beamX) < 0.3 * BUG_SCALE) {
        bug.alive = false;
        line.heal = 1;
        burst(bugPosition.set(bug.x, 0.15 * BUG_SCALE, line.z), now);
      }
    }
    // Keep the codebase from ever being clean for long.
    const alive = bugs.filter((b) => b.alive).length;
    if (alive < 6 || Math.random() < dt * 0.9) spawnBug(now);

    // Infection follows the bugs; healing flashes decay.
    const pull = new Float32Array(lines.length);
    for (const bug of bugs) {
      if (!bug.alive) continue;
      pull[bug.line] = 1;
      for (const n of neighbors[bug.line]) pull[n] = Math.max(pull[n], 0.35);
    }
    lines.forEach((line, i) => {
      const target = pull[i];
      const rate = target > line.infection ? 2.2 : 1.4;
      line.infection += (target - line.infection) * Math.min(1, dt * rate);
      line.heal = Math.max(0, line.heal - dt * 0.9);
    });

    tokens.forEach((token, i) => {
      const line = lines[token.line];
      color.copy(token.color);
      color.lerp(INFECTED, line.infection * 0.85);
      color.lerp(HEALED, line.heal * line.heal);
      const near = Math.abs(token.x - beamX);
      if (near < 1.2) color.lerp(SCAN, (1 - near / 1.2) * 0.55);
      tokenMesh.setColorAt(i, color);
    });
    tokenMesh.instanceColor!.needsUpdate = true;

    let count = 0;
    for (const bug of bugs) {
      if (!bug.alive) continue;
      const line = lines[bug.line];
      const grow = Math.min(1, (now - bug.born) / 0.4);
      const wobble = Math.sin(now * 14 + bug.phase);
      bugPosition.set(bug.x, (0.11 + Math.abs(wobble) * 0.025) * BUG_SCALE, line.z);
      quaternion.setFromAxisAngle(up, (bug.dir > 0 ? Math.PI / 2 : -Math.PI / 2) + wobble * 0.12);
      scale.setScalar(grow * 0.95 * BUG_SCALE);
      matrix.compose(bugPosition, quaternion, scale);
      bugMesh.setMatrixAt(count++, matrix);
    }
    bugMesh.count = count;
    bugMesh.instanceMatrix.needsUpdate = true;
    bugMaterial.emissiveIntensity = palette.bugGlow * (0.9 + Math.sin(now * 5) * 0.15);

    particles.material.uniforms.uTime.value = now;

    // Gentle drift plus pointer parallax.
    const drift = reducedMotion ? 0 : Math.sin(now * 0.15) * 0.8;
    const targetX = cameraHome.x + drift + pointer.x * 1.6;
    const targetY = cameraHome.y - pointer.y * 1.0;
    camera.position.x += (targetX - camera.position.x) * Math.min(1, dt * 2);
    camera.position.y += (targetY - camera.position.y) * Math.min(1, dt * 2);
    camera.lookAt(lookTarget);

    composer.render();
  }
  tick();

  return function dispose() {
    cancelAnimationFrame(frame);
    resizeObserver.disconnect();
    window.removeEventListener("pointermove", onPointerMove);
    scene.traverse((object) => {
      const mesh = object as THREE.Mesh;
      mesh.geometry?.dispose();
      const material = mesh.material as THREE.Material | THREE.Material[] | undefined;
      if (Array.isArray(material)) material.forEach((m) => m.dispose());
      else material?.dispose();
    });
    composer.dispose();
    renderer.dispose();
    renderer.domElement.remove();
  };
}
