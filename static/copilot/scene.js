/* The constellation behind Hindsight: services as glowing nodes, their
   dependencies as links, packets of telemetry moving along them. Nodes near
   the pointer light up and the whole field leans toward it. The investigation
   page pulses it on each stage (see "hindsight:pulse" in copilot.js).

   Everything is optional: without WebGL, or if the CDN is unreachable, the
   CSS gradient on <body> is the background and nothing else changes. */

const THREE_URL = "https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.js";

const NODE_VERT = `
  uniform float uTime, uPixel, uAspect, uPulse;
  uniform vec2 uMouse;
  attribute float aSeed, aKind;
  varying float vGlow, vKind;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vec4 clip = projectionMatrix * mv;
    vec2 ndc = clip.xy / clip.w;
    float near = smoothstep(0.55, 0.0, distance(ndc * vec2(uAspect, 1.0), uMouse * vec2(uAspect, 1.0)));
    float twinkle = 0.55 + 0.45 * sin(uTime * 1.3 + aSeed * 6.2831);
    float alarm = aKind * (0.5 + 0.5 * sin(uTime * 3.2 + aSeed * 11.0));
    vGlow = 0.5 * twinkle + near * 2.2 + alarm * 0.9 + uPulse * 0.55;
    vKind = aKind;
    gl_PointSize = (5.5 + near * 16.0 + aKind * 4.0 + alarm * 5.0 + uPulse * 2.5) * uPixel * (32.0 / -mv.z);
    gl_Position = clip;
  }`;

const NODE_FRAG = `
  uniform vec3 uNode, uAlarm;
  uniform float uAlpha;
  varying float vGlow, vKind;
  void main() {
    float r = length(gl_PointCoord - 0.5);
    // A bright centre inside a soft halo.
    float core = smoothstep(0.5, 0.0, r);
    core = core * core * 0.7 + smoothstep(0.16, 0.0, r) * 0.6;
    vec3 colour = mix(uNode, uAlarm, vKind);
    gl_FragColor = vec4(colour * (0.55 + vGlow), core * clamp(vGlow, 0.0, 1.5) * uAlpha);
  }`;

const LINK_VERT = `
  uniform float uAspect;
  uniform vec2 uMouse;
  attribute float aSeed, aEnd;
  varying float vNear, vSeed, vEnd;
  void main() {
    vec4 clip = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    vec2 ndc = clip.xy / clip.w;
    vNear = smoothstep(0.6, 0.0, distance(ndc * vec2(uAspect, 1.0), uMouse * vec2(uAspect, 1.0)));
    vSeed = aSeed;
    vEnd = aEnd;
    gl_Position = clip;
  }`;

const LINK_FRAG = `
  uniform vec3 uLink, uPacket;
  uniform float uTime, uPulse, uAlpha;
  varying float vNear, vSeed, vEnd;
  void main() {
    // A packet of telemetry travelling along some links; all of them during a pulse.
    float carries = max(step(0.68, fract(vSeed * 7.31)), uPulse);
    float head = fract(uTime * (0.18 + 0.2 * fract(vSeed * 3.7)) + vSeed);
    float packet = smoothstep(0.09, 0.0, abs(vEnd - head)) * carries;
    float base = 0.13 + vNear * 0.55 + uPulse * 0.22;
    vec3 colour = mix(uLink, uPacket, clamp(packet + vNear * 0.4, 0.0, 1.0));
    gl_FragColor = vec4(colour, (base + packet * 0.9) * uAlpha);
  }`;

const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function main() {
  const canvas = document.getElementById("scene");
  if (!canvas) return;
  let THREE;
  try {
    THREE = await import(THREE_URL);
  } catch {
    return; // offline or blocked: the CSS background stays
  }

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: "low-power" });
  } catch {
    return; // no WebGL
  }
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.75));

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 100);
  camera.position.set(0, 0, 22);
  const field = new THREE.Group();
  scene.add(field);

  // --- nodes ---
  const count = innerWidth < 700 ? 80 : 150;
  const points = [];
  const positions = new Float32Array(count * 3);
  const seeds = new Float32Array(count);
  const kinds = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    // A flattened cloud, wider than tall, deeper behind the page than in front.
    const p = new THREE.Vector3(
      (Math.random() - 0.5) * 34, (Math.random() - 0.5) * 18, -Math.random() * 16 + 3);
    points.push(p);
    positions.set([p.x, p.y, p.z], i * 3);
    seeds[i] = Math.random();
    kinds[i] = Math.random() < 0.045 ? 1 : 0; // a few nodes are mid-incident
  }
  const uniforms = {
    uTime: { value: 0 }, uPulse: { value: 0 }, uMouse: { value: new THREE.Vector2(9, 9) },
    uPixel: { value: renderer.getPixelRatio() }, uAspect: { value: 1 }, uAlpha: { value: 1 },
    uNode: { value: new THREE.Color() }, uAlarm: { value: new THREE.Color() },
    uLink: { value: new THREE.Color() }, uPacket: { value: new THREE.Color() },
  };
  const nodeGeo = new THREE.BufferGeometry();
  nodeGeo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  nodeGeo.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 1));
  nodeGeo.setAttribute("aKind", new THREE.BufferAttribute(kinds, 1));
  const nodeMat = new THREE.ShaderMaterial({
    uniforms, vertexShader: NODE_VERT, fragmentShader: NODE_FRAG,
    transparent: true, depthWrite: false,
  });
  field.add(new THREE.Points(nodeGeo, nodeMat));

  // --- links: each node to its two nearest neighbours, if close enough ---
  const linkPos = [];
  const linkSeed = [];
  const linkEnd = [];
  const seen = new Set();
  points.forEach((p, i) => {
    points
      .map((q, j) => [j, p.distanceToSquared(q)])
      .filter(([j, d]) => j !== i && d < 36)
      .sort((a, b) => a[1] - b[1])
      .slice(0, 2)
      .forEach(([j]) => {
        const key = i < j ? `${i}-${j}` : `${j}-${i}`;
        if (seen.has(key)) return;
        seen.add(key);
        const s = Math.random();
        linkPos.push(p.x, p.y, p.z, points[j].x, points[j].y, points[j].z);
        linkSeed.push(s, s);
        linkEnd.push(0, 1);
      });
  });
  const linkGeo = new THREE.BufferGeometry();
  linkGeo.setAttribute("position", new THREE.Float32BufferAttribute(linkPos, 3));
  linkGeo.setAttribute("aSeed", new THREE.Float32BufferAttribute(linkSeed, 1));
  linkGeo.setAttribute("aEnd", new THREE.Float32BufferAttribute(linkEnd, 1));
  const linkMat = new THREE.ShaderMaterial({
    uniforms, vertexShader: LINK_VERT, fragmentShader: LINK_FRAG,
    transparent: true, depthWrite: false,
  });
  field.add(new THREE.LineSegments(linkGeo, linkMat));

  // --- theme ---
  function applyTheme() {
    const dark = document.documentElement.dataset.theme !== "light";
    uniforms.uNode.value.set(css("--accent-ink") || "#9fe03a");
    uniforms.uAlarm.value.set(css("--exhibit") || "#f5b41e");
    uniforms.uLink.value.set(dark ? "#5d7bb8" : "#7d8bb0");
    uniforms.uPacket.value.set(css("--accent") || "#76b900");
    uniforms.uAlpha.value = dark ? 1 : 0.75;
    for (const mat of [nodeMat, linkMat]) {
      mat.blending = dark ? THREE.AdditiveBlending : THREE.NormalBlending;
      mat.needsUpdate = true;
    }
    if (reducedMotion) render();
  }
  addEventListener("hindsight:theme", applyTheme);
  applyTheme();

  // --- size ---
  function resize() {
    const w = innerWidth, h = innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    uniforms.uAspect.value = w / h;
    uniforms.uPixel.value = renderer.getPixelRatio();
    if (reducedMotion) render();
  }
  addEventListener("resize", resize);
  resize();

  // --- pointer, scroll and pulses ---
  const target = new THREE.Vector2(9, 9); // far off-screen until the pointer moves
  let scroll = 0;
  if (!reducedMotion) {
    addEventListener("pointermove", (e) => {
      target.set((e.clientX / innerWidth) * 2 - 1, -((e.clientY / innerHeight) * 2 - 1));
    }, { passive: true });
    document.addEventListener("mouseout", (e) => { if (!e.relatedTarget) target.set(9, 9); });
    addEventListener("scroll", () => { scroll = scrollY; }, { passive: true });
    addEventListener("hindsight:pulse", (e) => {
      uniforms.uPulse.value = Math.max(uniforms.uPulse.value, Number(e.detail?.strength) || 0.5);
    });
  }

  function render() { renderer.render(scene, camera); }

  // --- loop ---
  const clock = new THREE.Clock();
  let running = false;
  function frame() {
    if (!running) return;
    const t = clock.getElapsedTime();
    uniforms.uTime.value = t;
    uniforms.uMouse.value.lerp(target, 0.08);
    uniforms.uPulse.value *= 0.965;
    const lean = Math.abs(target.x) > 2 ? 0 : 1; // no lean while the pointer is away
    field.rotation.y += ((target.x * 0.22 * lean + Math.sin(t * 0.05) * 0.18) - field.rotation.y) * 0.04;
    field.rotation.x += ((-target.y * 0.12 * lean) - field.rotation.x) * 0.04;
    field.position.y += (scroll * 0.004 - field.position.y) * 0.06;
    render();
    requestAnimationFrame(frame);
  }
  function play() {
    if (running || reducedMotion || document.hidden) return;
    running = true;
    requestAnimationFrame(frame);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) running = false;
    else play();
  });

  render();
  canvas.classList.add("on");
  play();
}

main();
