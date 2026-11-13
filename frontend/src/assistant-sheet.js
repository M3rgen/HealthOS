// On phones Umay opens as a full-screen sheet (chat-assistant.css). The on-screen keyboard shrinks
// the visual viewport without resizing the layout viewport, so the sheet follows the visual one.
const root = document.documentElement.style;

function update() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  root.setProperty('--assistant-sheet-top', `${viewport.offsetTop}px`);
  root.setProperty('--assistant-sheet-height', `${viewport.height}px`);
}

update();
window.visualViewport?.addEventListener('resize', update);
window.visualViewport?.addEventListener('scroll', update);
