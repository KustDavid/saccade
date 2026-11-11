// core.js — the pure reading logic: tokenizing, pivot letters, timing,
// sentence boundaries, skip rules, position <-> percent, and the playback clock.
// No DOM access in here, so it runs (and is tested) under plain Node.

export const WPM_MIN = 100;
export const WPM_MAX = 1000;
export const WPM_STEP = 25;

// Dwell is expressed in "word units": 1 unit = one plain word at the nominal pace.
// The paper's average unit cost is normalised away (see meanFactor), so a
// 350 wpm setting really averages 350 words a minute; punctuation, numbers and
// long words simply take a larger share of that time than "the" or "of".
export const TIMING = Object.freeze({
  sentence: 1.3,       // after . ? !
  semicolon: 0.8,      // after ; :
  comma: 0.45,         // after ,
  dash: 0.45,          // after — –
  number: 0.35,        // token contains a digit
  longFrom: 7,         // letters before a word counts as long
  longPer: 0.08,       // extra units per letter beyond longFrom
  longMax: 0.9,
  paragraph: 1.6,      // last word of a paragraph
  headingBase: 2.2,    // headings are shown whole, as a title card
  headingPerWord: 1.1,
  headingFixedMs: 350,
  assetMs: 2600,       // figure card shown briefly in the flow
  equationMs: 2500,    // used for time estimates (equations hold in RSVP)
  rampStart: 2.0,      // first frame after resuming is shown this many times longer
  rampFrames: 8,       // ...easing back to normal speed over this many frames
  maxChunkChars: 24,   // multi-word flashes never exceed this many characters
  inlineImage: 1.5,    // a word that is a picture (inline math from a web book)
});

const ALNUM = /[\p{L}\p{N}]/u;
const TRAILING_CLOSERS = /["'”’»)\]}*†‡]+$/u;
const LEADING_OPENERS = /^["'“‘«(\[{]+/u;

// Never a sentence end (unless the paragraph ends there).
const ABBREV_NEVER = new Set([
  'e.g.', 'i.e.', 'cf.', 'vs.', 'viz.', 'fig.', 'figs.', 'eq.', 'eqs.', 'eqn.', 'no.', 'nos.',
  'sec.', 'secs.', 'ch.', 'dr.', 'mr.', 'mrs.', 'ms.', 'prof.', 'st.', 'approx.', 'resp.',
  'p.', 'pp.', 'vol.', 'ed.', 'eds.', 'tab.', 'app.', 'thm.', 'prop.', 'def.', 'cor.', 'lem.',
  'assn.', 'jr.', 'sr.', 'ref.', 'refs.', 'est.', 'col.', 'cols.', 'chap.', 'para.', 'w.r.t.',
]);
// A sentence end only when the next word starts with a capital letter.
const ABBREV_MAYBE = new Set(['etc.', 'al.', 'inc.', 'ltd.', 'co.', 'corp.', 'u.s.', 'u.k.', 'e.u.']);

// Paragraph text marks an inline picture as \uE000<key>\uE001; block.images[key] describes it.
const INLINE_IMAGE = /\uE000([^\uE000\uE001]*)\uE001/;

const EM_DASH_JOIN = /(?<=[\p{L}\p{N})\]”’"'.,]—)(?=[\p{L}\p{N}(\[“‘"'])/u;

export function tokenize(text) {
  // Whitespace-split, empties dropped. Words joined by an em dash ("realizations—is") are
  // shown one at a time: the dash stays with the first word, where it earns a clause pause.
  return String(text ?? '').split(/\s+/).filter(Boolean).flatMap((t) => (t.includes('—') ? t.split(EM_DASH_JOIN) : [t]));
}

export function clampWpm(wpm) {
  const n = Math.round(Number(wpm));
  if (!Number.isFinite(n)) return 350;
  return Math.min(WPM_MAX, Math.max(WPM_MIN, n));
}

/**
 * Optimal recognition point: index (in code points) of the letter the eye should
 * fixate. Leading punctuation is skipped; multi-word chunks pivot ~30% in.
 */
export function orpIndex(text) {
  const chars = Array.from(String(text ?? ''));
  if (!chars.length) return 0;
  let lead = 0;
  while (lead < chars.length && !ALNUM.test(chars[lead])) lead++;
  if (lead === chars.length) return Math.floor((chars.length - 1) / 2);
  let tail = chars.length;
  while (tail > lead && !ALNUM.test(chars[tail - 1])) tail--;
  const len = tail - lead;
  if (chars.slice(lead, tail).includes(' ')) {
    let i = lead + Math.round(len * 0.3);
    while (i < tail && !ALNUM.test(chars[i])) i++;
    if (i >= tail) i = lead;
    return i;
  }
  return lead + Math.min(4, Math.floor((len + 2) / 4));
}

/** Split a frame's text into [before, pivot, after] around the ORP. */
export function splitAtOrp(text) {
  const chars = Array.from(String(text ?? ''));
  const i = orpIndex(text);
  return [chars.slice(0, i).join(''), chars[i] ?? '', chars.slice(i + 1).join('')];
}

function stripLeading(token) {
  return String(token ?? '').replace(LEADING_OPENERS, '');
}

/** Does `token` end a sentence, given the following token in the same paragraph? */
export function isSentenceEnd(token, next) {
  if (!token) return false;
  const t = String(token).replace(TRAILING_CLOSERS, '');
  if (/[?!]$/.test(t)) return true;
  const nextStartsUpper = next !== undefined && /^\p{Lu}/u.test(stripLeading(next));
  if (/(\.\.\.|…)$/.test(t)) return next === undefined || nextStartsUpper;
  if (!t.endsWith('.')) return false;
  const lower = stripLeading(t).toLowerCase();
  if (ABBREV_NEVER.has(lower)) return false;
  if (/^\p{Lu}\.$/u.test(stripLeading(t))) return false; // an initial: "J."
  if (next === undefined) return true;
  if (ABBREV_MAYBE.has(lower) || /^(\p{L}\.){2,}$/u.test(lower)) return nextStartsUpper;
  if (/^\p{Ll}/u.test(stripLeading(next))) return false; // "...approx. ten", unknown abbreviations
  return true;
}

/** Mid-sentence punctuation that deserves a pause: 'semicolon' | 'comma' | 'dash' | null. */
export function clauseMark(token) {
  const t = String(token ?? '').replace(TRAILING_CLOSERS, '');
  if (/[;:]$/.test(t)) return 'semicolon';
  if (/,$/.test(t)) return 'comma';
  if (/[—–]$/.test(t) || /^[—–-]+$/.test(t)) return 'dash';
  return null;
}

/** Dwell for one word, in word units (before normalisation and ramp). */
export function wordFactor(token, ctx = {}) {
  const T = TIMING;
  const s = String(token ?? '');
  let f = 1;
  const core = s.replace(/[^\p{L}\p{N}]/gu, '');
  if (core.length > T.longFrom) f += Math.min(T.longMax, (core.length - T.longFrom) * T.longPer);
  if (/\p{N}/u.test(s)) f += T.number;
  const mark = ctx.sentenceEnd ? 'sentence' : clauseMark(s);
  if (mark) f += T[mark];
  if (ctx.paraEnd) f += T.paragraph;
  return f;
}

/** Slow-start multiplier for the k-th frame after resuming (k = 0 is the first). */
export function rampMultiplier(k, enabled = true) {
  if (!enabled || k >= TIMING.rampFrames) return 1;
  return 1 + (TIMING.rampStart - 1) * (1 - Math.max(0, k) / TIMING.rampFrames);
}

/**
 * Section ids skipped by default: any section flagged skip_default, plus its
 * subsections (following sections with a deeper level).
 */
export function skippedSectionIds(sections) {
  const out = new Set();
  let skipLevel = null;
  for (const s of sections || []) {
    const level = Number.isFinite(s?.level) ? s.level : 1;
    if (skipLevel !== null && level > skipLevel) { out.add(s.id); continue; }
    skipLevel = null;
    if (s?.skip_default) { out.add(s.id); skipLevel = level; }
  }
  return out;
}

/**
 * Locate in-text mentions ("Figure 2", "Figures 2 and 3", "Table 1,") of the given
 * assets. Returns [[wordIndex, assetId], ...] pointing at the number token.
 */
export function findMentions(tokens, assets) {
  const out = [];
  if (!assets?.length) return out;
  const found = new Set();
  let kind = null;
  let at = -100;
  tokens.forEach((tok, w) => {
    const bare = stripLeading(tok).replace(/[)\]"'”’.,;:]+$/u, '');
    if (/^(figures?|figs?)$/i.test(bare)) { kind = 'figure'; at = w; return; }
    if (/^(tables?|tabs?)$/i.test(bare)) { kind = 'table'; at = w; return; }
    if (!kind || w - at > 4) return;
    for (const a of assets) {
      if (found.has(a.id) || (a.kind || 'figure') !== kind || !a.number) continue;
      const num = String(a.number);
      if (bare === num || (bare.startsWith(num) && !/\d/.test(bare[num.length]))) {
        out.push([w, a.id]);
        found.add(a.id);
        at = w; // "Figures 2, 3 and 4": keep the window open along the list
        break;
      }
    }
  });
  // refs the text matcher couldn't place are pinned to the paragraph start
  for (const a of assets) if (!found.has(a.id)) out.push([0, a.id]);
  return out;
}

/**
 * Turn a Paper into a flat, playable stream of items.
 * Word items map 1:1 onto whitespace-split words of paragraph blocks; headings,
 * equations and assets are one item each (word index 0).
 */
export function buildStream(paper, opts = {}) {
  const blocks = Array.isArray(paper?.blocks) ? paper.blocks : [];
  const sections = Array.isArray(paper?.sections) ? paper.sections : [];
  const assets = new Map((paper?.assets || []).map((a) => [a.id, a]));
  const skippedIds = skippedSectionIds(sections);
  const excluded = opts.includeSkipped ? new Set() : skippedIds;
  const items = [];
  const blockStart = new Array(blocks.length).fill(-1);
  const blockLen = new Array(blocks.length).fill(0);
  const mentionIndex = {};

  blocks.forEach((blk, b) => {
    if (!blk || (blk.section != null && excluded.has(blk.section))) return;
    const start = items.length;
    const section = blk.section ?? null;
    const type = blk.type || 'paragraph';
    if (type === 'heading') {
      const words = tokenize(blk.text);
      if (!words.length) return;
      items.push({ kind: 'heading', b, w: 0, text: words.join(' '), words: words.length, section, level: blk.level || 1 });
    } else if (type === 'equation') {
      if (!blk.image && !String(blk.text || '').trim()) return;
      items.push({ kind: 'equation', b, w: 0, text: blk.text || '', words: 0, section });
    } else if (type === 'asset') {
      const a = assets.get(blk.asset);
      if (!a) return;
      items.push({ kind: 'asset', b, w: 0, text: a.label || a.id, asset: a.id, words: 0, section });
    } else {
      const words = tokenize(blk.text);
      if (!words.length) return;
      words.forEach((t, w) => {
        const item = { kind: 'word', b, w, text: t, words: 1, section };
        const m = INLINE_IMAGE.exec(t);
        if (m) {
          const info = blk.images?.[m[1]];
          const pre = t.slice(0, m.index);
          const post = t.slice(m.index + m[0].length);
          if (info) { item.img = { ...info, pre, post }; item.text = `${pre}[math]${post}`; }
          else item.text = pre + m[1] + post;
        }
        items.push(item);
      });
      const refs = [...new Set(blk.refs || [])].map((id) => assets.get(id)).filter(Boolean);
      for (const [w, id] of findMentions(words, refs)) {
        const it = items[start + w];
        (it.mentions ||= []).push(id);
        if (!(id in mentionIndex) || mentionIndex[id] > start + w) mentionIndex[id] = start + w;
      }
    }
    blockStart[b] = start;
    blockLen[b] = items.length - start;
  });

  const T = TIMING;
  const sentenceStarts = [];
  let sum = 0;
  let count = 0;
  items.forEach((it, i) => {
    const prev = items[i - 1];
    const next = items[i + 1];
    if (it.kind !== 'word') {
      it.sentStart = it.sentEnd = it.pause = true;
      if (it.kind === 'heading') { it.factor = T.headingBase + T.headingPerWord * it.words; it.fixedMs = T.headingFixedMs; }
      else { it.factor = 0; it.fixedMs = it.kind === 'asset' ? T.assetMs : T.equationMs; }
    } else {
      const nextInPara = next && next.kind === 'word' && next.b === it.b ? next : null;
      it.paraEnd = !nextInPara;
      it.sentStart = !prev || prev.kind !== 'word' || prev.b !== it.b || prev.sentEnd;
      it.sentEnd = it.paraEnd || isSentenceEnd(it.text, nextInPara?.text);
      it.pause = it.sentEnd || !!clauseMark(it.text);
      it.factor = wordFactor(it.text, { sentenceEnd: it.sentEnd, paraEnd: it.paraEnd }) + (it.img ? T.inlineImage : 0);
      it.fixedMs = 0;
      sum += it.factor;
      count++;
    }
    if (it.sentStart) sentenceStarts.push(i);
  });

  const n = items.length;
  const sufF = new Float64Array(n + 1);
  const sufX = new Float64Array(n + 1);
  for (let i = n - 1; i >= 0; i--) {
    sufF[i] = sufF[i + 1] + items[i].factor;
    sufX[i] = sufX[i + 1] + items[i].fixedMs;
  }

  return {
    items, blocks, sections, assets, blockStart, blockLen, sentenceStarts, mentionIndex,
    skippedIds, includeSkipped: !!opts.includeSkipped,
    meanFactor: count ? sum / count : 1, sufF, sufX,
  };
}

// ---------- timing ----------

/** Milliseconds per word unit at a given WPM (normalised so the average word = 60000/wpm). */
export function unitMs(stream, wpm) {
  return 60000 / clampWpm(wpm) / (stream.meanFactor || 1);
}

export function frameMs(stream, frame, wpm, ramp = 1) {
  const u = unitMs(stream, wpm);
  let f = 0;
  let x = 0;
  for (let i = frame.start; i < frame.end; i++) {
    f += stream.items[i].factor;
    x += stream.items[i].fixedMs;
  }
  return f * u * ramp + x;
}

/** Estimated reading time from item `from` (inclusive) to `to` (exclusive). */
export function rangeMs(stream, from, to, wpm) {
  const n = stream.items.length;
  const a = Math.max(0, Math.min(n, from));
  const b = Math.max(a, Math.min(n, to ?? n));
  return (stream.sufF[a] - stream.sufF[b]) * unitMs(stream, wpm) + (stream.sufX[a] - stream.sufX[b]);
}

export function remainingMs(stream, index, wpm) {
  return rangeMs(stream, index, stream.items.length, wpm);
}

/** Rough minutes for a paper summary (library view), from its word count. */
export function estimateMinutes(wordCount, wpm, assetCount = 0) {
  if (!wordCount) return 0;
  return wordCount / clampWpm(wpm) + (assetCount * TIMING.assetMs) / 60000;
}

export function formatDuration(ms) {
  const min = Math.round(ms / 60000);
  if (!Number.isFinite(min) || ms <= 0) return '0 min';
  if (min < 1) return '<1 min';
  if (min < 60) return `${min} min`;
  return `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, '0')} min`;
}

// ---------- frames (1–3 words per flash) ----------

export function frameAt(stream, index, chunk = 1) {
  const items = stream.items;
  const n = items.length;
  if (!n) return { start: 0, end: 0 };
  const start = Math.max(0, Math.min(n - 1, index | 0));
  const first = items[start];
  let end = start + 1;
  if (first.kind === 'word' && chunk > 1 && !first.img) {
    let len = Array.from(first.text).length;
    while (end < n && end - start < chunk) {
      const prev = items[end - 1];
      const it = items[end];
      if (it.kind !== 'word' || it.b !== first.b || prev.pause || it.sentStart || it.img) break;
      const add = Array.from(it.text).length + 1;
      if (len + add > TIMING.maxChunkChars) break;
      len += add;
      end++;
    }
  }
  return { start, end };
}

export function frameText(stream, frame) {
  return stream.items.slice(frame.start, frame.end).map((it) => it.text).join(' ');
}

// ---------- positions ----------

export function positionAt(stream, index) {
  const it = stream.items[Math.max(0, Math.min(stream.items.length - 1, index))];
  return it ? { block: it.b, word: it.w } : { block: 0, word: 0 };
}

/** First item index at or after block `b` (or the last item if none). */
export function indexAtOrAfterBlock(stream, b) {
  const items = stream.items;
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (items[mid].b < b) lo = mid + 1; else hi = mid;
  }
  return Math.min(lo, Math.max(0, items.length - 1));
}

/** Map a saved (block, word) position to an item index; excluded blocks snap forward. */
export function indexAt(stream, block, word = 0) {
  if (!stream.items.length) return 0;
  const b = Number.isFinite(+block) ? +block : 0;
  const start = stream.blockStart[b];
  if (start !== undefined && start >= 0) {
    const len = stream.blockLen[b];
    return start + Math.max(0, Math.min(len - 1, Number.isFinite(+word) ? +word : 0));
  }
  return indexAtOrAfterBlock(stream, b);
}

/** 0–100. */
export function percentAt(stream, index) {
  const n = stream.items.length;
  if (n <= 1) return 0;
  return Math.max(0, Math.min(100, (index / (n - 1)) * 100));
}

export function indexAtFraction(stream, fraction) {
  const n = stream.items.length;
  if (!n) return 0;
  const f = Math.max(0, Math.min(1, Number(fraction) || 0));
  return Math.round(f * (n - 1));
}

// ---------- navigation ----------

function lastAtOrBefore(sorted, x) {
  let lo = 0;
  let hi = sorted.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] <= x) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

export function sentenceStartAt(stream, index) {
  const k = lastAtOrBefore(stream.sentenceStarts, index);
  return k < 0 ? 0 : stream.sentenceStarts[k];
}

/** Back one sentence. Within the first `slack` words of a sentence, go to the previous one. */
export function prevSentenceIndex(stream, index, slack = 2) {
  const ss = stream.sentenceStarts;
  const k = lastAtOrBefore(ss, index);
  if (k < 0) return 0;
  if (index - ss[k] > slack) return ss[k];
  return k > 0 ? ss[k - 1] : 0;
}

export function nextSentenceIndex(stream, index) {
  const ss = stream.sentenceStarts;
  const k = lastAtOrBefore(ss, index);
  const next = ss[k + 1];
  return next === undefined ? index : next;
}

/** Outline rows: where each section starts in the stream and how long it takes. */
export function sectionRanges(stream) {
  const secs = stream.sections;
  const n = stream.items.length;
  const rows = secs.map((s, k) => {
    const nextFirst = secs[k + 1]?.first_block ?? Infinity;
    let start = -1;
    if (n) {
      const i = indexAtOrAfterBlock(stream, s.first_block ?? 0);
      const it = stream.items[i];
      if (it && it.b >= (s.first_block ?? 0) && it.b < nextFirst) start = i;
    }
    return { id: s.id, title: s.title, level: s.level || 1, skipped: stream.skippedIds.has(s.id), start };
  });
  rows.forEach((r, k) => {
    // a section's span runs to the next section at the same or a shallower level
    let end = n;
    for (let j = k + 1; j < rows.length; j++) {
      if (rows[j].level <= r.level && rows[j].start >= 0) { end = rows[j].start; break; }
    }
    r.end = r.start >= 0 ? Math.max(r.start, end) : -1;
  });
  return rows;
}

// ---------- playback clock ----------

export function systemClock() {
  return {
    now: () => performance.now(),
    set: (fn, ms) => setTimeout(fn, ms),
    clear: (id) => clearTimeout(id),
  };
}

/**
 * Drives the stream. Each frame's deadline is the previous deadline plus its
 * delay (not "now + delay"), so timer lateness never accumulates into drift.
 * If we fall more than a whole frame behind (sleep, throttled tab) we resync
 * rather than flashing a burst of words.
 *
 * onFrame(frame, cause) — cause is 'load' | 'seek' | 'play' | 'tick'.
 * onState(state)        — 'paused' | 'playing' | 'ended'.
 * isHold(item)          — items that stop playback once shown (equations, ...).
 */
export class Player {
  constructor({ clock = systemClock(), onFrame = () => {}, onState = () => {}, isHold = () => false } = {}) {
    this.clock = clock;
    this.onFrame = onFrame;
    this.onState = onState;
    this.isHold = isHold;
    this.stream = null;
    this.frame = null;
    this.state = 'paused';
    this.wpm = 350;
    this.chunk = 1;
    this.ramp = true;
    this.timer = null;
    this.nextDue = 0;
    this.sinceResume = 0;
  }

  get index() { return this.frame ? this.frame.start : 0; }
  get playing() { return this.state === 'playing'; }
  get item() { return this.stream?.items[this.index] ?? null; }

  load(stream, index = 0) {
    this._clear();
    this.stream = stream;
    this._setState('paused');
    this._show(index, 'load');
  }

  setWpm(wpm) { this.wpm = clampWpm(wpm); }

  setChunk(chunk) {
    this.chunk = Math.max(1, Math.min(3, chunk | 0 || 1));
    if (this.stream && !this.playing) this._show(this.index, 'seek');
  }

  /** Returns false if there is nothing to play (empty stream or already at the end). */
  play() {
    const n = this.stream?.items.length ?? 0;
    if (!n || this.playing) return false;
    let i = this.index;
    if (this.state === 'ended') return false;
    if (this.isHold(this.item)) {
      i = this.frame.end;
      if (i >= n) { this._setState('ended'); return false; }
    }
    this.sinceResume = 0;
    this._setState('playing');
    this._show(i, 'play');
    this._afterShow(this.clock.now(), true);
    return true;
  }

  pause() {
    if (!this.playing) return;
    this._clear();
    this._setState('paused');
  }

  toggle() { return this.playing ? (this.pause(), false) : this.play(); }

  seek(index, cause = 'seek') {
    if (!this.stream?.items.length) return;
    this._clear();
    if (this.state === 'ended') this._setState('paused');
    this._show(index, cause);
    if (this.playing) {
      this.sinceResume = Math.min(this.sinceResume, TIMING.rampFrames >> 1);
      this._afterShow(this.clock.now(), true);
    }
  }

  destroy() { this._clear(); this.onFrame = () => {}; this.onState = () => {}; }

  _show(index, cause) {
    const n = this.stream.items.length;
    const i = Math.max(0, Math.min(n - 1, index | 0));
    this.frame = frameAt(this.stream, i, this.chunk);
    this.onFrame(this.frame, cause);
  }

  _delay() {
    return frameMs(this.stream, this.frame, this.wpm, rampMultiplier(this.sinceResume, this.ramp));
  }

  // After a frame is on screen: hold, or schedule the next one.
  _afterShow(now, fresh) {
    if (!this.playing) return; // an onFrame handler paused us
    if (this.isHold(this.item)) { this._clear(); this._setState('paused'); return; }
    const d = this._delay();
    if (fresh) this.nextDue = now + d;
    else {
      this.nextDue += d;
      if (this.nextDue < now) this.nextDue = now + d; // fell a whole frame behind: resync
    }
    this.timer = this.clock.set(() => this._tick(), Math.max(0, this.nextDue - now));
  }

  _tick() {
    this.timer = null;
    if (!this.playing) return;
    const now = this.clock.now();
    if (now < this.nextDue - 1) { // woke early
      this.timer = this.clock.set(() => this._tick(), this.nextDue - now);
      return;
    }
    const next = this.frame.end;
    if (next >= this.stream.items.length) { this._setState('ended'); return; }
    this.sinceResume++;
    this._show(next, 'tick');
    this._afterShow(now, false);
  }

  _clear() {
    if (this.timer !== null) this.clock.clear(this.timer);
    this.timer = null;
  }

  _setState(s) {
    if (this.state === s) return;
    this.state = s;
    this.onState(s);
  }
}
