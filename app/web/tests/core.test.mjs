// Run with:  node --test web/tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  tokenize, orpIndex, splitAtOrp, isSentenceEnd, clauseMark, wordFactor, rampMultiplier,
  skippedSectionIds, findMentions, buildStream, unitMs, frameMs, rangeMs, remainingMs,
  frameAt, frameText, positionAt, indexAt, indexAtOrAfterBlock, percentAt, indexAtFraction,
  sentenceStartAt, prevSentenceIndex, nextSentenceIndex, sectionRanges, Player, TIMING,
  clampWpm, estimateMinutes, formatDuration,
} from '../js/core.js';

// ---------- fixtures ----------

function samplePaper() {
  return {
    id: 'p',
    sections: [
      { id: 's1', title: 'Abstract', level: 1, first_block: 0, skip_default: false },
      { id: 's2', title: '1 Introduction', level: 1, first_block: 2, skip_default: false },
      { id: 's3', title: '1.1 Setup', level: 2, first_block: 6, skip_default: false },
      { id: 's4', title: 'References', level: 1, first_block: 9, skip_default: true },
      { id: 's5', title: 'A.1 Extra', level: 2, first_block: 11, skip_default: false },
      { id: 's6', title: 'Appendix B', level: 1, first_block: 12, skip_default: false },
    ],
    blocks: [
      { type: 'heading', text: 'Abstract', section: 's1', page: 1, level: 1 },
      { type: 'paragraph', text: 'We study demand. Prices rise by 3.2%, e.g. in winter.', section: 's1', page: 1 },
      { type: 'heading', text: '1  Introduction', section: 's2', page: 1, level: 1 },
      { type: 'paragraph', text: 'As Figure 1 shows, the effect is large; see also Table 1.', section: 's2', page: 2, refs: ['fig-1', 'tab-1'] },
      { type: 'asset', asset: 'fig-1', section: 's2', page: 2 },
      { type: 'equation', text: '', image: 'eq-1.png', width: 900, height: 120, section: 's2', page: 2 },
      { type: 'heading', text: '1.1 Setup', section: 's3', page: 3, level: 2 },
      { type: 'paragraph', text: '  Smith et al. (2019) disagree.   Why?  ', section: 's3', page: 3 },
      { type: 'asset', asset: 'missing', section: 's3', page: 3 },
      { type: 'heading', text: 'References', section: 's4', page: 4, level: 1 },
      { type: 'paragraph', text: 'Berry, S. (1994). Estimating discrete-choice models.', section: 's4', page: 4 },
      { type: 'paragraph', text: 'Nested appendix text.', section: 's5', page: 4 },
      { type: 'paragraph', text: 'Back to normal.', section: 's6', page: 5 },
    ],
    assets: [
      { id: 'fig-1', kind: 'figure', number: '1', label: 'Figure 1', caption: 'c', page: 2, image: 'fig-1.png', first_ref_block: 3 },
      { id: 'tab-1', kind: 'table', number: '1', label: 'Table 1', caption: 'c', page: 2, image: 'tab-1.png', first_ref_block: 3 },
    ],
  };
}

function fakeClock() {
  let t = 0;
  let id = 0;
  const timers = new Map();
  return {
    now: () => t,
    set(fn, ms) { const k = ++id; timers.set(k, { fn, at: t + Math.max(0, ms) }); return k; },
    clear(k) { timers.delete(k); },
    advance(ms) { t += ms; },
    pending: () => timers.size,
    /** Fire the earliest timer `late` ms after it was due. */
    step(late = 0) {
      const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) return false;
      timers.delete(next[0]);
      t = Math.max(t, next[1].at + late);
      next[1].fn();
      return true;
    },
  };
}

function wordsPaper(nWords, text = null) {
  const words = text ? tokenize(text) : Array.from({ length: nWords }, (_, i) => `w${i % 10}x`);
  return { sections: [], assets: [], blocks: [{ type: 'paragraph', text: words.join(' '), section: null, page: 1 }] };
}

// ---------- tokenizing & ORP ----------

test('tokenize splits on any whitespace and drops empties', () => {
  assert.deepEqual(tokenize('  a  b\tc\nd  '), ['a', 'b', 'c', 'd']);
  assert.deepEqual(tokenize(''), []);
  assert.deepEqual(tokenize(null), []);
  // em dash joining two words: one word per flash, dash kept with the first
  assert.deepEqual(tokenize('past realizations—is only'), ['past', 'realizations—', 'is', 'only']);
  assert.deepEqual(tokenize('effects—as a (—what I call “3.0”—) step'), ['effects—', 'as', 'a', '(—what', 'I', 'call', '“3.0”—)', 'step']);
  assert.deepEqual(tokenize('1953–2024 well-known a — b x—“y”'), ['1953–2024', 'well-known', 'a', '—', 'b', 'x—', '“y”']);
});

test('orpIndex follows the classic length table', () => {
  const cases = { a: 0, of: 1, the: 1, model: 1, models: 2, estimate: 2, estimation: 3, identification: 4, 'heteroskedasticity-robust': 4 };
  for (const [w, i] of Object.entries(cases)) assert.equal(orpIndex(w), i, w);
});

test('orpIndex skips leading punctuation and handles odd tokens', () => {
  assert.equal(orpIndex('(Berry,'), 2); // "Berry" -> index 1, plus the "("
  assert.equal(orpIndex('“Hello”'), 2);
  assert.equal(orpIndex('—'), 0);
  assert.equal(orpIndex(''), 0);
  const chunk = 'of the model';
  const i = orpIndex(chunk);
  assert.notEqual(Array.from(chunk)[i], ' ');
  assert.ok(i > 0 && i < chunk.length / 2);
});

test('splitAtOrp reassembles to the original text', () => {
  for (const w of ['a', 'word', '(Berry,', 'heteroskedasticity-robust', 'of the model', 'naïve', 'α-test']) {
    const [pre, pivot, post] = splitAtOrp(w);
    assert.equal(pre + pivot + post, w);
    assert.equal(Array.from(pivot).length, 1);
  }
});

// ---------- sentence boundaries & dwell ----------

test('isSentenceEnd handles abbreviations, initials, numbers and closers', () => {
  const yes = [['demand.', 'Prices'], ['why?', 'We'], ['results.)', 'The'], ['(1995).', 'Second'], ['al.', 'The'],
    ['U.S.', 'The'], ['etc.', 'We'], ['3.', 'We'], ['end.', undefined], ['wow!', 'x'], ['so…', 'Then']];
  const no = [['e.g.', 'the'], ['e.g.', 'The'], ['i.e.,', 'a'], ['al.', '(2019)'], ['al.', 'find'], ['J.', 'Smith'],
    ['3.2', 'and'], ['approx.', 'ten'], ['U.S.', 'economy'], ['Fig.', '2'], ['value.', 'x'], ['word', 'Word'], ['cf.', 'Smith']];
  for (const [t, n] of yes) assert.equal(isSentenceEnd(t, n), true, `${t} ${n}`);
  for (const [t, n] of no) assert.equal(isSentenceEnd(t, n), false, `${t} ${n}`);
});

test('clauseMark classifies mid-sentence punctuation', () => {
  assert.equal(clauseMark('large;'), 'semicolon');
  assert.equal(clauseMark('follows:'), 'semicolon');
  assert.equal(clauseMark('(Berry,'), 'comma');
  assert.equal(clauseMark('quote,”'), 'comma');
  assert.equal(clauseMark('effect—'), 'dash');
  assert.equal(clauseMark('—'), 'dash');
  assert.equal(clauseMark('plain'), null);
});

test('wordFactor adds dwell for punctuation, numbers, long words and paragraph ends', () => {
  assert.equal(wordFactor('the'), 1);
  assert.ok(wordFactor('the,') > 1);
  assert.ok(wordFactor('the;') > wordFactor('the,'));
  assert.ok(wordFactor('end.', { sentenceEnd: true }) > wordFactor('the;'));
  assert.ok(wordFactor('end.', { sentenceEnd: true, paraEnd: true }) > wordFactor('end.', { sentenceEnd: true }));
  assert.ok(wordFactor('12,408') > 1);
  assert.ok(wordFactor('identification') > wordFactor('estimate'));
  assert.ok(wordFactor('heteroskedasticity-robust') <= 1 + TIMING.longMax + 1e-9);
});

test('rampMultiplier eases from rampStart down to 1', () => {
  assert.equal(rampMultiplier(0), TIMING.rampStart);
  assert.equal(rampMultiplier(TIMING.rampFrames), 1);
  assert.equal(rampMultiplier(0, false), 1);
  for (let k = 1; k <= TIMING.rampFrames; k++) assert.ok(rampMultiplier(k) <= rampMultiplier(k - 1));
});

test('clampWpm / estimateMinutes / formatDuration', () => {
  assert.equal(clampWpm(50), 100);
  assert.equal(clampWpm(5000), 1000);
  assert.equal(clampWpm('abc'), 350);
  assert.equal(estimateMinutes(3500, 350), 10);
  assert.equal(formatDuration(30 * 60000), '30 min');
  assert.equal(formatDuration(20000), '<1 min');
  assert.equal(formatDuration(125 * 60000), '2 h 05 min');
});

// ---------- skip logic ----------

test('skippedSectionIds covers flagged sections and their subsections only', () => {
  const s = skippedSectionIds(samplePaper().sections);
  assert.deepEqual([...s].sort(), ['s4', 's5']);
});

// ---------- mentions ----------

test('findMentions locates figure/table numbers, lists and fallbacks', () => {
  const assets = [
    { id: 'fig-2', kind: 'figure', number: '2' },
    { id: 'fig-3', kind: 'figure', number: '3' },
    { id: 'tab-1', kind: 'table', number: '1' },
    { id: 'fig-12', kind: 'figure', number: '12' },
  ];
  const toks = tokenize('As (Figure 2a) and Figures 3 and 12 show, Table 1, fits.');
  const m = Object.fromEntries(findMentions(toks, assets).map(([w, id]) => [id, w]));
  assert.equal(toks[m['fig-2']], '2a)');
  assert.equal(toks[m['fig-3']], '3');
  assert.equal(toks[m['fig-12']], '12');
  assert.equal(toks[m['tab-1']], '1,');
  const fallback = findMentions(tokenize('No mention here.'), [{ id: 'x', kind: 'figure', number: '9' }]);
  assert.deepEqual(fallback, [[0, 'x']]);
});

// ---------- stream ----------

test('buildStream maps blocks to items and honours skip_default', () => {
  const paper = samplePaper();
  const s = buildStream(paper);
  // references (s4) and its nested subsection (s5) are excluded; s6 is back in
  assert.equal(s.blockStart[9], -1);
  assert.equal(s.blockStart[10], -1);
  assert.equal(s.blockStart[11], -1);
  assert.ok(s.blockStart[12] >= 0);
  // headings are one item with normalised spacing
  const h = s.items[s.blockStart[2]];
  assert.equal(h.kind, 'heading');
  assert.equal(h.text, '1 Introduction');
  // paragraph words line up with whitespace-split indices
  const p = s.blockStart[7];
  assert.deepEqual(s.items.slice(p, p + s.blockLen[7]).map((i) => i.text), tokenize(paper.blocks[7].text));
  assert.equal(s.items[p + 3].w, 3);
  // asset & equation items exist; unknown asset ids are dropped
  assert.equal(s.items[s.blockStart[4]].kind, 'asset');
  assert.equal(s.items[s.blockStart[5]].kind, 'equation');
  assert.equal(s.blockStart[8], -1);
  // mentions land on the number tokens
  assert.equal(s.items[s.mentionIndex['fig-1']].text, '1');
  assert.equal(s.items[s.mentionIndex['tab-1']].text, '1.');

  const all = buildStream(paper, { includeSkipped: true });
  assert.ok(all.blockStart[10] >= 0 && all.blockStart[11] >= 0);
  assert.ok(all.items.length > s.items.length);
});

test('sentence starts respect abbreviations and block boundaries', () => {
  const s = buildStream(samplePaper());
  const starts = s.sentenceStarts.map((i) => s.items[i].text);
  assert.ok(starts.includes('Prices'));
  assert.ok(!starts.includes('in')); // "e.g. in winter" is one sentence
  assert.ok(starts.includes('Why?'));
  assert.ok(!starts.includes('(2019)')); // "et al. (2019)"
  for (const i of s.sentenceStarts) {
    const it = s.items[i];
    if (it.kind === 'word' && it.w === 0) continue;
    if (it.kind !== 'word') continue;
    assert.ok(s.items[i - 1].sentEnd, `sentence start ${it.text} follows a sentence end`);
  }
});

test('position <-> index round-trips for every item; excluded blocks snap forward', () => {
  const s = buildStream(samplePaper());
  s.items.forEach((it, i) => {
    const pos = positionAt(s, i);
    assert.equal(indexAt(s, pos.block, pos.word), i);
  });
  // a saved position inside skipped References resumes at the next readable block
  assert.equal(s.items[indexAt(s, 10, 3)].b, 12);
  // out-of-range word clamps inside the block
  assert.equal(indexAt(s, 1, 999), s.blockStart[1] + s.blockLen[1] - 1);
  assert.equal(indexAt(s, 999, 0), s.items.length - 1);
  assert.equal(indexAtOrAfterBlock(s, 0), 0);
});

test('percent <-> index mapping is monotone and invertible', () => {
  const s = buildStream(samplePaper());
  const n = s.items.length;
  assert.equal(percentAt(s, 0), 0);
  assert.equal(percentAt(s, n - 1), 100);
  let last = -1;
  for (let i = 0; i < n; i++) {
    const p = percentAt(s, i);
    assert.ok(p > last);
    last = p;
    assert.equal(indexAtFraction(s, p / 100), i);
  }
  assert.equal(indexAtFraction(s, -3), 0);
  assert.equal(indexAtFraction(s, 7), n - 1);
});

test('timing is normalised so the average word takes 60000/wpm', () => {
  const s = buildStream(wordsPaper(0, 'We estimate demand, carefully. Prices rose 3.2% in 2019; identification is subtle — and robust. Done.'.repeat(5)));
  const nWords = s.items.length;
  const total = rangeMs(s, 0, nWords, 300);
  assert.ok(Math.abs(total - (nWords * 60000) / 300) < 1e-6);
  assert.equal(remainingMs(s, 0, 300), total);
  assert.ok(Math.abs(unitMs(s, 300) * s.meanFactor - 200) < 1e-9);
  // a frame's delay grows with ramp
  const f = { start: 0, end: 1 };
  assert.ok(frameMs(s, f, 300, 2) > frameMs(s, f, 300, 1));
});

test('frameAt chunks words but never across punctuation, sentences or blocks', () => {
  const s = buildStream(wordsPaper(0, 'the big model works, and it is fast. A second sentence here'));
  const t = (i, c) => frameText(s, frameAt(s, i, c));
  assert.equal(t(0, 1), 'the');
  assert.equal(t(0, 3), 'the big model');
  assert.equal(t(3, 3), 'works,');            // comma ends the chunk
  assert.equal(t(6, 3), 'is fast.');          // sentence end ends the chunk
  assert.equal(t(8, 3), 'A second sentence');
  const long = buildStream(wordsPaper(0, 'heteroskedasticity-robust identification strategies'));
  assert.equal(frameAt(long, 0, 3).end, 1);   // exceeds maxChunkChars
  const p = buildStream(samplePaper());
  const lastOfAbstract = p.blockStart[1] + p.blockLen[1] - 1;
  assert.equal(frameAt(p, lastOfAbstract, 3).end, lastOfAbstract + 1);
  assert.equal(frameAt(p, p.blockStart[2], 3).end, p.blockStart[2] + 1); // heading is one frame
});

test('sentence navigation: back restarts the sentence, then steps to the previous one', () => {
  const s = buildStream(wordsPaper(0, 'One two three four. Five six seven eight. Nine ten.'));
  assert.equal(sentenceStartAt(s, 6), 4);
  assert.equal(prevSentenceIndex(s, 7), 4);   // deep in sentence 2 -> its start
  assert.equal(prevSentenceIndex(s, 5), 0);   // just started sentence 2 -> sentence 1
  assert.equal(prevSentenceIndex(s, 0), 0);
  assert.equal(nextSentenceIndex(s, 1), 4);
  assert.equal(nextSentenceIndex(s, 4), 8);
  assert.equal(nextSentenceIndex(s, 9), 9);   // nowhere to go
});

test('sectionRanges gives starts, spans and skip flags', () => {
  const s = buildStream(samplePaper());
  const rows = Object.fromEntries(sectionRanges(s).map((r) => [r.id, r]));
  assert.equal(rows.s1.start, 0);
  assert.equal(rows.s2.start, s.blockStart[2]);
  assert.equal(rows.s2.end, s.blockStart[12]);  // level-1 span includes subsection s3, skipped s4/s5 contribute nothing
  assert.equal(rows.s3.end, s.blockStart[12]);
  assert.equal(rows.s4.start, -1);
  assert.equal(rows.s4.skipped, true);
  assert.equal(rows.s5.skipped, true);
  assert.equal(rows.s6.end, s.items.length);
});

// ---------- player ----------

test('player schedules from deadlines, so late timers do not accumulate drift', () => {
  const s = buildStream(wordsPaper(200));
  const clock = fakeClock();
  const shown = [];
  const p = new Player({ clock, onFrame: (f, cause) => shown.push({ i: f.start, t: clock.now(), cause }) });
  p.ramp = false;
  p.setWpm(600);
  p.load(s, 0);
  p.play();
  const t0 = clock.now();
  while (p.playing && clock.step(7)) { /* every timer fires 7 ms late */ }
  assert.equal(p.state, 'ended');
  const ticks = shown.filter((x) => x.cause !== 'load');
  assert.equal(ticks.length, 200);
  let expected = t0;
  for (let k = 0; k < ticks.length; k++) {
    assert.ok(Math.abs(ticks[k].t - expected) <= 7 + 1e-6, `frame ${k} off by ${ticks[k].t - expected}`);
    expected += frameMs(s, { start: k, end: k + 1 }, 600);
  }
});

test('player resyncs instead of bursting after a long stall', () => {
  const s = buildStream(wordsPaper(50));
  const clock = fakeClock();
  const p = new Player({ clock });
  p.ramp = false;
  p.load(s, 0);
  p.play();
  clock.step(0);
  clock.advance(10000); // laptop slept
  clock.step(0);
  const idx = p.index;
  clock.step(0);
  assert.equal(p.index, idx + 1); // exactly one frame advanced per tick
  assert.ok(clock.pending() === 1);
});

test('player ramps up after resume and holds on hold items', () => {
  const s = buildStream(samplePaper());
  const clock = fakeClock();
  const states = [];
  const p = new Player({ clock, isHold: (it) => it?.kind === 'equation', onState: (st) => states.push(st) });
  p.load(s, s.blockStart[3]);
  p.play();
  const firstDelay = p.nextDue - clock.now();
  const plain = frameMs(s, p.frame, p.wpm, 1);
  assert.ok(Math.abs(firstDelay - plain * TIMING.rampStart) < 1e-6);
  while (p.playing && clock.step(0)) { /* run */ }
  assert.equal(p.item.kind, 'equation');
  assert.equal(p.state, 'paused');
  // play continues past the held item
  p.play();
  assert.equal(p.item.kind, 'heading');
  assert.equal(states.at(-1), 'playing');
  p.pause();
  assert.equal(clock.pending(), 0);
});

test('player seek, chunking and end-of-stream behaviour', () => {
  const s = buildStream(wordsPaper(0, 'a b c d e f g h'));
  const clock = fakeClock();
  const p = new Player({ clock });
  p.load(s, 0);
  p.setChunk(3);
  assert.deepEqual(p.frame, { start: 0, end: 3 });
  p.seek(6);
  assert.deepEqual(p.frame, { start: 6, end: 8 });
  p.play();
  while (p.playing && clock.step(0)) { /* run */ }
  assert.equal(p.state, 'ended');
  assert.equal(p.play(), false);
  p.seek(0);
  assert.equal(p.state, 'paused');
  assert.equal(p.play(), true);
  p.pause();
});

test('an onFrame handler can pause playback (auto-open figure at first mention)', () => {
  const s = buildStream(samplePaper());
  const clock = fakeClock();
  let p;
  p = new Player({
    clock,
    onFrame: (f, cause) => { if (cause === 'tick' && s.items[f.start].mentions) p.pause(); },
  });
  p.load(s, s.blockStart[3]);
  p.play();
  while (p.playing && clock.step(0)) { /* run */ }
  assert.equal(p.index, s.mentionIndex['fig-1']);
  assert.equal(clock.pending(), 0);
});

// ---------- the bundled mock paper, if present ----------

const fixture = fileURLToPath(new URL('../mock/paper.json', import.meta.url));
test('mock fixture builds a sane stream', { skip: !existsSync(fixture) && 'no web/mock/paper.json yet' }, () => {
  const paper = JSON.parse(readFileSync(fixture, 'utf8'));
  const s = buildStream(paper);
  assert.ok(s.items.length > 1000);
  // every asset mentioned in the readable text resolves to a mention index
  for (const a of paper.assets) if (a.first_ref_block != null) assert.ok(a.id in s.mentionIndex, a.id);
  // skip_default sections are excluded and their blocks map forward
  const skipped = paper.sections.filter((x) => x.skip_default).map((x) => x.id);
  assert.ok(skipped.length > 0);
  for (const it of s.items) assert.ok(!skipped.includes(it.section));
  // word count agrees with the backend definition (plain whitespace split)
  const words = paper.blocks
    .filter((b) => (b.type === 'paragraph' || b.type === 'heading') && !s.skippedIds.has(b.section))
    .reduce((n, b) => n + b.text.split(/\s+/).filter(Boolean).length, 0);
  assert.equal(words, paper.word_count);
  const rows = sectionRanges(s);
  assert.equal(rows.length, paper.sections.length);
});

// ---------- inline images (math from web books) ----------

function mathPaper() {
  return {
    sections: [{ id: 's1', title: 'One', level: 1, first_block: 0, skip_default: false }],
    blocks: [
      {
        type: 'paragraph', section: 's1', page: 1,
        text: 'The ratio (p1-4), is large and gone too.',
        images: { 'p1-4': { image: 'p1-4.png', w: 2.5, h: 1.8, d: 0.6, alt: '\\frac{a}{b}' } },
      },
    ],
  };
}

test('an inline image is one word that keeps its surrounding punctuation', () => {
  const s = buildStream(mathPaper());
  const it = s.items[2];
  assert.deepEqual(it.img, { image: 'p1-4.png', w: 2.5, h: 1.8, d: 0.6, alt: '\\frac{a}{b}', pre: '(', post: '),' });
  assert.equal(it.text, '([math]),');
  assert.equal(clauseMark(it.text), 'comma');
  assert.equal(s.items.length, 8);
});

test('an inline image whose picture is missing leaves no marker characters behind', () => {
  const s = buildStream(mathPaper());
  const it = s.items[6];
  assert.equal(it.img, undefined);
  assert.equal(it.text, 'gone');
});

test('an inline image is shown longer than a plain word', () => {
  const s = buildStream(mathPaper());
  assert.ok(s.items[2].factor > wordFactor('(x),') + 1);
});

test('an inline image is never flashed together with other words', () => {
  const s = buildStream(mathPaper());
  assert.deepEqual(frameAt(s, 0, 3), { start: 0, end: 2 });   // "The ratio" stops before the image
  assert.deepEqual(frameAt(s, 2, 3), { start: 2, end: 3 });   // the image alone
});
