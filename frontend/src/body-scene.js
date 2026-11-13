// The 3D stage of the body viewer: environment light, fixed lights, the ground decals and the
// turntable → figure → normalizer hierarchy the model hangs from. Nothing here knows the model.
import {
  AdditiveBlending, CanvasTexture, CircleGeometry, DirectionalLight, Group, HemisphereLight, Mesh, MeshBasicMaterial,
  NormalBlending, PerspectiveCamera, PlaneGeometry, PMREMGenerator, Scene, ShadowMaterial, SRGBColorSpace, TextureLoader,
} from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const PEDESTAL_URL = '/assets/3d/pedestal-glow.webp';
export const FOV = 20;
export const PEDESTAL_WIDTH = 1.6;

/** @param {[number, string][]} stops */
function radialTexture(stops) {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 128;
  const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext('2d'));
  const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  for (const [offset, color] of stops) gradient.addColorStop(offset, color);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

/** @param {import('./body-types').Texture} texture three.js Texture @param {number} opacity @param {number} blending a three.js blending constant */
function groundDecal(texture, opacity, blending) {
  const mesh = new Mesh(new PlaneGeometry(1, 1), new MeshBasicMaterial({
    map: texture, transparent: true, opacity, depthWrite: false, blending, toneMapped: false,
  }));
  mesh.rotation.x = -Math.PI / 2;
  mesh.renderOrder = 1;
  return mesh;
}

// The render target behind the reflections lives only in the GL context, so a restored context
// needs a fresh one (buildEnvironment); every other texture still has its source image.
/** @param {import('./body-types').Renderer} renderer three.js WebGLRenderer */
function buildEnvironment(renderer) {
  const pmrem = new PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  const environment = pmrem.fromScene(room, .04).texture;
  room.dispose();
  pmrem.dispose();
  return environment;
}

/** @param {import('./body-types').Renderer} renderer three.js WebGLRenderer @param {{onPedestalLoaded: () => void}} options @returns {import('./body-types').Stage} */
export function createStage(renderer, { onPedestalLoaded }) {
  const scene = new Scene();
  let envMap = buildEnvironment(renderer);
  scene.environment = envMap;
  scene.environmentIntensity = .14;

  // Lights are fixed in world space; the figure turns beneath them.
  const camera = new PerspectiveCamera(FOV, 1, .1, 60);
  scene.add(new HemisphereLight(0xeee9df, 0x221f1a, .12));
  const key = new DirectionalLight(0xfff5ea, 2.9);
  key.position.set(-2.6, 4.4, 3.2);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  Object.assign(key.shadow.camera, { left: -1.4, right: 1.4, top: 1.6, bottom: -1.6, near: .5, far: 12 });
  // VSM radius 5: a wider blur bleeds light into blocky steps on the shoulders.
  key.shadow.radius = 5; key.shadow.blurSamples = 12; key.shadow.bias = -.0005; key.shadow.normalBias = .03;
  key.target.position.set(0, .9, 0);
  scene.add(key, key.target);
  const fill = new DirectionalLight(0xd9e0ea, .32);
  fill.position.set(3, 1.4, 2.6);
  // Rim lights are warm but desaturated so skin never turns orange; the UI amber (#f0c478) is
  // carried only by the thin Fresnel accent in the skin shader.
  const rim = new DirectionalLight(0xf4dcba, 3.6);
  rim.position.set(2.8, 2.6, -3.2);
  const rimLeft = new DirectionalLight(0xf4dcba, 1.6);
  rimLeft.position.set(-3.2, 2, -2.4);
  scene.add(fill, rim, rimLeft);

  // Ground: the amber pedestal glow and the key light's cast shadow stay put; one soft contact
  // shadow per foot turns with the figure (placed from the model's foot vertices).
  const ground = new Group();
  const contactTexture = radialTexture([[0, 'rgba(0,0,0,.7)'], [.5, 'rgba(0,0,0,.32)'], [1, 'rgba(0,0,0,0)']]);
  const contacts = [0, 1].map(() => {
    const decal = groundDecal(contactTexture, 1, NormalBlending);
    decal.position.y = .002;
    decal.visible = false;
    return decal;
  });
  // The pedestal image is already drawn as a screen-space ellipse (2:1 canvas), so its ground depth
  // is stretched by the camera fit to cancel the camera's foreshortening and keep that 2:1 on screen.
  const pedestalTexture = new TextureLoader().load(PEDESTAL_URL, onPedestalLoaded);
  pedestalTexture.colorSpace = SRGBColorSpace;
  const pedestal = groundDecal(pedestalTexture, .6, AdditiveBlending);
  pedestal.position.y = .001;
  const castShadow = new Mesh(new CircleGeometry(1.1, 48), new ShadowMaterial({ opacity: .24, depthWrite: false }));
  castShadow.rotation.x = -Math.PI / 2;
  castShadow.position.y = .003;
  castShadow.receiveShadow = true;
  ground.add(pedestal, castShadow);
  scene.add(ground);

  // turntable (rotation) → figure (height scale) → normalizer (fit source to contract) → model
  const turntable = new Group();
  const figure = new Group();
  const normalizer = new Group();
  figure.add(normalizer, ...contacts);
  turntable.add(figure);
  scene.add(turntable);

  return {
    scene, camera, ground, contacts, pedestal, turntable, figure, normalizer,
    renewEnvironment() {
      // The old target died with its context; disposing it would touch a GL object that is gone.
      envMap = buildEnvironment(renderer);
      scene.environment = envMap;
    },
    disposeEnvironment: () => envMap.dispose(),
  };
}
