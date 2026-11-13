// Removes `open`...`close` comments from a file of this repository before it is scanned; the text
// is never written out or rendered. An unterminated comment is left in place.
/** @param {string} text @param {string} open @param {string} close */
export function stripComments(text, open, close) {
  let out = '', at = 0;
  for (;;) {
    const start = text.indexOf(open, at);
    const end = start < 0 ? -1 : text.indexOf(close, start + open.length);
    if (end < 0) return out + text.slice(at);
    out += text.slice(at, start);
    at = end + close.length;
  }
}
