// Runs in the browser. Range rectangles cover every rendered text node, including nested labels.
function auditText() {
  const exceptions = [
    { selector: '.sr-only, .skip-link:not(:focus)', reason: 'Accessible text intentionally outside the visual layout.' },
    { selector: '#export-button > span:last-child, .brand-wordmark, .history-months label', clippedOnly: true, reason: 'Compact controls retain their accessible names while displaying icons or abbreviated dates.' },
    { selector: '.page > .section-title > div', clippedOnly: true, reason: 'At short heights the active navigation names the page; its heading remains screen-reader-only.' },
    { selector: 'option, script, style', reason: 'Native option popups and non-rendered source are not page text.' },
  ];
  const boxes = [], overflow = [], overlap = [];
  // A sticky bar with an opaque background (e.g. the connection card's footer) paints over the
  // content that scrolls beneath it inside the same scroller; that text is covered, not colliding.
  const stickyCover = el => {
    for (let p = el; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (s.position === 'sticky' && (s.backgroundImage !== 'none' || !/rgba|transparent|\/ *0?\.?\d*\)$/.test(s.backgroundColor))) return p;
    }
    return null;
  };
  const scroller = el => { for (let p = el.parentElement; p; p = p.parentElement) if (/auto|scroll/.test(getComputedStyle(p).overflowY)) return p; return null; };
  const underCover = (top, other, x) => {
    if (!top.cover || top.cover.contains(other.node) || !scroller(top.cover)?.contains(other.node)) return false;
    const q = top.cover.getBoundingClientRect();
    return x.l >= q.left - 1 && x.r <= q.right + 1 && x.t >= q.top - 1 && x.b <= q.bottom + 1;
  };
  const name = el => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : `.${String(el.className).split(/\s+/)[0]}`}`;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, el = node.parentElement;
    if (!node.data.trim() || exceptions.some(({ selector, clippedOnly }) => {
      const match = el.closest(selector);
      return match && (!clippedOnly || (getComputedStyle(match).clip === 'rect(0px, 0px, 0px, 0px)' || getComputedStyle(match).clipPath === 'inset(50%)'));
    })) continue;
    const closed = el.closest('details:not([open])');
    if (closed && !closed.querySelector(':scope > summary')?.contains(el)) continue;
    if (el.closest('[hidden], dialog:not([open]), [popover]:not(:popover-open)')) continue;
    const style = getComputedStyle(el);
    if (style.visibility !== 'visible' || style.display === 'none') continue;
    const range = document.createRange();
    range.selectNode(node);
    const label = `${name(el)}: ${node.data.trim().slice(0, 70)}`;
    for (const rect of range.getClientRects()) {
      if (rect.width < 0.1 || rect.height < 0.1) continue;
      let l = Math.max(0, rect.left), r = Math.min(innerWidth, rect.right);
      let t = Math.max(0, rect.top), b = Math.min(innerHeight, rect.bottom);
      const content = { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
      for (let p = el; p; p = p.parentElement) {
        const s = getComputedStyle(p), q = p.getBoundingClientRect();
        if (s.opacity === '0') { r = l; break; }
        for (const [axis, start, end, client, scroll, offset] of [
          ['X', 'left', 'right', 'clientWidth', 'scrollWidth', 'scrollLeft'],
          ['Y', 'top', 'bottom', 'clientHeight', 'scrollHeight', 'scrollTop'],
        ]) {
          if (!/auto|scroll|hidden|clip/.test(s[`overflow${axis}`])) continue;
          const border = axis === 'X' ? p.clientLeft : p.clientTop;
          const lo = q[start] + border, hi = lo + p[client];
          // Scrollable content is allowed beyond the visible window, but must fit its content
          // extent. hidden/clip never excuses losing text (ellipsis is dealt with explicitly).
          const scrollable = /auto|scroll/.test(s[`overflow${axis}`]);
          const contentLo = lo - (scrollable ? p[offset] : 0);
          const contentHi = contentLo + (scrollable ? p[scroll] : p[client]);
          if (content[start] < contentLo - 1 || content[end] > contentHi + 1)
            overflow.push(`${label} exceeds ${name(p)} ${axis} by ${Math.max(contentLo - content[start], content[end] - contentHi).toFixed(1)}px`);
          content[start] = Math.max(content[start], lo); content[end] = Math.min(content[end], hi);
          if (axis === 'X') { l = Math.max(l, lo); r = Math.min(r, hi); }
          else { t = Math.max(t, lo); b = Math.min(b, hi); }
        }
      }
      if (r > l && b > t) boxes.push({ node, label, l, r, t, b, cover: stickyCover(el) });
    }
  }
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    if (a.node === b.node) continue; // Multiple fragments of the same text are not two labels.
    const x = Math.min(a.r, b.r) - Math.max(a.l, b.l), y = Math.min(a.b, b.b) - Math.max(a.t, b.t);
    const area = { l: Math.max(a.l, b.l), r: Math.min(a.r, b.r), t: Math.max(a.t, b.t), b: Math.min(a.b, b.b) };
    if (underCover(a, b, area) || underCover(b, a, area)) continue;
    if (x > 1 && y > 1) overlap.push(`${a.label} / ${b.label} (${x.toFixed(1)}×${y.toFixed(1)})`);
  }
  return { nodes: new Set(boxes.map(b => b.node)).size, overflow: [...new Set(overflow)], overlap };
}
module.exports = { auditText };
