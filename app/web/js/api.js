// api.js — talks to the local server (/api), or to an in-browser mock built
// from web/mock/ when the page is opened with ?mock=1.

export const isMock = new URLSearchParams(location.search).has('mock');

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status; // 0 = server unreachable
  }
}

const enc = encodeURIComponent;

class HttpApi {
  async _req(method, path, { body, json = true, keepalive = false } = {}) {
    let res;
    try {
      res = await fetch(`/api${path}`, {
        method,
        body,
        keepalive,
        headers: body && !(body instanceof FormData) ? { 'Content-Type': 'application/json' } : undefined,
      });
    } catch (e) {
      throw new ApiError(0, 'The reading server is not reachable.');
    }
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { const j = await res.json(); if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch { /* not JSON */ }
      throw new ApiError(res.status, detail);
    }
    if (!json || res.status === 204) return null;
    return res.json();
  }

  listPapers() { return this._req('GET', '/papers'); }
  getPaper(id) { return this._req('GET', `/papers/${enc(id)}`); }
  getProgress(id) { return this._req('GET', `/papers/${enc(id)}/progress`); }
  putProgress(id, progress, { keepalive = false } = {}) {
    return this._req('PUT', `/papers/${enc(id)}/progress`, { body: JSON.stringify(progress), keepalive });
  }
  uploadPaper(file) {
    const fd = new FormData();
    fd.append('file', file, file.name);
    return this._req('POST', '/papers', { body: fd });
  }
  deletePaper(id) { return this._req('DELETE', `/papers/${enc(id)}`, { json: false }); }
  reingest(id) { return this._req('POST', `/papers/${enc(id)}/reingest`); }
  listAnnotations(id) { return this._req('GET', `/papers/${enc(id)}/annotations`); }
  createAnnotation(id, ann) { return this._req('POST', `/papers/${enc(id)}/annotations`, { body: JSON.stringify(ann) }); }
  updateAnnotation(id, aid, note) { return this._req('PUT', `/papers/${enc(id)}/annotations/${enc(aid)}`, { body: JSON.stringify({ note }) }); }
  deleteAnnotation(id, aid) { return this._req('DELETE', `/papers/${enc(id)}/annotations/${enc(aid)}`, { json: false }); }
  assetUrl(id, file) { return `/api/papers/${enc(id)}/assets/${enc(file)}`; }
  pdfUrl(id, page) { return `/api/papers/${enc(id)}/pdf${page ? `#page=${page}` : ''}`; }
}

// ---------------------------------------------------------------------------
// Mock: one real-looking fixture paper, a paper that fails, and a paper that is
// "processing" for a few seconds after load. Uploads become ready after ~4 s.
// Progress is kept in localStorage so resume works across reloads.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (x) => JSON.parse(JSON.stringify(x));
const MOCK_PROGRESS_KEY = 'pace.mock.progress.';

function lsGet(key) {
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}
function lsSet(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
}

class MockApi {
  constructor() {
    this.base = null;
    this.entries = null; // [{summary, readyAt?, titleWhenReady?}]
  }

  async _init() {
    if (this.entries) return;
    const res = await fetch('mock/paper.json');
    if (!res.ok) throw new ApiError(res.status, 'Mock fixture web/mock/paper.json is missing.');
    this.base = await res.json();
    const start = Date.now();
    const { sections, blocks, assets, ...summary } = this.base;
    this.entries = [
      {
        summary: {
          ...summary, id: 'mock-inbox-demo-91ac0e', title: 'transformers_in_context_2401.pdf', authors: [], year: null,
          source_filename: 'transformers_in_context_2401.pdf', added: new Date(start).toISOString(), status: 'processing',
          page_count: 0, word_count: 0, num_figures: 0, num_tables: 0,
        },
        readyAt: start + 7000,
        whenReady: { ...summary, title: `${summary.title} (inbox copy)` },
      },
      { summary: { ...summary } },
      {
        summary: {
          id: 'mock-scanned-error-0b77d1', title: 'scan_0042.pdf', authors: [], year: null, source_filename: 'scan_0042.pdf',
          added: '2026-10-28T09:12:00Z', status: 'error', page_count: 31, word_count: 0, num_figures: 0, num_tables: 0, progress: null,
          error: 'No extractable text: this PDF looks like a scanned image (OCR is not supported yet).',
        },
      },
    ];
  }

  _tick() {
    const now = Date.now();
    for (const e of this.entries) {
      if (e.summary.status === 'processing' && e.readyAt && now >= e.readyAt) {
        const { id, added, source_filename } = e.summary;
        e.summary = { status: 'ready', error: null, ...clone(e.whenReady), id, added, source_filename };
        e.readyAt = null;
      }
    }
  }

  _find(id) {
    const e = this.entries.find((x) => x.summary.id === id);
    if (!e) throw new ApiError(404, 'Paper not found');
    return e;
  }

  async listPapers() {
    await this._init();
    await sleep(80);
    this._tick();
    return this.entries
      .map((e) => ({ ...clone(e.summary), progress: e.summary.status === 'ready' ? lsGet(MOCK_PROGRESS_KEY + e.summary.id) : null }))
      .sort((a, b) => String(b.added).localeCompare(String(a.added)));
  }

  async getPaper(id) {
    await this._init();
    await sleep(120);
    this._tick();
    const e = this._find(id);
    if (e.summary.status !== 'ready') throw new ApiError(404, 'Paper is not ready yet');
    return { ...clone(this.base), ...clone(e.summary), progress: lsGet(MOCK_PROGRESS_KEY + id) };
  }

  async getProgress(id) {
    await this._init();
    this._find(id);
    return lsGet(MOCK_PROGRESS_KEY + id) || { block: 0, word: 0, wpm: 350, percent: 0, updated: null };
  }

  async putProgress(id, progress) {
    await this._init();
    this._find(id);
    const p = { ...progress, updated: new Date().toISOString() };
    lsSet(MOCK_PROGRESS_KEY + id, p);
    return p;
  }

  async uploadPaper(file) {
    await this._init();
    await sleep(300);
    if (!/\.pdf$/i.test(file.name)) throw new ApiError(400, 'Only PDF files are supported.');
    const stem = file.name.replace(/\.pdf$/i, '');
    const id = `${stem.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40)}-${Math.random().toString(16).slice(2, 8)}`;
    const { sections, blocks, assets, ...summary } = this.base;
    const entry = {
      summary: {
        ...summary, id, title: file.name, authors: [], year: null, source_filename: file.name,
        added: new Date().toISOString(), status: 'processing', page_count: 0, word_count: 0, num_figures: 0, num_tables: 0, progress: null,
      },
      readyAt: Date.now() + 4000,
      whenReady: { ...summary, title: `${stem} (demo: shows the fixture paper)` },
    };
    this.entries.push(entry);
    return clone(entry.summary);
  }

  async deletePaper(id) {
    await this._init();
    await sleep(80);
    this._find(id);
    this.entries = this.entries.filter((e) => e.summary.id !== id);
    try { localStorage.removeItem(MOCK_PROGRESS_KEY + id); } catch { /* ignore */ }
    return null;
  }

  async reingest(id) {
    await this._init();
    const e = this._find(id);
    // the mock "error" paper fails again after a retry, to show the error state round-trips
    e.whenReady = { ...e.summary, status: 'error', error: 'Still no extractable text (this is the mock error paper).' };
    e.summary = { ...e.summary, status: 'processing', error: null };
    e.readyAt = Date.now() + 3000;
    return clone(e.summary);
  }

  // highlights live in memory only in the demo
  async listAnnotations(id) { return clone((this.annots ||= {})[id] || []); }
  async createAnnotation(id, ann) {
    const list = ((this.annots ||= {})[id] ||= []);
    const a = { id: Math.random().toString(16).slice(2, 14), note: '', in_pdf: false, created: new Date().toISOString(), ...ann };
    list.push(a);
    return clone(a);
  }
  async updateAnnotation(id, aid, note) {
    const a = ((this.annots ||= {})[id] || []).find((x) => x.id === aid);
    if (!a) throw new ApiError(404, 'No such highlight');
    a.note = note;
    return clone(a);
  }
  async deleteAnnotation(id, aid) {
    const all = (this.annots ||= {});
    all[id] = (all[id] || []).filter((x) => x.id !== aid);
    return null;
  }
  assetUrl(_id, file) { return `mock/assets/${enc(file)}`; }
  pdfUrl(_id, page) { return `mock/original.pdf${page ? `#page=${page}` : ''}`; }
}

export const api = isMock ? new MockApi() : new HttpApi();
