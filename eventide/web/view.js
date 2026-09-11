export function el(tag, className = "", text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Inline stroke icons replace text glyphs (＋ ⋯ × …) so chrome buttons stay
// crisp at any zoom and screen-reader labels do the naming work instead.
const ICONS = {
  plus: '<path d="M8 3v10M3 8h10"/>',
  close: '<path d="M4 4l8 8M12 4l-8 8"/>',
  menu: '<path d="M3 5h10M3 8h10M3 11h10"/>',
  dots: '<circle cx="4" cy="8" r="1.1"/><circle cx="8" cy="8" r="1.1"/><circle cx="12" cy="8" r="1.1"/>',
  "chevron-left": '<path d="M9.5 4L5.5 8l4 4"/>',
  "chevron-down": '<path d="M4 6l4 4 4-4"/>',
  send: '<path d="M8 13V3M4 7l4-4 4 4"/>',
};
export function icon(name) {
  const node = el("span", `icon icon-${name}`);
  node.innerHTML = `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;
  return node;
}

export function button(text, action, className = "text-button") {
  const node = el("button", className, text);
  node.type = "button";
  node.onclick = action;
  return node;
}

// Keep unchanged DOM (including focus, text selection and disclosure state).
export function reconcile(parent, items, key, signature, build) {
  const existing = new Map([...parent.children].map((node) => [node.dataset.key, node]));
  items.forEach((item, index) => {
    const id = key(item), version = signature(item);
    let node = existing.get(id);
    existing.delete(id);
    if (!node || node._version !== version) {
      const open = new Set(node ? [...node.querySelectorAll("details[open]")].map((d) => d.dataset.disclosure) : []);
      const wasOpen = node?.tagName === "DETAILS" && node.open;
      const focused = node?.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
      const replacement = build(item);
      replacement.dataset.key = id;
      replacement._version = version;
      if (wasOpen && replacement.tagName === "DETAILS") replacement.open = true;
      replacement.querySelectorAll("details").forEach((d) => { if (open.has(d.dataset.disclosure)) d.open = true; });
      if (node) node.replaceWith(replacement);
      node = replacement;
      if (focused) [...node.querySelectorAll("[data-focus]")].find((n) => n.dataset.focus === focused)?.focus({preventScroll: true});
    }
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
  });
  for (const node of existing.values()) node.remove();
}

function inline(parent, text) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^\s)]+\))/g;
  let position = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(position, match.index)));
    const token = match[0];
    if (token.startsWith("`")) parent.append(el("code", "", token.slice(1, -1)));
    else if (token.startsWith("**")) parent.append(el("strong", "", token.slice(2, -2)));
    else {
      const [, label, href] = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      let safe = false;
      try { safe = ["http:", "https:", "mailto:"].includes(new URL(href, location.href).protocol); } catch { /* plain text */ }
      if (safe) { const a = el("a", "", label); a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer"; parent.append(a); }
      else parent.append(document.createTextNode(label));
    }
    position = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(position)));
}

// Deliberately small Markdown subset. No HTML parsing and no remote dependencies.
export function markdown(text) {
  const root = el("div", "prose");
  const lines = String(text || "").split(/\r?\n/);
  for (let i = 0; i < lines.length;) {
    const line = lines[i++];
    if (!line.trim()) continue;
    if (/^\s*```/.test(line)) {
      const code = [];
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++;
      const pre = el("pre"); pre.append(el("code", "", code.join("\n"))); root.append(pre); continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)/);
    if (heading) { const h = el(`h${Math.min(heading[1].length + 2, 6)}`); inline(h, heading[2]); root.append(h); continue; }
    if (/^\s*(?:[-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const list = el(ordered ? "ol" : "ul");
      let entry = line;
      do {
        const li = el("li"); inline(li, entry.replace(/^\s*(?:[-*]|\d+\.)\s+/, "")); list.append(li);
        if (i >= lines.length || !(ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/).test(lines[i])) break;
        entry = lines[i++];
      } while (true);
      root.append(list); continue;
    }
    const p = el("p"); inline(p, line); root.append(p);
  }
  return root;
}
