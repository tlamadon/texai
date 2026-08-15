// The whole project's source, read once and kept until something writes to it.
//
// Two things need more than the file in the editor: the palette, which searches
// every heading in the project, and the document outline beside the PDF, which
// is main.tex with each \input spliced in where it appears. Both read through
// here so the project is fetched once rather than twice.
//
// Deliberately the files on disk, not the buffer: this outline navigates the
// PDF, and the PDF was built from disk. Unsaved typing is not in the document
// being read, so it should not be in the map of it either.

import { getJSON } from './api.js';
import { parseOutline } from './outline.js';

const MAX_FILES = 80;
const MAX_DEPTH = 12;

/** Turn an \input argument into a project file, the way latexmk would. */
export function resolveInput(target, from, files) {
  const cleaned = String(target).replace(/^\.\//, '').trim();
  const named = /\.[a-z]+$/i.test(cleaned) ? cleaned : `${cleaned}.tex`;
  if (files.includes(named)) return named;
  // \input resolves from the compilation directory, but a project that keeps
  // its chapters together may well mean the file next door.
  const directory = from?.includes('/') ? from.replace(/[^/]*$/, '') : '';
  const sibling = directory + named;
  return files.includes(sibling) ? sibling : named;
}

export class ProjectSources {
  constructor() {
    this.invalidate();
  }

  /** Something wrote to the project — you or the agent. */
  invalidate() {
    this.texts = new Map();
    this.listing = null;
    this._pending = null;
  }

  /** `{ files, rootTex }`, fetched once. */
  async files() {
    if (!this.listing) this.listing = await getJSON('/api/source/files');
    return this.listing;
  }

  async text(file) {
    if (this.texts.has(file)) return this.texts.get(file);
    let text = '';
    try {
      text = (await getJSON(`/api/source?file=${encodeURIComponent(file)}`)).text || '';
    } catch {
      text = ''; // a file that cannot be read simply has no headings
    }
    this.texts.set(file, text);
    return text;
  }

  /** One file's outline, at absolute depths so it can be spliced into another. */
  async outline(file) {
    return parseOutline(await this.text(file), { normalize: false });
  }

  /** Every heading in the project, whether or not the root pulls it in. */
  async everything() {
    const { files } = await this.files();
    const tex = files.filter((file) => file.endsWith('.tex')).slice(0, MAX_FILES);
    const outlines = await Promise.all(tex.map((file) => this.outline(file)));
    return new Map(tex.map((file, index) => [file, outlines[index]]));
  }

  /**
   * The document in reading order: the root .tex, with each \input expanded in
   * place. Entries carry the file they came from, and depths are normalised
   * once across the whole document rather than per file — a chapter file that
   * starts at \section has to keep sitting under the \chapter that pulled it in.
   *
   * Concurrent callers share one walk; the panel and a refresh often ask at once.
   */
  document() {
    if (!this._pending) {
      this._pending = this._walkDocument().catch((error) => {
        this._pending = null;
        throw error;
      });
    }
    return this._pending;
  }

  async _walkDocument() {
    const { files, rootTex } = await this.files();
    const root = rootTex || files.find((file) => file.endsWith('.tex'));
    if (!root) return [];

    const seen = new Set();
    const flat = [];

    const walk = async (file, depth) => {
      // A file that inputs itself, or a pair that input each other, would
      // otherwise walk until the stack gives out.
      if (depth > MAX_DEPTH || seen.has(file) || seen.size >= MAX_FILES) return;
      seen.add(file);
      for (const entry of await this.outline(file)) {
        if (entry.kind === 'input') {
          const target = resolveInput(entry.target, file, files);
          if (files.includes(target) && !seen.has(target)) {
            await walk(target, depth + 1);
            continue;
          }
          // A file that is not in the project (or is already in) still deserves
          // a line, so the outline does not silently skip a whole chapter.
        }
        flat.push({ ...entry, file });
      }
    };

    await walk(root, 0);

    const headings = flat.filter((entry) => entry.kind !== 'input');
    const shallowest = headings.length ? Math.min(...headings.map((e) => e.level)) : 0;
    return flat.map((entry) => ({ ...entry, level: Math.max(0, entry.level - shallowest) }));
  }
}
