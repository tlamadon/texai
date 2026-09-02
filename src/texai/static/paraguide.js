// The paragraph rhythm guide on the page — the twin of the editor's gutter.
//
// The backend maps each blank-line-separated paragraph forward through SyncTeX
// and hands back a bar per paragraph per page, carrying the same palette index
// the editor paints (paragraph ordinal within its file, % palette). Drawing
// them here in the page margin means a block of source and the block it
// produced wear the same colour across the two panes.
//
// Like the change markers, the bars live in an overlay that every page render
// wipes, so `draw()` reruns cheaply on any scroll or zoom and `refresh()`
// refetches only when the document itself has been rebuilt.

import { getJSON } from './api.js';

const BAR_WIDTH = 4; // px: the coloured rule itself
const BAR_GAP = 12; // px to the left of the text column, so it clears the text

export class ParagraphGuide {
  constructor({ viewer }) {
    this.viewer = viewer;
    this.bars = [];
    this.palette = 5;
    this.enabled = true;
  }

  async refresh() {
    if (!this.enabled) return;
    try {
      const data = await getJSON('/api/paragraphs');
      this.bars = data.bars || [];
      this.palette = data.palette || this.palette;
    } catch {
      // A PDF mid-rebuild, or no SyncTeX yet: keep what we had rather than
      // clearing, and the next refresh after the build will replace it.
      return;
    }
    this.draw();
  }

  clear() {
    for (const entry of this.viewer.pages) {
      entry.el.querySelector('.para-layer')?.remove();
    }
  }

  /** The overlay for one page, made once and reused. */
  _layerFor(entry) {
    let layer = entry.el.querySelector('.para-layer');
    if (!layer) {
      layer = document.createElement('div');
      layer.className = 'para-layer';
      entry.el.append(layer);
    }
    return layer;
  }

  /** Re-place every bar. Cheap enough to run on any render or zoom. */
  draw() {
    this.clear();
    if (!this.enabled || !this.bars.length) return;

    for (const bar of this.bars) {
      const entry = this.viewer.pageEntry(bar.page);
      if (!entry || !entry.viewport) continue;

      // Width is a screen thickness, not a document measure, so only the box's
      // left edge and vertical extent come from the transform.
      const rect = this.viewer.pdfRectToPageRect(entry, {
        x: bar.x,
        y: bar.y,
        width: 1,
        height: bar.height,
      });

      const node = document.createElement('div');
      node.className = `para-bar p${bar.palette}`;
      node.style.left = `${rect.left - BAR_GAP}px`;
      node.style.top = `${rect.top}px`;
      node.style.width = `${BAR_WIDTH}px`;
      node.style.height = `${Math.max(rect.height, 2)}px`;
      node.title = `${bar.file} — paragraph ${bar.index + 1}`;
      this._layerFor(entry).append(node);
    }
  }
}
