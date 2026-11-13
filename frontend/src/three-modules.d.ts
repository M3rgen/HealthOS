// three.js ships no type declarations and @types/three is not installed (adding it is a dependency
// decision). These modules are `any` on purpose; the code around them says so at each parameter.
declare module 'three';
declare module 'three/addons/environments/RoomEnvironment.js';
declare module 'three/addons/loaders/GLTFLoader.js';
declare module 'three/addons/libs/meshopt_decoder.module.js';
