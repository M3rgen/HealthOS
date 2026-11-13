// Dragging the Umay bubble with a pointer. A press without movement stays a click.
const DRAG_THRESHOLD = 6;

/**
 * @param {HTMLElement} launcher
 * @param {{box: () => {left: number, top: number}, onMove: (left: number, top: number) => void}} callbacks
 */
export function setupLauncherDrag(launcher, { box, onMove }) {
  /** @type {{id: number, x: number, y: number, left: number, top: number, moved: boolean} | null} */
  let drag = null;
  let suppressClick = false, moved = false;

  /** @param {PointerEvent} event */
  function start(event) {
    if (!event.isPrimary || event.button !== 0) return;
    const { left, top } = box();
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, left, top, moved: false };
    suppressClick = false;
    launcher.setPointerCapture(event.pointerId);
  }
  /** @param {PointerEvent} event */
  function move(event) {
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    drag.moved = true;
    moved = true;
    launcher.classList.add('is-dragging');
    onMove(drag.left + dx, drag.top + dy);
  }
  /** @param {PointerEvent} event */
  function finish(event) {
    if (!drag || drag.id !== event.pointerId) return;
    suppressClick = event.type === 'pointerup' && drag.moved;
    drag = null;
    launcher.classList.remove('is-dragging');
    if (launcher.hasPointerCapture(event.pointerId)) launcher.releasePointerCapture(event.pointerId);
  }

  launcher.addEventListener('pointerdown', start);
  launcher.addEventListener('pointermove', move);
  launcher.addEventListener('pointerup', finish);
  launcher.addEventListener('pointercancel', finish);
  launcher.addEventListener('lostpointercapture', finish);

  return {
    /** True once a drag has moved the bubble from its stylesheet position. */
    hasMoved: () => moved,
    markMoved() { moved = true; },
    /** True once when this click only ended a drag. A keyboard click (detail 0) never counts. @param {MouseEvent} event */
    consumeClick(event) {
      const drop = suppressClick && event.detail !== 0;
      if (drop) suppressClick = false;
      return drop;
    },
  };
}
