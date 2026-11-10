// library.js — the list of papers: upload, processing/error states, delete, resume.

import { api, isMock } from './api.js';
import { estimateMinutes, formatDuration } from './core.js';
import { settings, esc, icon, toast, toggleTheme, effectiveTheme, authorsLine, onSettingChange } from './ui.js';

export function mountLibrary(root, ctx) {
  let papers = null;     // last successful list
  let failure = null;    // ApiError when the server is unreachable / failing
  let lastJson = '';
  let confirming = null; // id of the paper whose delete is being confirmed
  let pollTimer = null;
  let destroyed = false;

  document.title = 'Reading tool — Library';
  root.innerHTML = `
    <div class="library">
      <header class="lib-head">
        <div class="brand"><span class="brand-dot" aria-hidden="true"></span>Reading tool
          ${isMock ? '<span class="tag" title="Loaded with ?mock=1 — sample data, nothing is saved to the server">demo data</span>' : ''}
        </div>
        <div class="lib-actions">
          <button class="btn" data-act="upload">${icon('upload', 18)}<span>Add PDF</span></button>
          <button class="icon-btn" data-act="theme" title="Light / dark (T)" aria-label="Toggle light or dark theme"></button>
        </div>
      </header>
      <p class="lib-hint">Drop PDFs anywhere on this page, or put them in <code>papers/inbox</code> — they show up here on their own.</p>
      <ul class="papers" aria-live="polite"></ul>
    </div>`;
  const list = root.querySelector('.papers');
  const themeBtn = root.querySelector('[data-act="theme"]');
  const paintTheme = () => { themeBtn.innerHTML = icon(effectiveTheme() === 'dark' ? 'sun' : 'moon'); };
  paintTheme();

  root.querySelector('[data-act="upload"]').addEventListener('click', () => ctx.pickFiles());
  themeBtn.addEventListener('click', () => { toggleTheme(); paintTheme(); });

  function lastReadId(ps) {
    let best = null;
    for (const p of ps) {
      const u = p.progress?.updated;
      if (u && (p.progress.percent || 0) > 0 && (!best || u > best.progress.updated)) best = p;
    }
    return best?.id ?? null;
  }

  function paperRow(p, lastId) {
    const ready = p.status === 'ready';
    const title = esc(p.title || p.source_filename || p.id);
    const byline = [authorsLine(p.authors), p.year].filter(Boolean).map(esc).join(' · ');
    const assets = (p.num_figures || 0) + (p.num_tables || 0);
    const minutes = estimateMinutes(p.word_count, settings.wpm, assets);
    const pct = Math.max(0, Math.min(100, Number(p.progress?.percent) || 0));
    const meta = ready ? [
      p.page_count ? `${p.page_count} pages` : '',
      minutes ? `${formatDuration(minutes * 60000)} at ${settings.wpm} wpm` : '',
      p.num_figures ? `${p.num_figures} figure${p.num_figures === 1 ? '' : 's'}` : '',
      p.num_tables ? `${p.num_tables} table${p.num_tables === 1 ? '' : 's'}` : '',
    ].filter(Boolean).join(' · ') : '';

    let status = '';
    if (p.status === 'processing') {
      status = `<div class="paper-status"><span class="spinner" aria-hidden="true"></span>Processing — extracting text, figures and tables…</div>`;
    } else if (p.status === 'error') {
      status = `<div class="paper-status error">Couldn't process this PDF${p.error ? `: ${esc(p.error)}` : '.'}</div>`;
    } else if (pct > 0) {
      const left = pct >= 99.5 ? 'finished' : `${formatDuration(minutes * 60000 * (1 - pct / 100))} left`;
      status = `<div class="paper-progress"><div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div><span>${Math.round(pct)}% · ${left}</span></div>`;
    }

    const confirm = confirming === p.id ? `
      <div class="confirm" role="alertdialog" aria-label="Confirm delete">
        <span>Delete <b>${title}</b>? The PDF, extracted figures and your progress are removed.</span>
        <span class="confirm-btns"><button class="btn" data-act="cancel">Cancel</button><button class="btn danger" data-act="confirm-delete">Delete</button></span>
      </div>` : '';

    return `
      <li class="paper ${p.status}${confirming === p.id ? ' confirming' : ''}" data-id="${esc(p.id)}">
        <${ready ? 'a' : 'div'} class="paper-main" ${ready ? `href="#/read/${encodeURIComponent(p.id)}"` : ''}>
          <div class="paper-title">${title}${p.id === lastId ? '<span class="tag">last read</span>' : ''}</div>
          ${byline ? `<div class="paper-byline">${byline}</div>` : ''}
          ${meta ? `<div class="paper-meta">${meta}</div>` : ''}
          ${status}
        </${ready ? 'a' : 'div'}>
        <div class="paper-actions">
          ${p.status === 'error' ? `<button class="icon-btn" data-act="retry" title="Try again" aria-label="Try processing again">${icon('retry', 18)}</button>` : ''}
          <button class="icon-btn" data-act="delete" title="Delete" aria-label="Delete paper">${icon('trash', 18)}</button>
        </div>
        ${confirm}
      </li>`;
  }

  function render() {
    if (failure && !papers) {
      const offline = failure.status === 0;
      list.innerHTML = `
        <li class="lib-empty problem">
          <h2>${offline ? 'The reading server isn’t running' : 'Something went wrong'}</h2>
          <p>${offline ? 'Start it by double-clicking <code>start.command</code> in the reading_tool folder. This page reconnects by itself.' : esc(failure.message)}</p>
          ${isMock ? '' : '<p><a href="?mock=1#/">Or look around with a demo paper →</a></p>'}
        </li>`;
      return;
    }
    if (!papers) { list.innerHTML = '<li class="lib-empty"><span class="spinner"></span></li>'; return; }
    if (!papers.length) {
      list.innerHTML = `
        <li class="lib-empty dropzone" data-act="upload" tabindex="0" role="button">
          ${icon('upload', 32)}
          <h2>Add your first paper</h2>
          <p>Drop a PDF here, or click to choose one.</p>
        </li>`;
      return;
    }
    const lastId = lastReadId(papers);
    list.innerHTML = papers.map((p) => paperRow(p, lastId)).join('');
    if (failure) list.insertAdjacentHTML('afterbegin', '<li class="lib-banner">Lost the connection to the reading server — retrying…</li>');
  }

  async function refresh() {
    try {
      const next = await api.listPapers();
      if (destroyed) return;
      failure = null;
      const json = JSON.stringify(next);
      if (json !== lastJson) {
        const wasProcessing = new Set((papers || []).filter((p) => p.status === 'processing').map((p) => p.id));
        for (const p of next) if (wasProcessing.has(p.id) && p.status === 'ready') toast(`Ready: ${p.title}`);
        lastJson = json;
        papers = next;
        render();
      }
    } catch (e) {
      if (destroyed) return;
      const changed = !failure;
      failure = e;
      if (changed) render();
    }
    schedule();
  }

  function schedule() {
    clearTimeout(pollTimer);
    if (destroyed) return;
    const busy = papers?.some((p) => p.status === 'processing');
    // processing papers are polled briskly; otherwise a slow poll picks up inbox drops
    pollTimer = setTimeout(() => (document.hidden ? schedule() : refresh()), busy ? 1500 : failure ? 3000 : 5000);
  }

  list.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.closest('[data-id]')?.dataset.id;
    const act = btn.dataset.act;
    if (act === 'upload') { ctx.pickFiles(); return; }
    e.preventDefault();
    if (act === 'delete') { confirming = id; render(); list.querySelector('[data-act="cancel"]')?.focus(); }
    if (act === 'cancel') { confirming = null; render(); }
    if (act === 'confirm-delete') {
      confirming = null;
      try { await api.deletePaper(id); toast('Deleted'); } catch (err) { toast(`Couldn't delete: ${err.message}`, { kind: 'error' }); }
      lastJson = '';
      refresh();
    }
    if (act === 'retry') {
      try { await api.reingest(id); } catch (err) { toast(`Couldn't retry: ${err.message}`, { kind: 'error' }); }
      lastJson = '';
      refresh();
    }
  });
  list.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('.dropzone')) { e.preventDefault(); ctx.pickFiles(); }
  });

  const onKey = (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest?.('input, textarea')) return;
    if (e.key === 'Escape' && confirming) { confirming = null; render(); }
    if (e.key === 't' || e.key === 'T') { toggleTheme(); paintTheme(); }
  };
  const onChanged = () => { lastJson = ''; refresh(); };
  const offSettings = onSettingChange((k) => { if (k === 'theme') paintTheme(); });
  const onVisible = () => { if (!document.hidden) refresh(); };
  document.addEventListener('keydown', onKey);
  window.addEventListener('pace:papers-changed', onChanged);
  document.addEventListener('visibilitychange', onVisible);

  render();
  refresh();

  return {
    destroy() {
      destroyed = true;
      clearTimeout(pollTimer);
      offSettings();
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('pace:papers-changed', onChanged);
      document.removeEventListener('visibilitychange', onVisible);
    },
  };
}
