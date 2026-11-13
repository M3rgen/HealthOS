// The viewer's used subset of three.js contracts. Type-only; no library code or dependency change.
export interface Vector2 { x: number; y: number; clone(): Vector2; copy(v: Vector2): this; multiplyScalar(n: number): this }
export interface Vector3 {
  x: number; y: number; z: number;
  set(x: number, y: number, z: number): this; setScalar(n: number): this;
}
export interface Color { copy(color: Color): this; set(value: number | string): this }
export interface Texture {
  isTexture: true; wrapS: number; wrapT: number; colorSpace: string; anisotropy: number; dispose(): void;
}
export interface Shader { uniforms: Record<string, {value: unknown}>; fragmentShader: string }
export interface Material {
  opacity: number; map: Texture | null; normalMap: Texture | null; roughnessMap: Texture | null;
  normalScale: Vector2; color: Color; roughness: number; defines: Record<string, string>;
  specularIntensity: number; sheen: number; sheenRoughness: number; sheenColor: Color; needsUpdate: boolean;
  onBeforeCompile(shader: Shader): void; customProgramCacheKey(): string; dispose(): void;
}
export interface Attribute { count: number }
export interface Geometry {
  attributes: Record<string, Attribute>; morphAttributes: Record<string, Attribute[]>;
  morphTargetsRelative: boolean; dispose(): void;
}
export interface Object3D {
  position: Vector3; scale: Vector3; rotation: {x: number; y: number; z: number};
  matrix: unknown; matrixWorld: unknown; visible: boolean; userData: Record<string, unknown>;
  traverse(callback: (node: ModelNode) => void): void;
  add(...nodes: Object3D[]): void; updateMatrixWorld(force: boolean): void; updateMatrix(): void;
}
export interface Mesh extends Object3D {
  isMesh: true; geometry: Geometry; material: Material | Material[];
  castShadow: boolean; receiveShadow: boolean;
  morphTargetDictionary?: Record<string, number>; morphTargetInfluences: number[];
}
export type ModelNode = (Object3D & {isMesh?: false}) | Mesh;
export interface Decal extends Object3D { material: Material }
export interface Camera extends Object3D {
  aspect: number; lookAt(x: number, y: number, z: number): void; updateProjectionMatrix(): void;
}
export interface Scene extends Object3D { environment: Texture; environmentIntensity: number }
export interface Renderer {
  domElement: HTMLCanvasElement; outputColorSpace: string; toneMapping: number; toneMappingExposure: number;
  shadowMap: {enabled: boolean; type: number}; capabilities: {getMaxAnisotropy(): number};
  setPixelRatio(ratio: number): void; setClearColor(color: number, alpha: number): void;
  setSize(w: number, h: number, updateStyle: boolean): void;
  setAnimationLoop(callback: ((timestamp: number) => void) | null): void;
  render(scene: Scene, camera: Camera): void; forceContextLoss(): void; dispose(): void;
}
export interface Stage {
  scene: Scene; camera: Camera; ground: Object3D; contacts: Decal[]; pedestal: Decal;
  turntable: Object3D; figure: Object3D; normalizer: Object3D;
  renewEnvironment(): void; disposeEnvironment(): void;
}
export type SkinUniforms = Record<string, {value: number | Color | Vector3 | Texture | null}>;
export interface HealthosExtras {
  skinToneHex?: string;
  suggestedNormalScale?: {formula?: unknown; max?: unknown; min?: unknown; exponent?: unknown; fatLowGain?: unknown};
}
