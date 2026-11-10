// app.js — boot, hash routing (#/ and #/read/<id>), and PDF upload by drag-and-drop or picker.

import { api } from './api.js';
import { applyAppearance, toast } from './ui.js';
import { mountLibrary } from './library.js';
import { mountReader } from './reader.js';

applyAppearance();

const root = document.getElementById('app');
let view = null;

const ctx = { pickFiles };

function route() {
  view?.destroy();
  view = null;
  root.textContent = '';
  window.scrollTo(0, 0);
  const m = location.hash.match(/^#\/read\/(.+)$/);
  view = m ? mountReader(root, decodeURIComponent(m[1]), ctx) : mountLibrary(root, ctx);
}
window.addEventListener('hashchange', route);
route();

// ---------- uploads ----------

const picker = Object.assign(document.createElement('input'), { type: 'file', accept: 'application/pdf,.pdf', multiple: true, hidden: true });
document.body.append(picker);
picker.addEventListener('change', () => { upload([...picker.files]); picker.value = ''; });

function pickFiles() { picker.click(); }

async function upload(files) {
  const pdfs = files.filter((f) => f.type === 'application/pdf' || /\.pdf$/i.test(f.name));
  if (!pdfs.length) { toast('Only PDF files can be added.', { kind: 'error' }); return; }
  if (pdfs.length < files.length) toast('Skipped files that aren’t PDFs.');
  for (const f of pdfs) {
    toast(`Uploading ${f.name}…`, { timeout: 1800 });
    try {
      await api.uploadPaper(f);
      toast(`Added ${f.name} — processing`);
    } catch (e) {
      toast(`Couldn’t add ${f.name}: ${e.message}`, { kind: 'error', timeout: 6000 });
    }
    window.dispatchEvent(new CustomEvent('pace:papers-changed'));
  }
}

// Drop a PDF anywhere in the app. Outside the library it uploads in the background.
const overlay = document.getElementById('drop-overlay');
let depth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
window.addEventListener('dragenter', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  depth++;
  overlay.hidden = false;
});
window.addEventListener('dragover', (e) => { if (hasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
window.addEventListener('dragleave', (e) => {
  if (!hasFiles(e)) return;
  depth = Math.max(0, depth - 1);
  if (!depth) overlay.hidden = true;
});
window.addEventListener('drop', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  depth = 0;
  overlay.hidden = true;
  upload([...e.dataTransfer.files]);
});
