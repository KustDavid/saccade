// reader.js — the reading screen: RSVP stage, full-text mode, outline, figures,
// lightbox, keyboard, and progress saving. Timing lives in core.js (Player).

import { api, ApiError } from './api.js';
import {
  buildStream, Player, frameText, splitAtOrp, positionAt, indexAt, percentAt, indexAtFraction,
  remainingMs, rangeMs, formatDuration, sentenceStartAt, prevSentenceIndex, nextSentenceIndex,
  sectionRanges, clampWpm, WPM_MIN, WPM_MAX, WPM_STEP,
} from './core.js';
import {
  settings, setSetting, onSettingChange, h, esc, icon, toast, toggleTheme, effectiveTheme,
  isTyping, authorsLine,
} from './ui.js';

const PANEL_COLS = ['figure', 'table', 'note', 'mark'];
const PIVOT = 0.4;               // pivot letter sits at 40% of the stage width
const LONG_PAUSE_MS = 20000;     // "smart rewind" threshold
const SAVE_EVERY_MS = 5000;      // progress save cadence while playing

const SETTINGS_UI = [
  { key: 'font', label: 'Font', options: [['serif', 'Serif'], ['sans', 'Sans'], ['mono', 'Mono']] },
  { key: 'size', label: 'Size', options: [['s', 'S'], ['m', 'M'], ['l', 'L']] },
  { key: 'chunk', label: 'Words per flash', options: [[1, '1'], [2, '2'], [3, '3']] },
  { key: 'context', label: 'Paragraph while playing', options: [['hide', 'Hide'], ['dim', 'Dim']] },
  { key: 'assetFlow', label: 'Figures in the text', options: [['brief', 'Show briefly'], ['pause', 'Pause']] },
  { key: 'autoOpenFigures', label: 'Open each figure at its first mention', toggle: true },
  { key: 'includeSkipped', label: 'Read references & appendix too', toggle: true },
  { key: 'headingPause', label: 'Wait at a new section', options: [['all', 'Every heading'], ['main', 'Main sections only'], ['off', 'Off']] },
  { key: 'smartRewind', label: 'After a long pause, restart the sentence', toggle: true },
  { key: 'theme', label: 'Theme', options: [['auto', 'Auto'], ['light', 'Light'], ['dark', 'Dark']] },
];

const KEYS = [
  ['Space', 'Play / pause · continue after a new section or an equation'],
  ['← →', 'Previous / next sentence'],
  ['⇧← ⇧→', 'Previous / next word'],
  ['↑ ↓', 'Faster / slower (±25 wpm)'],
  ['[ ]', 'Previous / next section'],
  ['Enter', 'Open the figure or table mentioned here'],
  ['F', 'Figures panel'],
  ['T', 'Tables panel'],
  ['Z', 'Footnotes panel'],
  ['H', 'Highlight this sentence (also in the PDF) · again to remove'],
  ['N', 'Write a note on this sentence'],
  ['C', 'Contents'],
  ['M', 'Full text ⇄ one word at a time'],
  ['1 2 3', 'Words per flash'],
  ['P', 'Open the PDF at this page'],
  ['D', 'Light / dark'],
  ['Home', 'Back to the beginning'],
  ['Esc', 'Close panels'],
  ['?', 'This help'],
];

export function mountReader(root, paperId, ctx) {
  const reader = new Reader(root, paperId, ctx);
  reader.start();
  return { destroy: () => reader.destroy() };
}

class Reader {
  constructor(root, id, ctx) {
    this.root = root;
    this.id = id;
    this.ctx = ctx;
    this.paper = null;
    this.stream = null;
    this.player = null;
    this.cover = false;          // title card before the first play
    this.ctxBlock = -1;          // block shown in the context paragraph
    this.ctxStart = 0;
    this.ctxCur = [];
    this.chipBlock = -1;
    this.sectionId = undefined;
    this.cardKey = '';
    this.pausedAt = 0;
    this.navigatedSincePause = false;
    this.autoOpened = new Set();
    this.lb = null;              // lightbox state
    this.lastSaveAt = 0;
    this.lastSavedKey = '';
    this.saveTimer = null;
    this.saveFailed = false;
    this.hadProgress = false;
    this.ftEls = null;           // full-text: item index -> element
    this.ftCur = [];
    this.ftSent = [-1, -1];
    this.userScrollAt = 0;
    this.timeKey = '';
    this.disposers = [];
    this.destroyed = false;
    this.build();
  }

  // ---------------------------------------------------------------- DOM

  build() {
    document.title = 'Pace';
    this.root.innerHTML = `
      <div class="reader" data-state="loading">
        <header class="r-top">
          <a class="icon-btn" href="#/" title="Library" aria-label="Back to library">${icon('back')}</a>
          <div class="r-title"><span class="r-paper-title"></span><span class="r-section-top"></span></div>
          <nav class="r-tools" aria-label="Reader tools">
            <button class="icon-btn" data-act="outline" title="Contents (C)" aria-label="Contents">${icon('list')}</button>
            <button class="icon-btn" data-act="figures" title="Figures (F) · Tables (T) · Footnotes (Z)" aria-label="Figures, tables and footnotes">${icon('image')}<span class="badge"></span></button>
            <button class="icon-btn" data-act="mode" title="Full text (M)" aria-label="Switch reading mode"></button>
            <a class="icon-btn r-pdf" target="_blank" rel="noopener" title="Open the PDF at this page (P)" aria-label="Open PDF">${icon('pdf')}<span class="r-pdf-page"></span></a>
            <button class="icon-btn" data-act="theme" title="Light / dark (D)" aria-label="Toggle theme"></button>
            <button class="icon-btn" data-act="settings" title="Settings" aria-label="Settings" aria-expanded="false">${icon('sliders')}</button>
            <button class="icon-btn" data-act="help" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts">${icon('help')}</button>
          </nav>
        </header>

        <main class="r-main">
          <section class="r-rsvp">
            <div class="stage" aria-label="Reading stage. Click to play or pause.">
              <i class="stage-line top"></i><i class="stage-line bottom"></i>
              <i class="stage-tick top"></i><i class="stage-tick bottom"></i>
              <div class="stage-word" aria-hidden="true"><span class="pre"></span><span class="orp"></span><span class="post"></span></div>
              <div class="stage-card"></div>
            </div>
            <div class="chips" aria-label="Figures mentioned here"></div>
            <div class="context"><p class="ctx-text"></p></div>
          </section>
          <section class="r-text" tabindex="-1"><article class="ft"></article></section>
          <div class="notepad" role="dialog" aria-label="Note on this sentence" hidden></div>
          <div class="r-message" hidden></div>
        </main>

        <footer class="r-bottom">
          <div class="scrub" aria-label="Position in paper — click or drag to jump">
            <div class="scrub-track"><div class="scrub-fill"></div><div class="scrub-ticks"></div></div>
            <div class="scrub-tip" hidden></div>
          </div>
          <div class="controls">
            <div class="c-left"><div class="c-section"></div><div class="c-time"></div></div>
            <div class="c-center">
              <button class="icon-btn" data-act="prev" title="Previous sentence (←)" aria-label="Previous sentence">${icon('prevSent', 22)}</button>
              <button class="play-btn" data-act="play" title="Play / pause (Space)" aria-label="Play">${icon('play', 26)}</button>
              <button class="icon-btn" data-act="next" title="Next sentence (→)" aria-label="Next sentence">${icon('nextSent', 22)}</button>
            </div>
            <div class="c-right">
              <input type="range" class="wpm" min="${WPM_MIN}" max="${WPM_MAX}" step="${WPM_STEP}" aria-label="Words per minute">
              <output class="wpm-val"></output><span class="wpm-unit">wpm</span>
            </div>
          </div>
        </footer>

        <aside class="drawer left d-outline" aria-label="Contents">
          <header class="d-head"><div><div class="d-title">Contents</div><div class="d-sub"></div></div>
            <button class="icon-btn" data-act="close-drawers" aria-label="Close">${icon('x')}</button></header>
          <label class="switch"><input type="checkbox" data-setting="includeSkipped"><span>Read references &amp; appendix too</span></label>
          <ol class="toc"></ol>
        </aside>
        <aside class="drawer right d-figures" aria-label="Figures and tables">
          <div class="d-resize" title="Drag to resize"></div>
          <header class="d-head"><div><div class="d-title">Figures, tables &amp; notes</div><div class="d-sub">Stays open while you read · click one to enlarge</div></div>
            <button class="icon-btn" data-act="close-figures" aria-label="Close">${icon('x')}</button></header>
          <div class="seg fig-tabs" role="tablist" aria-label="Show"></div>
          <div class="fig-list"></div>
        </aside>
        <div class="scrim" data-act="close-drawers"></div>
        <div class="popover settings-pop" role="dialog" aria-label="Settings" hidden></div>
        <div class="modal help" role="dialog" aria-label="Keyboard shortcuts" hidden></div>
        <div class="lightbox" role="dialog" aria-label="Figure viewer" hidden></div>
      </div>`;

    const $ = (s) => this.root.querySelector(s);
    this.el = {
      reader: $('.reader'), title: $('.r-paper-title'), sectionTop: $('.r-section-top'),
      stage: $('.stage'), word: $('.stage-word'), pre: $('.pre'), orp: $('.orp'), post: $('.post'), card: $('.stage-card'),
      chips: $('.chips'), context: $('.context'), ctxText: $('.ctx-text'),
      text: $('.r-text'), ft: $('.ft'), message: $('.r-message'),
      scrub: $('.scrub'), fill: $('.scrub-fill'), ticks: $('.scrub-ticks'), tip: $('.scrub-tip'),
      section: $('.c-section'), time: $('.c-time'), play: $('[data-act="play"]'),
      wpm: $('.wpm'), wpmVal: $('.wpm-val'), pdf: $('.r-pdf'), pdfPage: $('.r-pdf-page'),
      mode: $('[data-act="mode"]'), theme: $('[data-act="theme"]'), badge: $('.badge'),
      toc: $('.toc'), outlineSub: $('.d-outline .d-sub'), figList: $('.fig-list'), figTabs: $('.fig-tabs'), figDrawer: $('.d-figures'),
      settingsPop: $('.settings-pop'), settingsBtn: $('[data-act="settings"]'), help: $('.modal.help'), lightbox: $('.lightbox'), notepad: $('.notepad'),
      skipToggle: $('[data-setting="includeSkipped"]'),
    };
    this.paintTheme();
    this.paintMode();
    this.paintWpm();
    this.el.skipToggle.checked = settings.includeSkipped;
    this.el.reader.classList.toggle('ctx-dim', settings.context === 'dim');
  }

  on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    this.disposers.push(() => target.removeEventListener(type, fn, opts));
  }

  // ---------------------------------------------------------------- loading

  start() {
    this.bindEvents();
    this.load();
  }

  async load() {
    this.showMessage('<span class="spinner"></span>');
    let paper;
    let progress = null;
    try {
      [paper, progress] = await Promise.all([
        api.getPaper(this.id),
        api.getProgress(this.id).catch(() => null),
      ]);
    } catch (e) {
      if (this.destroyed) return;
      return this.handleLoadError(e);
    }
    if (this.destroyed) return;
    this.annots = [];
    this.setup(paper, progress || paper.progress || null);
    api.listAnnotations(this.id).then((xs) => {
      if (this.destroyed || !Array.isArray(xs)) return;
      this.annots = xs;
      this.annotsChanged();
    }).catch(() => {});
  }

  async handleLoadError(e) {
    if (e instanceof ApiError && e.status === 404) {
      // 404 until the paper is ready: find out whether it's processing, failed, or gone
      let summary = null;
      try { summary = (await api.listPapers()).find((p) => p.id === this.id); } catch { /* ignore */ }
      if (this.destroyed) return;
      if (summary?.status === 'processing') {
        this.showMessage(`<span class="spinner"></span><h2>${esc(summary.title || 'This paper')} is still being processed</h2><p>It will open by itself when it's ready.</p><p><a href="#/">Back to library</a></p>`);
        this.retryTimer = setTimeout(() => this.load(), 2000);
        return;
      }
      if (summary?.status === 'error') {
        this.showMessage(`<h2>This PDF couldn't be processed</h2><p>${esc(summary.error || '')}</p><p><a href="#/">Back to library</a></p>`);
        return;
      }
      this.showMessage('<h2>Paper not found</h2><p>It may have been deleted.</p><p><a href="#/">Back to library</a></p>');
      return;
    }
    const offline = e?.status === 0;
    this.showMessage(`<h2>${offline ? 'The reading server isn’t reachable' : 'Couldn’t open this paper'}</h2><p>${esc(e?.message || e)}</p><p><a href="#/">Back to library</a></p>`);
    if (offline) this.retryTimer = setTimeout(() => this.load(), 3000);
  }

  showMessage(html) {
    this.el.message.innerHTML = html;
    this.el.message.hidden = false;
    this.el.reader.dataset.state = 'loading';
  }

  setup(paper, progress) {
    this.paper = paper;
    this.el.message.hidden = true;
    document.title = `${paper.title || 'Paper'} — Pace`;
    this.el.title.textContent = paper.title || paper.source_filename || '';
    this.el.outlineSub.textContent = [authorsLine(paper.authors, 2), paper.year].filter(Boolean).join(' · ');
    const nAssets = (paper.assets || []).length;
    this.el.badge.textContent = nAssets ? String(nAssets) : '';
    this.stream = buildStream(paper, { includeSkipped: settings.includeSkipped });

    this.player = new Player({
      onFrame: (f, cause) => this.onFrame(f, cause),
      onState: (s) => this.onState(s),
      isHold: (it) => this.isHold(it),
    });
    this.player.setWpm(settings.wpm);
    this.player.chunk = settings.mode === 'rsvp' ? settings.chunk : 1;

    let index = 0;
    const hasPos = progress && ((progress.block | 0) > 0 || (progress.word | 0) > 0);
    if (hasPos) {
      // resume at the start of the sentence they were in: context beats precision
      index = sentenceStartAt(this.stream, indexAt(this.stream, progress.block, progress.word));
      this.hadProgress = true;
    }
    this.cover = !hasPos;
    this.renderOutline();
    this.renderFigures();
    this.renderTicks();
    this.el.reader.classList.toggle('mode-text', settings.mode === 'text');
    if (settings.mode === 'text') this.renderFullText();
    this.player.load(this.stream, index);
    this.lastSavedKey = this.saveKey(); // the resumed position is already saved
    this.onState(this.player.state);
    if (hasPos) {
      const sec = this.sectionTitle(this.player.item?.section);
      toast(`Resuming at ${Math.round(percentAt(this.stream, index))}%${sec ? ` · ${sec}` : ''}`);
    }
    if (!this.stream.items.length) this.showMessage('<h2>No readable text was found in this paper.</h2><p><a href="#/">Back to library</a></p>');
  }

  // ---------------------------------------------------------------- player callbacks

  isHold(it) {
    if (!it) return false;
    // a new section waits for Space or a click, so there is a moment to take stock before going on
    if (it.kind === 'heading' && settings.headingPause !== 'off') return settings.headingPause === 'all' || (it.level || 1) <= 1;
    if (settings.mode !== 'rsvp') return false;
    return it.kind === 'equation' || (it.kind === 'asset' && settings.assetFlow === 'pause');
  }

  onFrame(frame, cause) {
    if (cause !== 'load') this.cover = false;
    const it = this.stream.items[frame.start];
    if (settings.mode === 'rsvp') {
      this.renderStage(cause);
      this.renderContext(frame);
      this.renderChips(frame);
    } else {
      this.highlightText(frame, cause);
    }
    this.renderPosition(it);
    if (cause === 'tick' && settings.autoOpenFigures) this.maybeAutoOpen(frame);
    if (this.player?.playing && performance.now() - this.lastSaveAt > SAVE_EVERY_MS) this.saveNow();
    else if (cause === 'seek') this.saveSoon();
  }

  onState(state) {
    const r = this.el.reader;
    r.dataset.state = state;
    const playing = state === 'playing';
    this.el.play.innerHTML = icon(playing ? 'pause' : 'play', 26);
    this.el.play.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    if (!playing) {
      this.pausedAt = performance.now();
      this.navigatedSincePause = false;
      if (this.stream) {
        if (settings.mode === 'rsvp') { this.renderStage('state'); this.scrollContextToCurrent(); }
        this.saveNow();
      }
    }
  }

  // ---------------------------------------------------------------- RSVP stage

  renderStage(cause) {
    const p = this.player;
    if (!p) return;
    const it = p.item;
    const st = this.el.stage;
    this.el.reader.classList.toggle('is-cover', !!this.cover);
    if (this.cover) return this.showCard('cover', () => this.coverHtml());
    if (p.state === 'ended') return this.showCard('end', () => this.endHtml());
    if (!it) return;
    if (it.kind === 'word') {
      st.classList.remove('has-card');
      this.cardKey = '';
      const [a, b, c] = splitAtOrp(frameText(this.stream, p.frame));
      this.el.pre.textContent = a;
      this.el.orp.textContent = b;
      this.el.post.textContent = c;
      this.positionWord();
      return;
    }
    const held = !p.playing;
    if (it.kind === 'heading') {
      const row = this.sectionRows?.find((r) => r.id === it.section);
      const mins = row && row.start === p.index ? formatDuration(rangeMs(this.stream, row.start, row.end, settings.wpm)) : '';
      const wait = held && this.isHold(it) && !this.cover;
      return this.showCard(`h${p.index}${wait}`, () => `
        <div class="card heading lvl-${it.level}">
          <div class="h-text">${esc(it.text)}</div>
          ${mins ? `<div class="h-meta">${mins}</div>` : ''}
          ${wait ? '<div class="hint"><kbd>Space</kbd> or click to continue</div>' : ''}
        </div>`);
    }
    if (it.kind === 'equation') {
      const blk = this.stream.blocks[it.b];
      return this.showCard(`e${p.index}:${held}`, () => `
        <div class="card equation">
          ${blk.image ? `<img src="${esc(api.assetUrl(this.id, blk.image))}" alt="Equation">` : `<code>${esc(blk.text)}</code>`}
          <div class="hint">${held ? '<kbd>Space</kbd> to continue' : ''}</div>
        </div>`);
    }
    if (it.kind === 'asset') {
      const a = this.stream.assets.get(it.asset);
      const brief = p.playing;
      // re-key on every play/tick so the countdown restarts with the real remaining time
      return this.showCard(`a${p.index}:${brief ? cause : 'held'}:${brief ? performance.now() : ''}`, () => `
        <button class="card asset" data-asset="${esc(a.id)}">
          <img src="${esc(api.assetUrl(this.id, a.image))}" alt="${esc(a.label)}">
          <span class="asset-text">
            <span class="asset-label">${esc(a.label)}</span>
            <span class="asset-cap">${esc(a.caption || '')}</span>
            <span class="hint"><kbd>Enter</kbd> to open${brief ? '' : ' · <kbd>Space</kbd> to continue'}</span>
          </span>
          ${brief ? `<span class="countdown" style="animation-duration:${this.stream.items[p.index].fixedMs}ms"></span>` : ''}
        </button>`);
    }
  }

  showCard(key, html) {
    this.el.stage.classList.add('has-card');
    if (key === this.cardKey) return;
    this.cardKey = key;
    this.el.card.innerHTML = html();
  }

  coverHtml() {
    const p = this.paper;
    const mins = formatDuration(remainingMs(this.stream, 0, settings.wpm));
    const meta = [p.year, p.page_count ? `${p.page_count} pages` : '', p.num_figures ? `${p.num_figures} figures` : '', p.num_tables ? `${p.num_tables} tables` : '']
      .filter(Boolean).join(' · ');
    return `
      <div class="card cover">
        <div class="cover-meta">${esc(meta)}</div>
        <h1>${esc(p.title || p.source_filename || '')}</h1>
        ${p.authors?.length ? `<p class="cover-authors">${esc(authorsLine(p.authors, 6))}</p>` : ''}
        <button class="btn primary" data-act="play">${icon('play', 16)} Start reading · ${mins} at ${settings.wpm} wpm</button>
        <p class="cover-keys"><kbd>Space</kbd> play/pause · <kbd>←</kbd><kbd>→</kbd> sentence · <kbd>↑</kbd><kbd>↓</kbd> speed · <kbd>F</kbd> figures · <kbd>?</kbd> more</p>
      </div>`;
  }

  endHtml() {
    const s = this.stream;
    const skippedExist = s.skippedIds.size > 0 && !settings.includeSkipped;
    return `
      <div class="card end">
        <h2>End of paper</h2>
        <p>${(this.paper.word_count || s.items.length).toLocaleString()} words read.</p>
        <div class="end-btns">
          <button class="btn" data-act="restart">${icon('restart', 16)} Read again</button>
          <a class="btn primary" href="#/">Back to library</a>
        </div>
        ${skippedExist ? '<p class="end-skip">References and appendix were skipped. <button class="linklike" data-act="read-skipped">Read them now</button></p>' : ''}
      </div>`;
  }

  positionWord() {
    const { stage, word, pre, orp, post } = this.el;
    const W = stage.clientWidth;
    if (!W) return;
    const pivotX = Math.round(W * PIVOT);
    const preW = pre.offsetWidth;
    const orpW = orp.offsetWidth;
    const postW = post.offsetWidth;
    const anchor = preW + orpW / 2;
    const pad = Math.min(24, W * 0.04);
    let scale = 1;
    if (anchor > pivotX - pad) scale = Math.min(scale, (pivotX - pad) / anchor);
    if (postW + orpW / 2 > W - pivotX - pad) scale = Math.min(scale, (W - pivotX - pad) / (postW + orpW / 2));
    word.style.transformOrigin = `${anchor}px 50%`;
    word.style.transform = `translate(${pivotX - anchor}px, -50%) scale(${scale.toFixed(3)})`;
  }

  layoutStage() {
    const W = this.el.stage.clientWidth;
    this.el.stage.style.setProperty('--pivot-x', `${Math.round(W * PIVOT)}px`);
    if (this.player && settings.mode === 'rsvp' && !this.el.stage.classList.contains('has-card')) this.positionWord();
  }

  // ---------------------------------------------------------------- context paragraph & chips

  renderContext(frame) {
    const s = this.stream;
    const it = s.items[frame.start];
    const box = this.el.ctxText;
    if (it.b !== this.ctxBlock) {
      this.ctxBlock = it.b;
      this.ctxCur = [];
      box.textContent = '';
      box.className = 'ctx-text';
      if (it.kind === 'word') {
        const start = s.blockStart[it.b];
        this.ctxStart = start;
        const frag = document.createDocumentFragment();
        for (let i = start; i < start + s.blockLen[it.b]; i++) {
          const sp = document.createElement('span');
          sp.className = 'w';
          sp.dataset.i = i;
          sp.textContent = s.items[i].text;
          frag.append(sp, ' ');
        }
        box.append(frag);
      } else if (it.kind === 'heading' || it.kind === 'equation') {
        // paused on a heading or an equation: show the paragraph that follows it
        // (after an equation that is usually where its variables are defined)
        let next = s.items[frame.start + 1];
        if (it.kind === 'equation') {
          for (let k = frame.start + 1; k <= frame.start + 3 && next && next.kind !== 'word' && next.kind !== 'heading'; k++) next = s.items[k + 1];
        }
        if (next?.kind === 'word') {
          this.ctxStart = s.blockStart[next.b];
          for (let i = this.ctxStart; i < this.ctxStart + s.blockLen[next.b]; i++) {
            const sp = document.createElement('span');
            sp.className = 'w';
            sp.dataset.i = i;
            sp.textContent = s.items[i].text;
            box.append(sp, ' ');
          }
        } else if (it.kind === 'heading') {
          box.classList.add('is-heading');
          box.textContent = it.text;
        }
      } else if (it.kind === 'asset') {
        const a = s.assets.get(it.asset);
        box.innerHTML = `<b>${esc(a.label)}.</b> ${esc(a.caption || '')}`;
      }
    }
    for (const el of this.ctxCur) el.classList.remove('cur');
    this.ctxCur = [];
    if (it.kind === 'word') {
      for (let i = frame.start; i < frame.end; i++) {
        const el = box.children[i - this.ctxStart];
        if (el) { el.classList.add('cur'); this.ctxCur.push(el); }
      }
    }
  }

  scrollContextToCurrent() {
    const el = this.ctxCur[0];
    if (!el) return;
    const box = this.el.context;
    const top = el.offsetTop - box.offsetTop;
    if (top < box.scrollTop || top > box.scrollTop + box.clientHeight - 30) box.scrollTop = Math.max(0, top - box.clientHeight / 3);
  }

  renderChips(frame) {
    const s = this.stream;
    const it = s.items[frame.start];
    if (it.b !== this.chipBlock) {
      this.chipBlock = it.b;
      const blk = s.blocks[it.b];
      const ids = [...new Set([...(it.kind === 'word' ? blk.refs || [] : []), ...(it.kind === 'asset' ? [it.asset] : [])])]
        .filter((id) => s.assets.has(id));
      this.chipIds = ids;
      this.el.chips.innerHTML = ids.map((id) => {
        const a = s.assets.get(id);
        return `<button class="chip" data-asset="${esc(id)}" title="Open ${esc(a.label)} (Enter)">${icon('image', 14)}<span>${esc(a.label)}</span><kbd>↵</kbd></button>`;
      }).join('');
    }
    const active = this.activeAssetId();
    for (const c of this.el.chips.children) c.classList.toggle('active', c.dataset.asset === active);
  }

  /** The figure Enter should open: the most recent mention at or before the current word. */
  activeAssetId() {
    const s = this.stream;
    const p = this.player;
    if (!s || !p) return null;
    const it = p.item;
    if (it?.kind === 'asset') return it.asset;
    if (!it || it.kind !== 'word') return null;
    const start = s.blockStart[it.b];
    let best = null;
    for (let i = start; i < p.frame.end; i++) {
      const m = s.items[i].mentions;
      if (m) best = m[m.length - 1];
    }
    return best || (s.blocks[it.b].refs || []).find((id) => s.assets.has(id)) || null;
  }

  maybeAutoOpen(frame) {
    const s = this.stream;
    for (let i = frame.start; i < frame.end; i++) {
      for (const id of s.items[i].mentions || []) {
        if (s.mentionIndex[id] === i && !this.autoOpened.has(id)) {
          this.autoOpened.add(id);
          this.player.pause();
          this.openLightbox(id, { resume: true, note: 'First mention — Esc to keep reading' });
          return;
        }
      }
    }
  }

  // ---------------------------------------------------------------- position, time, section

  renderPosition(it) {
    const s = this.stream;
    const p = this.player;
    const pct = p.state === 'ended' ? 100 : percentAt(s, p.index);
    this.el.fill.style.width = `${pct}%`;
    const left = p.state === 'ended' ? 'finished' : `${formatDuration(remainingMs(s, p.index, settings.wpm))} left`;
    const timeKey = `${Math.round(pct)}|${left}`;
    if (timeKey !== this.timeKey) {
      this.timeKey = timeKey;
      this.el.time.textContent = `${Math.round(pct)}% · ${left}`;
    }
    if (it && it.section !== this.sectionId) {
      this.sectionId = it.section;
      const title = this.sectionTitle(it.section);
      this.el.section.textContent = title;
      this.el.sectionTop.textContent = title ? ` · ${title}` : '';
      for (const li of this.el.toc.children) li.classList.toggle('active', li.dataset.sec === it.section);
    }
    const page = s.blocks[it?.b]?.page;
    if (page !== this.pdfPage) {
      this.pdfPage = page;
      this.el.pdf.href = api.pdfUrl(this.id, page);
      this.el.pdfPage.textContent = page ? `p.${page}` : '';
      this.el.pdf.title = `Open the PDF${page ? ` at page ${page}` : ''} (P)`;
      this.markNotes(true);
    }
  }

  sectionTitle(id) {
    return (this.paper?.sections || []).find((x) => x.id === id)?.title || '';
  }

  // ---------------------------------------------------------------- outline, figures, ticks

  renderOutline() {
    const rows = sectionRanges(this.stream);
    this.sectionRows = rows;
    this.el.toc.innerHTML = rows.map((r) => {
      const time = r.start >= 0 ? formatDuration(rangeMs(this.stream, r.start, r.end, settings.wpm)) : r.skipped ? 'skipped' : '';
      return `<li class="lvl-${Math.min(3, r.level)}${r.skipped ? ' skipped' : ''}${r.id === this.sectionId ? ' active' : ''}" data-sec="${esc(r.id)}">
        <button><span class="t">${esc(r.title)}</span><span class="m">${time}</span></button></li>`;
    }).join('');
  }

  renderTicks() {
    const s = this.stream;
    const rows = (this.sectionRows || []).filter((r) => r.start > 0 && r.level === 1);
    this.el.ticks.innerHTML = rows.map((r) => `<i style="left:${percentAt(s, r.start)}%"></i>`).join('');
  }

  renderFigures() {
    const assets = this.paper.assets || [];
    const notes = this.paper.footnotes || [];
    const of = (kind) => assets.filter((a) => (a.kind || 'figure') === kind);
    this.el.figTabs.innerHTML = [['figure', 'Figures', of('figure').length, 'F'], ['table', 'Tables', of('table').length, 'T'], ['note', 'Footnotes', notes.length, 'Z'], ['mark', 'Highlights', (this.annots || []).length, 'H to add']]
      .map(([v, l, n, k]) => `<button data-figtab="${v}" title="${l} (${k})">${l} ${n}</button>`).join('');
    const col = (kind, title) => `<div class="fig-col" data-kind="${kind}"><h3>${title}</h3>` + (of(kind).map((a) => `
        <button class="fig-card" data-asset="${esc(a.id)}">
          <span class="fig-thumb"><img loading="lazy" src="${esc(api.assetUrl(this.id, a.image))}" alt="${esc(a.label)}"></span>
          <span class="fig-meta"><b>${esc(a.label)}</b>${a.page ? `<span>p. ${a.page}</span>` : ''}</span>
          <span class="fig-cap">${esc(a.caption || '')}</span>
        </button>`).join('') || `<p class="empty">No ${title.toLowerCase()} were found in this paper.</p>`) + '</div>';
    const noteCol = `<div class="fig-col" data-kind="note"><h3>Footnotes</h3>` + (notes.map((n) => `
        <div class="note" data-page="${n.page ?? ''}"><span class="note-mark">${esc(String(n.marker ?? ''))}</span>
          <p>${esc(n.text || '')}${n.page ? ` <span class="note-page">p. ${n.page}</span>` : ''}</p></div>`).join('')
      || '<p class="empty">No footnotes were found in this paper.</p>') + '</div>';
    this.el.figList.innerHTML = col('figure', 'Figures') + col('table', 'Tables') + noteCol
      + '<div class="fig-col" data-kind="mark"></div>';
    this.renderMarks();
    this.applyFigsLayout();
    this.markNotes(true);
  }

  figCols() {
    const xs = String(settings.figsCols).split(',').filter((k) => PANEL_COLS.includes(k));
    return xs.length ? xs : ['figure'];
  }

  // F / T / Z and the buttons in the panel: closed -> open with just that column; open -> add or remove it.
  togglePanelCol(kind) {
    const r = this.el.reader;
    let cols = this.figCols();
    if (!r.classList.contains('open-figures')) {
      cols = [kind];
      if (matchMedia('(max-width: 760px)').matches) r.classList.remove('open-outline');
      r.classList.add('open-figures');
    } else if (cols.includes(kind)) {
      cols = cols.filter((k) => k !== kind);
      if (!cols.length) { r.classList.remove('open-figures'); return; }
    } else cols = PANEL_COLS.filter((k) => k === kind || cols.includes(k));
    if (cols.join(',') === settings.figsCols) this.applyFigsLayout(); else setSetting('figsCols', cols.join(','));
    this.markNotes(true);
  }

  // Columns shown and width of the docked panel.
  applyFigsLayout() {
    const cols = this.figCols();
    const max = Math.max(280, window.innerWidth - 380);       // always leave room for the reading stage
    const w = Math.min(max, Math.max(280, settings.figsWidth) * cols.length);
    this.el.reader.style.setProperty('--figs-w', `${Math.round(w)}px`);
    this.el.figDrawer.dataset.cols = cols.length;
    for (const c of this.el.figList.children) c.hidden = !cols.includes(c.dataset.kind);
    for (const b of this.el.figTabs.children) {
      const on = cols.includes(b.dataset.figtab);
      b.classList.toggle('on', on); b.setAttribute('aria-pressed', String(on));
    }
  }

  // Highlight the footnotes of the page being read and keep them in view.
  markNotes(scroll) {
    const col = this.el.figList.querySelector('.fig-col[data-kind="note"]');
    if (!col) return;
    let first = null;
    for (const n of col.querySelectorAll('.note')) {
      const here = this.pdfPage != null && n.dataset.page === String(this.pdfPage);
      n.classList.toggle('here', here);
      if (here && !first) first = n;
    }
    if (scroll && first && !col.hidden && this.el.reader.classList.contains('open-figures')) {
      col.scrollTo({ top: first.offsetTop - col.offsetTop - 8, behavior: 'smooth' });
    }
  }

  startFigsResize(e) {
    e.preventDefault();
    const cols = this.figCols().length;
    this.el.reader.classList.add('resizing');
    const move = (ev) => {
      const w = Math.min(window.innerWidth - 380, Math.max(280, window.innerWidth - ev.clientX));
      setSetting('figsWidth', Math.round(w / cols));
    };
    const up = () => {
      this.el.reader.classList.remove('resizing');
      window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
  }

  // ---------------------------------------------------------------- highlights & notes

  /** Stream index of an annotation's first word, or -1 when its section is skipped. */
  annotIndex(a) {
    const start = this.stream?.blockStart[a.block];
    return start >= 0 ? start + a.start : -1;
  }

  /** Map: stream index -> annotation, for every highlighted word. */
  hlIndex() {
    if (this._hl && this._hlStream === this.stream) return this._hl;
    const m = new Map();
    for (const a of this.annots || []) {
      const i = this.annotIndex(a);
      if (i < 0) continue;
      for (let k = i; k <= i + (a.end - a.start); k++) m.set(k, a);
    }
    this._hl = m; this._hlStream = this.stream;
    return m;
  }

  paintHl(box) {
    if (!box) return;
    const m = this.hlIndex();
    for (const el of box.querySelectorAll('.w[data-i]')) {
      const a = m.get(+el.dataset.i);
      el.classList.toggle('hl', !!a);
      el.classList.toggle('hl-note', !!a?.note);
      if (a?.note) el.title = a.note; else el.removeAttribute('title');
    }
  }

  annotsChanged() {
    this._hl = null;
    this.paintHl(this.el.ctxText);
    this.paintHl(this.el.ft);
    this.renderMarks();
    if (this.player && this.stream) this.el.stage.classList.toggle('is-hl', this.hlIndex().has(this.player.index));
  }

  /** The sentence being read: stream range plus what the server needs to store and find it. */
  currentSentence() {
    const s = this.stream; const p = this.player;
    if (!s || !p) return null;
    let a = sentenceStartAt(s, p.index);
    // At speed, the key lands a word or two into the next sentence: take the one just finished.
    if (p.playing && p.index - a <= 1 && a > 0 && s.items[a]?.kind === 'word' && s.items[a - 1].kind === 'word') a = sentenceStartAt(s, a - 1);
    if (s.items[a]?.kind !== 'word') return null;
    let b = a;
    while (!s.items[b].sentEnd && b + 1 < s.items.length) b++;
    const it = s.items[a];
    return {
      a, b, block: it.b, start: it.w, end: s.items[b].w, page: s.blocks[it.b]?.page ?? null,
      text: s.items.slice(a, b + 1).map((x) => x.text).join(' '),
    };
  }

  async toggleHighlight() {
    const r = this.currentSentence();
    if (!r) { toast('Nothing to highlight here'); return; }
    const ex = this.hlIndex().get(r.a);
    try {
      if (ex) {
        if (ex.note) { toast('This highlight has a note — press N to edit or delete it'); return; }
        await api.deleteAnnotation(this.id, ex.id);
        this.annots = this.annots.filter((x) => x.id !== ex.id);
        toast('Highlight removed', { timeout: 1400 });
      } else {
        const tmp = { id: `tmp-${Date.now()}`, block: r.block, start: r.start, end: r.end, text: r.text, page: r.page, note: '' };
        this.annots.push(tmp); this.annotsChanged();                       // show it at once; the PDF copy takes a moment
        let saved;
        try { saved = await api.createAnnotation(this.id, { block: r.block, start: r.start, end: r.end, text: r.text, page: r.page }); }
        catch (e) { this.annots = this.annots.filter((x) => x !== tmp); this.annotsChanged(); throw e; }
        this.annots[this.annots.indexOf(tmp)] = saved;
        toast(saved.in_pdf === false && !api.constructor.name.startsWith('Mock') ? 'Highlighted here, but this sentence could not be located in the PDF' : 'Highlighted', { timeout: 1400 });
      }
    } catch (e) { toast(e.message || 'Could not save the highlight', { kind: 'error' }); return; }
    this.annotsChanged();
  }

  openNote() {
    const r = this.currentSentence();
    if (!r) { toast('Nothing to note here'); return; }
    const ex = this.hlIndex().get(r.a);
    this.note = { r, ex, wasPlaying: !!this.player?.playing };
    this.player?.pause();
    const box = this.el.notepad;
    box.innerHTML = `
      <blockquote class="np-quote">${esc(r.text)}</blockquote>
      <textarea class="np-text" rows="3" placeholder="Your note on this sentence…" aria-label="Note"></textarea>
      <div class="np-actions">
        ${ex ? '<button class="btn subtle" data-np="remove">Delete highlight</button>' : ''}
        <span class="np-hint">⌘↵ save · Esc cancel</span>
        <button class="btn" data-np="cancel">Cancel</button>
        <button class="btn primary" data-np="save">Save</button>
      </div>`;
    box.hidden = false;
    const ta = box.querySelector('.np-text');
    ta.value = ex?.note || '';
    ta.focus();
  }

  closeNote() {
    const n = this.note;
    if (!n) return;
    this.note = null;
    this.el.notepad.hidden = true;
    this.el.notepad.innerHTML = '';
    if (n.wasPlaying) this.player?.play();
  }

  async saveNote() {
    const n = this.note;
    if (!n) return;
    const text = this.el.notepad.querySelector('.np-text').value.trim();
    try {
      if (n.ex) {
        const saved = await api.updateAnnotation(this.id, n.ex.id, text);
        this.annots = this.annots.map((x) => (x.id === saved.id ? saved : x));
      } else {
        const { r } = n;
        this.annots.push(await api.createAnnotation(this.id, { block: r.block, start: r.start, end: r.end, text: r.text, page: r.page, note: text }));
      }
    } catch (e) { toast(e.message || 'Could not save the note', { kind: 'error' }); return; }
    this.annotsChanged();
    this.closeNote();
    toast(text ? 'Note saved' : 'Highlighted', { timeout: 1400 });
  }

  async removeNote() {
    const n = this.note;
    if (!n?.ex) return;
    try { await api.deleteAnnotation(this.id, n.ex.id); }
    catch (e) { toast(e.message || 'Could not delete', { kind: 'error' }); return; }
    this.annots = this.annots.filter((x) => x.id !== n.ex.id);
    this.annotsChanged();
    this.closeNote();
    toast('Highlight removed', { timeout: 1400 });
  }

  renderMarks() {
    const col = this.el.figList.querySelector('.fig-col[data-kind="mark"]');
    if (!col) return;
    const xs = [...(this.annots || [])].sort((a, b) => a.block - b.block || a.start - b.start);
    const tab = this.el.figTabs.querySelector('[data-figtab="mark"]');
    if (tab) tab.textContent = `Highlights ${xs.length}`;
    col.innerHTML = '<h3>Highlights &amp; notes</h3>' + (xs.map((a) => `
      <div class="mark" data-ann="${esc(a.id)}" title="Jump to this sentence">
        <p class="mark-quote">${esc(a.text)}</p>
        ${a.note ? `<p class="mark-note">${esc(a.note)}</p>` : ''}
        <div class="mark-meta">${a.page ? `<span>p. ${a.page}</span>` : ''}${a.in_pdf === false ? '<span>not in PDF</span>' : ''}
          <button class="link" data-act2="edit-note">${a.note ? 'Edit note' : 'Add note'}</button></div>
      </div>`).join('') || '<p class="empty">Press H while reading to highlight the current sentence, N to add a note to it.</p>');
  }

  // ---------------------------------------------------------------- full-text mode

  renderFullText() {
    const s = this.stream;
    const art = this.el.ft;
    art.textContent = '';
    this.ftEls = new Array(s.items.length);
    this.ftCur = [];
    this.ftSent = [-1, -1];
    const frag = document.createDocumentFragment();
    const p = this.paper;
    frag.append(h('header', { class: 'ft-head' },
      h('h1', {}, p.title || ''),
      p.authors?.length ? h('p', { class: 'ft-authors' }, authorsLine(p.authors, 8)) : null));
    let i = 0;
    while (i < s.items.length) {
      const it = s.items[i];
      if (it.kind === 'heading') {
        const el = h(`h${Math.min(4, (it.level || 1) + 1)}`, { class: 'ft-h', 'data-i': i }, it.text);
        this.ftEls[i++] = el;
        frag.append(el);
      } else if (it.kind === 'equation') {
        const blk = s.blocks[it.b];
        const el = h('div', { class: 'ft-eq', 'data-i': i },
          blk.image ? h('img', { src: api.assetUrl(this.id, blk.image), alt: 'Equation', loading: 'lazy' }) : h('code', {}, blk.text));
        this.ftEls[i++] = el;
        frag.append(el);
      } else if (it.kind === 'asset') {
        const a = s.assets.get(it.asset);
        const el = h('figure', { class: 'ft-fig', 'data-i': i, 'data-asset': a.id },
          h('img', { src: api.assetUrl(this.id, a.image), alt: a.label, loading: 'lazy' }),
          h('figcaption', {}, h('b', {}, `${a.label}.`), ' ', a.caption || ''));
        this.ftEls[i++] = el;
        frag.append(el);
      } else {
        const para = document.createElement('p');
        const b = it.b;
        while (i < s.items.length && s.items[i].b === b) {
          const w = s.items[i];
          const sp = document.createElement('span');
          sp.className = 'w';
          sp.dataset.i = i;
          sp.textContent = w.text;
          if (w.mentions) {
            sp.classList.add('ref');
            sp.dataset.asset = w.mentions[0];
            const prev = this.ftEls[i - 1];
            if (prev && s.items[i - 1].b === b && /^\(?(fig|tab)/i.test(s.items[i - 1].text)) { prev.classList.add('ref'); prev.dataset.asset = w.mentions[0]; }
          }
          para.append(sp, ' ');
          this.ftEls[i++] = sp;
        }
        frag.append(para);
      }
    }
    art.append(frag);
  }

  highlightText(frame, cause) {
    if (!this.ftEls) this.renderFullText();
    for (const el of this.ftCur) el.classList.remove('cur');
    this.ftCur = [];
    for (let i = frame.start; i < frame.end; i++) {
      const el = this.ftEls[i];
      if (el) { el.classList.add('cur'); this.ftCur.push(el); }
    }
    // softly mark the whole current sentence
    const s = this.stream;
    const a = sentenceStartAt(s, frame.start);
    if (a !== this.ftSent[0]) {
      const [oa, ob] = this.ftSent;
      for (let i = oa; i >= 0 && i < ob; i++) this.ftEls[i]?.classList.remove('sent');
      let b = nextSentenceIndex(s, a);
      if (b <= a) b = s.items.length;
      if (b - a > 120) b = a + 120;
      for (let i = a; i < b; i++) if (s.items[i].kind === 'word') this.ftEls[i]?.classList.add('sent');
      this.ftSent = [a, b];
    }
    const el = this.ftCur[0];
    if (!el) return;
    const followed = this.player?.playing && performance.now() - this.userScrollAt > 2500;
    if (followed || cause === 'seek' || cause === 'load' || cause === 'play') this.follow(el, cause !== 'tick');
  }

  follow(el, force) {
    const box = this.el.text;
    const r = el.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    const y = r.top - b.top;
    if (!force && y > b.height * 0.2 && y < b.height * 0.62) return;
    if (force && y > b.height * 0.15 && y < b.height * 0.75) return;
    box.scrollTo({ top: box.scrollTop + y - b.height * 0.36, behavior: force ? 'auto' : 'smooth' });
  }

  // ---------------------------------------------------------------- actions

  toggle() {
    if (!this.player) return;
    if (this.player.playing) this.player.pause();
    else this.play();
  }

  play({ rewind = true } = {}) {
    const p = this.player;
    if (!p || !this.stream.items.length) return;
    if (p.state === 'ended') { toast('End of paper — press Home to start over'); return; }
    if (rewind && settings.smartRewind && !this.cover && !this.navigatedSincePause && performance.now() - this.pausedAt > LONG_PAUSE_MS && p.item?.kind === 'word') {
      const start = sentenceStartAt(this.stream, p.index);
      if (p.index - start >= 3) {
        p.seek(start, 'rewind');
        toast('Picking up at the start of the sentence', { timeout: 1600 });
      }
    }
    this.cover = false;
    p.play();
  }

  jump(index, { keepCover = false } = {}) {
    if (!this.player) return;
    this.navigatedSincePause = true;
    if (!keepCover) this.cover = false;
    this.player.seek(index);
  }

  stepSentence(dir) {
    const s = this.stream;
    const i = this.player.index;
    this.jump(dir < 0 ? prevSentenceIndex(s, i) : nextSentenceIndex(s, i));
  }

  stepWord(dir) {
    const p = this.player;
    this.jump(dir < 0 ? p.index - 1 : p.frame.end);
  }

  stepSection(dir) {
    const rows = (this.sectionRows || []).filter((r) => r.start >= 0);
    if (!rows.length) return;
    const i = this.player.index;
    let k = -1;
    rows.forEach((r, j) => { if (r.start <= i) k = j; });
    if (dir > 0) { if (rows[k + 1]) this.jump(rows[k + 1].start); return; }
    if (k >= 0 && i - rows[k].start > 2) this.jump(rows[k].start);
    else if (k > 0) this.jump(rows[k - 1].start);
    else this.jump(0);
  }

  setWpm(v) {
    const w = clampWpm(v);
    setSetting('wpm', w);
    this.player?.setWpm(w);
    this.paintWpm();
    if (this.player) {
      this.timeKey = '';
      this.renderPosition(this.player.item);
      if (this.cover) { this.cardKey = ''; this.renderStage('state'); }
    }
  }

  paintWpm() {
    this.el.wpm.value = settings.wpm;
    this.el.wpmVal.textContent = settings.wpm;
    const pct = ((settings.wpm - WPM_MIN) / (WPM_MAX - WPM_MIN)) * 100;
    this.el.wpm.style.setProperty('--fill', `${pct}%`);
  }

  paintTheme() { this.el.theme.innerHTML = icon(effectiveTheme() === 'dark' ? 'sun' : 'moon'); }

  paintMode() {
    const text = settings.mode === 'text';
    this.el.mode.innerHTML = icon(text ? 'bolt' : 'text');
    this.el.mode.title = text ? 'One word at a time (M)' : 'Full text (M)';
  }

  // Called via the settings listener; to switch modes, setSetting('mode', ...).
  setMode(mode) {
    this.paintMode();
    if (!this.player) return;
    this.player.pause();
    this.el.reader.classList.toggle('mode-text', mode === 'text');
    this.ctxBlock = -1;
    this.chipBlock = -1;
    this.cardKey = '';
    if (mode === 'text' && !this.ftEls) this.renderFullText();
    this.player.setChunk(mode === 'rsvp' ? settings.chunk : 1); // re-renders the current frame
    if (mode === 'rsvp') requestAnimationFrame(() => this.layoutStage());
    else this.el.text.focus({ preventScroll: true });
  }

  rebuildStream(focusSection) {
    if (!this.paper || !this.player) return;
    const pos = positionAt(this.stream, this.player.index);
    this.player.pause();
    this.stream = buildStream(this.paper, { includeSkipped: settings.includeSkipped });
    this.ftEls = null;
    this.ctxBlock = -1;
    this.chipBlock = -1;
    this.cardKey = '';
    this.renderOutline();
    this.renderTicks();
    if (settings.mode === 'text') this.renderFullText();
    let index = indexAt(this.stream, pos.block, pos.word);
    if (focusSection) {
      const row = this.sectionRows.find((r) => r.id === focusSection);
      if (row && row.start >= 0) index = row.start;
    }
    this.player.load(this.stream, index);
  }

  openPdf() {
    window.open(this.el.pdf.href, '_blank', 'noopener');
  }

  // ---------------------------------------------------------------- drawers, popovers, help

  toggleDrawer(which) {
    const r = this.el.reader;
    const cls = `open-${which}`;
    const open = !r.classList.contains(cls);
    // The figures panel is docked beside the reader on wide windows, so it can stay open with the contents list.
    const narrow = matchMedia('(max-width: 760px)').matches;
    r.classList.remove(cls);
    if (narrow || which === 'outline') r.classList.remove('open-outline');
    if (narrow) r.classList.remove('open-figures');
    if (open) {
      r.classList.add(cls);
      if (which === 'outline') this.renderOutline();
      const active = this.root.querySelector(which === 'outline' ? '.toc li.active' : '.fig-card');
      active?.scrollIntoView({ block: 'nearest' });
    }
  }

  closeDrawers() {
    if (document.activeElement?.closest?.('.drawer')) document.activeElement.blur();
    this.el.reader.classList.remove('open-outline', 'open-figures');
  }

  toggleSettings(force) {
    const pop = this.el.settingsPop;
    const open = force ?? pop.hidden;
    if (open) this.renderSettings();
    pop.hidden = !open;
    this.el.settingsBtn.setAttribute('aria-expanded', String(open));
  }

  renderSettings() {
    this.el.settingsPop.innerHTML = `<div class="pop-title">Reading settings</div>` + SETTINGS_UI.map((row) => {
      if (row.toggle) {
        return `<label class="switch"><input type="checkbox" data-setting="${row.key}" ${settings[row.key] ? 'checked' : ''}><span>${esc(row.label)}</span></label>`;
      }
      return `<div class="set-row"><span class="set-label">${esc(row.label)}</span><span class="seg" role="radiogroup" aria-label="${esc(row.label)}">${row.options.map(([v, l]) =>
        `<button role="radio" aria-checked="${settings[row.key] === v}" data-setting="${row.key}" data-value="${v}" class="${settings[row.key] === v ? 'on' : ''}">${esc(l)}</button>`).join('')}</span></div>`;
    }).join('');
  }

  toggleHelp(force) {
    const m = this.el.help;
    const open = force ?? m.hidden;
    if (open) {
      m.innerHTML = `<div class="modal-card"><div class="modal-head"><h2>Keyboard shortcuts</h2><button class="icon-btn" data-act="close-help" aria-label="Close">${icon('x')}</button></div>
        <dl class="keys">${KEYS.map(([k, d]) => `<dt>${k.split(' ').map((x) => `<kbd>${esc(x)}</kbd>`).join(' ')}</dt><dd>${esc(d)}</dd>`).join('')}</dl>
        <p class="modal-foot">Click any word in the paragraph under the stage to jump there. Click the stage to play or pause.</p></div>`;
    }
    m.hidden = !open;
  }

  closeTopOverlay() {
    if (this.note) { this.closeNote(); return true; }
    if (this.lb) { this.closeLightbox(); return true; }
    if (!this.el.help.hidden) { this.toggleHelp(false); return true; }
    if (!this.el.settingsPop.hidden) { this.toggleSettings(false); return true; }
    const r = this.el.reader;
    if (r.classList.contains('open-outline') || r.classList.contains('open-figures')) { this.closeDrawers(); return true; }
    return false;
  }

  // ---------------------------------------------------------------- lightbox

  openLightbox(assetId, { resume, note = '' } = {}) {
    const assets = this.paper?.assets || [];
    const idx = assets.findIndex((a) => a.id === assetId);
    if (idx < 0) return;
    const wasPlaying = !!this.player?.playing;
    this.player?.pause();
    this.lb = { idx, resume: resume ?? wasPlaying, note, scale: 1, x: 0, y: 0, fit: 1, w: 0, h: 0 };
    const box = this.el.lightbox;
    box.innerHTML = `
      <div class="lb-bar">
        <div class="lb-title"><b class="lb-label"></b><span class="lb-count"></span><span class="lb-note"></span></div>
        <div class="lb-tools">
          <button class="icon-btn" data-lb="out" title="Zoom out (−)" aria-label="Zoom out">${icon('zoomOut')}</button>
          <button class="icon-btn" data-lb="fit" title="Fit (0)" aria-label="Fit to screen">${icon('fit')}</button>
          <button class="icon-btn" data-lb="in" title="Zoom in (+)" aria-label="Zoom in">${icon('zoomIn')}</button>
          <a class="icon-btn lb-pdf" target="_blank" rel="noopener" title="Open this page of the PDF">${icon('pdf')}<span></span></a>
          <button class="btn small" data-lb="mention" title="Jump to where the text first mentions it">Go to mention</button>
          <button class="icon-btn" data-lb="close" title="Close (Esc)" aria-label="Close">${icon('x')}</button>
        </div>
      </div>
      <div class="lb-canvas"><img class="lb-img" draggable="false" alt=""></div>
      <button class="lb-nav prev" data-lb="prev" aria-label="Previous figure">${icon('chevL', 28)}</button>
      <button class="lb-nav next" data-lb="next" aria-label="Next figure">${icon('chevR', 28)}</button>
      <div class="lb-caption"></div>`;
    box.hidden = false;
    this.el.reader.classList.add('lb-open');
    this.showLightboxAsset(idx);
  }

  showLightboxAsset(idx) {
    const assets = this.paper.assets;
    const lb = this.lb;
    lb.idx = (idx + assets.length) % assets.length;
    const a = assets[lb.idx];
    const box = this.el.lightbox;
    box.querySelector('.lb-label').textContent = a.label || a.id;
    box.querySelector('.lb-count').textContent = assets.length > 1 ? `${lb.idx + 1} / ${assets.length}` : '';
    box.querySelector('.lb-note').textContent = lb.note || '';
    box.querySelector('.lb-caption').innerHTML = `<b>${esc(a.label)}.</b> ${esc(a.caption || '')}`;
    const pdf = box.querySelector('.lb-pdf');
    pdf.href = api.pdfUrl(this.id, a.page);
    pdf.querySelector('span').textContent = a.page ? `p.${a.page}` : '';
    box.querySelector('[data-lb="mention"]').hidden = !(a.id in this.stream.mentionIndex);
    box.querySelectorAll('.lb-nav').forEach((b) => { b.hidden = assets.length < 2; });
    const img = box.querySelector('.lb-img');
    img.alt = a.label || '';
    lb.w = a.width || 0;
    lb.h = a.height || 0;
    img.onload = () => { lb.w = img.naturalWidth; lb.h = img.naturalHeight; this.lbFit(); };
    img.src = api.assetUrl(this.id, a.image);
    if (lb.w && lb.h) this.lbFit();
  }

  lbFit() {
    const lb = this.lb;
    if (!lb || !lb.w) return;
    const c = this.el.lightbox.querySelector('.lb-canvas');
    const cw = c.clientWidth;
    const ch = c.clientHeight;
    lb.fit = Math.min(cw / lb.w, ch / lb.h, 2);
    lb.scale = lb.fit;
    lb.x = (cw - lb.w * lb.scale) / 2;
    lb.y = (ch - lb.h * lb.scale) / 2;
    this.lbApply();
  }

  lbZoom(factor, px, py) {
    const lb = this.lb;
    if (!lb || !lb.w) return;
    const c = this.el.lightbox.querySelector('.lb-canvas');
    if (px === undefined) { px = c.clientWidth / 2; py = c.clientHeight / 2; }
    const next = Math.max(lb.fit * 0.5, Math.min(Math.max(8, lb.fit * 2), lb.scale * factor));
    lb.x = px - (px - lb.x) * (next / lb.scale);
    lb.y = py - (py - lb.y) * (next / lb.scale);
    lb.scale = next;
    this.lbApply();
  }

  lbApply() {
    const lb = this.lb;
    const img = this.el.lightbox.querySelector('.lb-img');
    img.style.width = `${lb.w}px`;
    img.style.height = `${lb.h}px`;
    img.style.transform = `translate(${lb.x}px, ${lb.y}px) scale(${lb.scale})`;
    this.el.lightbox.classList.toggle('zoomed', lb.scale > lb.fit * 1.02);
  }

  closeLightbox({ resume } = {}) {
    if (!this.lb) return;
    const shouldResume = resume ?? this.lb.resume;
    this.lb = null;
    this.el.lightbox.hidden = true;
    this.el.lightbox.innerHTML = '';
    this.el.reader.classList.remove('lb-open');
    if (shouldResume) setTimeout(() => { if (!this.lb && !this.destroyed) this.play({ rewind: false }); }, 250);
  }

  lbAction(act) {
    const lb = this.lb;
    if (!lb) return;
    if (act === 'close') this.closeLightbox();
    else if (act === 'prev') this.showLightboxAsset(lb.idx - 1);
    else if (act === 'next') this.showLightboxAsset(lb.idx + 1);
    else if (act === 'in') this.lbZoom(1.4);
    else if (act === 'out') this.lbZoom(1 / 1.4);
    else if (act === 'fit') this.lbFit();
    else if (act === 'mention') {
      const id = this.paper.assets[lb.idx].id;
      const i = this.stream.mentionIndex[id];
      this.closeLightbox({ resume: false });
      if (i !== undefined) { this.jump(sentenceStartAt(this.stream, i)); this.closeDrawers(); }
    }
  }

  bindLightbox() {
    const box = this.el.lightbox;
    let drag = null;
    this.on(box, 'click', (e) => {
      const b = e.target.closest('[data-lb]');
      if (b) { this.lbAction(b.dataset.lb); return; }
      if (e.target === box || e.target.classList.contains('lb-canvas')) {
        if (!drag?.moved) this.closeLightbox();
      }
    });
    this.on(box, 'wheel', (e) => {
      if (!this.lb || !e.target.closest('.lb-canvas')) return;
      e.preventDefault();
      const c = box.querySelector('.lb-canvas').getBoundingClientRect();
      // a notched mouse wheel sends big whole-number steps; a trackpad sends small fractional ones
      const mouseWheel = e.deltaMode === 1 || (e.deltaX === 0 && Math.abs(e.deltaY) >= 50 && Number.isInteger(e.deltaY));
      if (e.ctrlKey || e.metaKey) {
        this.lbZoom(Math.exp(-e.deltaY * 0.01), e.clientX - c.left, e.clientY - c.top); // trackpad pinch
      } else if (mouseWheel) {
        this.lbZoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX - c.left, e.clientY - c.top);
      } else if (this.lb.scale > this.lb.fit * 1.02) {
        this.lb.x -= e.deltaX; // two-finger scroll pans a zoomed figure
        this.lb.y -= e.deltaY;
        this.lbApply();
      }
    }, { passive: false });
    this.on(box, 'dblclick', (e) => {
      if (!this.lb || !e.target.closest('.lb-canvas')) return;
      const c = box.querySelector('.lb-canvas').getBoundingClientRect();
      if (this.lb.scale > this.lb.fit * 1.05) this.lbFit();
      else this.lbZoom(2.5, e.clientX - c.left, e.clientY - c.top);
    });
    this.on(box, 'pointerdown', (e) => {
      if (!this.lb || !e.target.closest('.lb-canvas') || e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, ox: this.lb.x, oy: this.lb.y, moved: false, id: e.pointerId };
      box.setPointerCapture?.(e.pointerId);
      box.classList.add('dragging');
    });
    this.on(box, 'pointermove', (e) => {
      if (!drag || !this.lb) return;
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      this.lb.x = drag.ox + dx;
      this.lb.y = drag.oy + dy;
      this.lbApply();
    });
    const end = () => {
      box.classList.remove('dragging');
      setTimeout(() => { drag = null; }, 0); // let the click handler see drag.moved
    };
    this.on(box, 'pointerup', end);
    this.on(box, 'pointercancel', end);
  }

  lbKey(e) {
    const k = e.key;
    const map = {
      Escape: 'close', ' ': 'close', Enter: 'close', ArrowLeft: 'prev', ArrowRight: 'next',
      '+': 'in', '=': 'in', '-': 'out', _: 'out', 0: 'fit', f: 'close', F: 'close',
    };
    const act = map[k];
    if (!act) return;
    e.preventDefault();
    this.lbAction(act);
  }

  // ---------------------------------------------------------------- scrubbing

  bindScrub() {
    const bar = this.el.scrub;
    let drag = null;
    const fractionAt = (x) => {
      const r = bar.getBoundingClientRect();
      return Math.max(0, Math.min(1, (x - r.left) / r.width));
    };
    const tipAt = (x) => {
      if (!this.stream?.items.length) return;
      const f = fractionAt(x);
      const i = indexAtFraction(this.stream, f);
      const it = this.stream.items[i];
      const tip = this.el.tip;
      tip.textContent = `${this.sectionTitle(it.section) || '—'} · ${Math.round(f * 100)}%`;
      tip.hidden = false;
      const r = bar.getBoundingClientRect();
      const w = tip.offsetWidth;
      tip.style.left = `${Math.max(0, Math.min(r.width - w, x - r.left - w / 2))}px`;
    };
    this.on(bar, 'pointerdown', (e) => {
      if (!this.player || e.button !== 0) return;
      drag = { wasPlaying: this.player.playing };
      this.player.pause();
      bar.setPointerCapture?.(e.pointerId);
      bar.classList.add('dragging');
      this.jump(indexAtFraction(this.stream, fractionAt(e.clientX)));
      tipAt(e.clientX);
    });
    this.on(bar, 'pointermove', (e) => {
      tipAt(e.clientX);
      if (drag) this.jump(indexAtFraction(this.stream, fractionAt(e.clientX)));
    });
    const end = () => {
      if (!drag) return;
      const resume = drag.wasPlaying;
      drag = null;
      bar.classList.remove('dragging');
      // land on a sentence start so playback resumes somewhere sensible
      this.jump(sentenceStartAt(this.stream, this.player.index));
      if (resume) this.play({ rewind: false });
    };
    this.on(bar, 'pointerup', end);
    this.on(bar, 'pointercancel', end);
    this.on(bar, 'pointerleave', () => { if (!drag) this.el.tip.hidden = true; });
  }

  // ---------------------------------------------------------------- events

  bindEvents() {
    const R = this.el.reader;
    this.bindLightbox();
    this.bindScrub();

    this.on(R, 'click', (e) => {
      const t = e.target;
      // clicks outside an open popover / on the help backdrop just close it
      if (!this.el.settingsPop.hidden && !t.closest('.settings-pop, [data-act="settings"]')) {
        this.toggleSettings(false);
        if (t.closest('.stage')) return;
      }
      if (!this.el.help.hidden && t === this.el.help) { this.toggleHelp(false); return; }
      const act = t.closest('[data-act]')?.dataset.act;
      const assetEl = t.closest('[data-asset]');
      if (act) {
        const fn = {
          play: () => this.toggle(),
          prev: () => this.stepSentence(-1),
          next: () => this.stepSentence(1),
          outline: () => this.toggleDrawer('outline'),
          figures: () => { if (this.el.reader.classList.contains('open-figures')) this.el.reader.classList.remove('open-figures'); else this.togglePanelCol(this.figCols()[0]); },
          'close-drawers': () => this.closeDrawers(),
          'close-figures': () => this.el.reader.classList.remove('open-figures'),
          mode: () => setSetting('mode', settings.mode === 'rsvp' ? 'text' : 'rsvp'),
          theme: () => { toggleTheme(); },
          settings: () => this.toggleSettings(),
          help: () => this.toggleHelp(),
          'close-help': () => this.toggleHelp(false),
          restart: () => this.jump(0),
          'read-skipped': () => {
            const first = (this.paper.sections || []).find((x) => this.stream.skippedIds.has(x.id));
            setSetting('includeSkipped', true);
            this.rebuildStream(first?.id);
          },
        }[act];
        if (fn) { e.preventDefault(); fn(); return; }
      }
      const mark = t.closest('.mark');
      if (mark) {
        const a = (this.annots || []).find((x) => x.id === mark.dataset.ann);
        const i = a ? this.annotIndex(a) : -1;
        if (i < 0) { toast('That sentence is in a skipped section'); return; }
        this.jump(i);
        if (t.closest('[data-act2="edit-note"]')) this.openNote();
        return;
      }
      const np = t.closest('[data-np]');
      if (np) { ({ save: () => this.saveNote(), cancel: () => this.closeNote(), remove: () => this.removeNote() })[np.dataset.np]?.(); return; }
      const figTab = t.closest('[data-figtab]');
      if (figTab) { this.togglePanelCol(figTab.dataset.figtab); figTab.blur(); return; }
      if (assetEl && !t.closest('.lightbox')) {
        e.preventDefault();
        this.openLightbox(assetEl.dataset.asset);
        return;
      }
      const sec = t.closest('.toc li');
      if (sec) {
        const row = this.sectionRows.find((r) => r.id === sec.dataset.sec);
        if (!row) return;
        if (row.start < 0 && row.skipped) {
          setSetting('includeSkipped', true);
          this.rebuildStream(row.id);
          toast('Including references & appendix');
        } else if (row.start >= 0) this.jump(row.start);
        if (matchMedia('(max-width: 760px)').matches) this.closeDrawers();
        return;
      }
      const word = t.closest('.ctx-text [data-i], .ft [data-i]');
      if (word) { this.jump(+word.dataset.i); return; }
      if (t.closest('a, input, label')) return;
      if (t.closest('.stage')) this.toggle();
    });

    // settings controls (segmented buttons and switches)
    this.on(R, 'click', (e) => {
      const b = e.target.closest('button[data-setting]');
      if (!b) return;
      const key = b.dataset.setting;
      const v = typeof settings[key] === 'number' ? Number(b.dataset.value) : b.dataset.value;
      setSetting(key, v);
      this.renderSettings();
    });
    this.on(R, 'change', (e) => {
      const input = e.target.closest('input[type="checkbox"][data-setting]');
      if (input) setSetting(input.dataset.setting, input.checked);
    });

    this.on(this.el.wpm, 'input', () => this.setWpm(this.el.wpm.value));
    this.on(this.el.wpm, 'change', () => this.el.wpm.blur());
    this.on(this.el.text, 'wheel', () => { this.userScrollAt = performance.now(); }, { passive: true });
    this.on(this.el.text, 'touchmove', () => { this.userScrollAt = performance.now(); }, { passive: true });

    const onNoteKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.closeNote(); }
      else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); e.stopPropagation(); this.saveNote(); }
    };
    this.el.notepad.addEventListener('keydown', onNoteKey);
    const onDown = (e) => { if (e.target.closest?.('.d-resize')) this.startFigsResize(e); };
    this.root.addEventListener('pointerdown', onDown);
    const onWin = () => this.applyFigsLayout();
    window.addEventListener('resize', onWin);
    this.disposers.push(() => { this.root.removeEventListener('pointerdown', onDown); window.removeEventListener('resize', onWin); });
    const ro = new ResizeObserver(() => { this.layoutStage(); if (this.lb) this.lbFit(); });
    ro.observe(this.el.stage);
    this.disposers.push(() => ro.disconnect());
    this.layoutStage();

    this.on(document, 'keydown', (e) => this.onKey(e));
    this.on(document, 'visibilitychange', () => {
      if (document.hidden) {
        if (this.player?.playing) { this.player.pause(); this.pausedByHide = true; }
        this.saveNow({ keepalive: true });
      } else if (this.pausedByHide) {
        this.pausedByHide = false;
        toast('Paused while the tab was hidden — Space to continue');
      }
    });
    this.on(window, 'pagehide', () => this.saveNow({ keepalive: true }));

    const off = onSettingChange((key) => {
      if (this.destroyed) return;
      if (key === 'theme') this.paintTheme();
      if (key === 'figsCols' || key === 'figsWidth') this.applyFigsLayout();
      if (key === 'context') this.el.reader.classList.toggle('ctx-dim', settings.context === 'dim');
      if (key === 'font' || key === 'size') requestAnimationFrame(() => this.layoutStage());
      if (key === 'chunk' && settings.mode === 'rsvp') this.player?.setChunk(settings.chunk);
      if (key === 'mode') this.setMode(settings.mode);
      if (key === 'includeSkipped') { this.el.skipToggle.checked = settings.includeSkipped; this.rebuildStream(); }
      if (key === 'assetFlow' && this.player) { this.cardKey = ''; this.renderStage('state'); }
      if (!this.el.settingsPop.hidden) this.renderSettings();
    });
    this.disposers.push(off);
  }

  onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;
    if (this.lb) { this.lbKey(e); return; }
    const k = e.key;
    if (k === 'Escape') { if (this.closeTopOverlay()) e.preventDefault(); return; }
    if (k === '?') { e.preventDefault(); this.toggleHelp(); return; }
    if (!this.player) return;
    const focused = document.activeElement;
    const onControl = focused && focused !== document.body && /^(BUTTON|A)$/.test(focused.tagName);
    if (k === 'Enter' && onControl) return; // let the focused button do its thing
    const actions = {
      ' ': () => this.toggle(),
      ArrowLeft: () => (e.shiftKey ? this.stepWord(-1) : this.stepSentence(-1)),
      ArrowRight: () => (e.shiftKey ? this.stepWord(1) : this.stepSentence(1)),
      ArrowUp: () => this.setWpm(settings.wpm + WPM_STEP),
      ArrowDown: () => this.setWpm(settings.wpm - WPM_STEP),
      '[': () => this.stepSection(-1),
      ']': () => this.stepSection(1),
      Home: () => this.jump(0),
      Enter: () => { const id = this.activeAssetId(); if (id) this.openLightbox(id); },
      f: () => this.togglePanelCol('figure'),
      t: () => this.togglePanelCol('table'),
      z: () => this.togglePanelCol('note'),
      h: () => this.toggleHighlight(),
      n: () => this.openNote(),
      c: () => this.toggleDrawer('outline'),
      o: () => this.toggleDrawer('outline'),
      m: () => setSetting('mode', settings.mode === 'rsvp' ? 'text' : 'rsvp'),
      1: () => setSetting('chunk', 1),
      2: () => setSetting('chunk', 2),
      3: () => setSetting('chunk', 3),
      p: () => this.openPdf(),
      d: () => toggleTheme(),
    };
    const fn = actions[k.length === 1 ? k.toLowerCase() : k];
    if (!fn) return;
    e.preventDefault();
    if (onControl || focused?.type === 'range') focused.blur(); // so Space doesn't also "click" it
    fn();
  }

  // ---------------------------------------------------------------- progress saving

  saveKey() {
    if (!this.player || !this.stream?.items.length) return '';
    const pos = positionAt(this.stream, this.player.index);
    return `${pos.block}:${pos.word}:${settings.wpm}:${this.player.state === 'ended'}`;
  }

  saveSoon() {
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.saveNow(), 800);
  }

  async saveNow({ keepalive = false } = {}) {
    clearTimeout(this.saveTimer);
    if (!this.player || !this.stream?.items.length) return;
    if (this.cover && !this.hadProgress) return; // opened but never started: don't create progress
    const key = this.saveKey();
    if (key === this.lastSavedKey) return;
    this.lastSavedKey = key;
    this.lastSaveAt = performance.now();
    const pos = positionAt(this.stream, this.player.index);
    const percent = this.player.state === 'ended' ? 100 : Math.round(percentAt(this.stream, this.player.index) * 10) / 10;
    const body = { block: pos.block, word: pos.word, wpm: settings.wpm, percent, updated: new Date().toISOString() };
    try {
      await api.putProgress(this.id, body, { keepalive });
      this.hadProgress = true;
      this.saveFailed = false;
    } catch (e) {
      this.lastSavedKey = '';
      if (!this.saveFailed) toast(`Couldn't save your place: ${e.message}`, { kind: 'error' });
      this.saveFailed = true;
    }
  }

  destroy() {
    this.destroyed = true;
    clearTimeout(this.retryTimer);
    this.saveNow({ keepalive: true });
    this.player?.destroy();
    for (const d of this.disposers) d();
    this.disposers = [];
  }
}

// Sentence highlights are painted whenever the paragraph under the stage or the full text is rebuilt.
const _renderContext = Reader.prototype.renderContext;
Reader.prototype.renderContext = function renderContext(frame) {
  const before = this.ctxBlock;
  _renderContext.call(this, frame);
  if (this.ctxBlock !== before) this.paintHl(this.el.ctxText);
  this.el.stage.classList.toggle('is-hl', this.hlIndex().has(frame.start));
};
const _renderFullText = Reader.prototype.renderFullText;
Reader.prototype.renderFullText = function renderFullText() {
  _renderFullText.call(this);
  this.paintHl(this.el.ft);
};
