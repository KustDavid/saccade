// ui.js — small shared pieces: persisted settings, theme, toasts, icons, DOM helpers.

import { clampWpm } from './core.js';

// ---------- settings (localStorage, failure-tolerant) ----------

const SETTINGS_KEY = 'pace.settings.v1';

export const DEFAULTS = Object.freeze({
  wpm: 350,
  chunk: 1,                 // words per flash
  font: 'serif',            // stage font: serif | sans | mono
  size: 'm',                // stage size: s | m | l
  context: 'hide',          // paragraph while playing: hide | dim
  assetFlow: 'brief',       // figure blocks in the flow: brief | pause
  autoOpenFigures: false,   // pause + open each figure at its first mention
  includeSkipped: false,    // read References / Appendix too
  smartRewind: true,        // after a long pause, restart the sentence
  headingPause: 'all',      // stop at each heading until Space/click: all | main | off
  mode: 'rsvp',             // rsvp | text
  theme: 'auto',            // auto | light | dark
  figsCols: 'figure',       // side panel columns shown: any of figure,table,note
  figsWidth: 400,           // side panel width per column, px
});

function loadSettings() {
  try {
    const raw = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
    const out = { ...DEFAULTS };
    for (const k of Object.keys(DEFAULTS)) if (raw && k in raw && typeof raw[k] === typeof DEFAULTS[k]) out[k] = raw[k];
    out.wpm = clampWpm(out.wpm);
    out.chunk = Math.min(3, Math.max(1, out.chunk | 0));
    return out;
  } catch {
    return { ...DEFAULTS };
  }
}

export const settings = loadSettings();
const listeners = new Set();

export function setSetting(key, value) {
  if (settings[key] === value) return;
  settings[key] = value;
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings)); } catch { /* storage unavailable */ }
  for (const fn of listeners) fn(key, value);
}

export function onSettingChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// ---------- theme & fonts ----------

const darkQuery = matchMedia('(prefers-color-scheme: dark)');

export function effectiveTheme() {
  if (settings.theme === 'light' || settings.theme === 'dark') return settings.theme;
  return darkQuery.matches ? 'dark' : 'light';
}

export function applyAppearance() {
  const root = document.documentElement;
  if (settings.theme === 'auto') delete root.dataset.theme; else root.dataset.theme = settings.theme;
  root.dataset.stageFont = settings.font;
  root.dataset.stageSize = settings.size;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = effectiveTheme() === 'dark' ? '#141518' : '#f6f3ec';
}

export function toggleTheme() {
  setSetting('theme', effectiveTheme() === 'dark' ? 'light' : 'dark');
}

darkQuery.addEventListener?.('change', applyAppearance);
onSettingChange((k) => { if (k === 'theme' || k === 'font' || k === 'size') applyAppearance(); });

// ---------- DOM helpers ----------

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function isTyping(target) {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) && target.type !== 'range' && target.type !== 'checkbox');
}

// ---------- toasts ----------

export function toast(message, { timeout = 2600, kind = '' } = {}) {
  const host = document.getElementById('toasts');
  if (!host) return;
  const el = h('div', { class: `toast ${kind}`, role: 'status' }, message);
  host.append(el);
  requestAnimationFrame(() => el.classList.add('in'));
  setTimeout(() => {
    el.classList.remove('in');
    setTimeout(() => el.remove(), 300);
  }, timeout);
}

// ---------- icons (inline SVG, stroke = currentColor) ----------

const P = {
  play: '<path d="M7 4.5v15l12.5-7.5z" fill="currentColor" stroke="none"/>',
  pause: '<rect x="6" y="4.5" width="4" height="15" rx="1" fill="currentColor" stroke="none"/><rect x="14" y="4.5" width="4" height="15" rx="1" fill="currentColor" stroke="none"/>',
  back: '<path d="M15 18l-6-6 6-6"/>',
  prevSent: '<path d="M11 17l-5-5 5-5"/><path d="M18 17l-5-5 5-5"/>',
  nextSent: '<path d="M13 17l5-5-5-5"/><path d="M6 17l5-5-5-5"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1" fill="currentColor"/><circle cx="3.5" cy="12" r="1" fill="currentColor"/><circle cx="3.5" cy="18" r="1" fill="currentColor"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="M21 16l-5-5-9 9"/>',
  text: '<path d="M4 6h16M4 10h16M4 14h16M4 18h10"/>',
  bolt: '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 015 .5c0 1.7-2.5 2-2.5 3.5"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>',
  pdf: '<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V4h6v3"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/>',
  chevL: '<path d="M15 18l-6-6 6-6"/>',
  chevR: '<path d="M9 18l6-6-6-6"/>',
  zoomIn: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.5-4.5M8 11h6M11 8v6"/>',
  zoomOut: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.5-4.5M8 11h6"/>',
  fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  retry: '<path d="M20 11a8 8 0 10-2.3 5.7"/><path d="M20 4v7h-7"/>',
  restart: '<path d="M4 12a8 8 0 108-8 8 8 0 00-5.7 2.3L4 8.5"/><path d="M4 4v4.5h4.5"/>',
};

export function icon(name, size = 20) {
  return `<svg class="icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ''}</svg>`;
}

export function authorsLine(authors, max = 3) {
  const a = (authors || []).filter(Boolean);
  if (!a.length) return '';
  if (a.length <= max) return a.length === 2 ? a.join(' and ') : a.join(', ');
  return `${a.slice(0, max).join(', ')} et al.`;
}
