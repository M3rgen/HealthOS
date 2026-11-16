// Vite's `?url` imports: the built file's public URL (here the MapLibre worker, served from /assets).
declare module '*?url' {
  const url: string;
  export default url;
}
