// Where you were, before something moved the page.
//
// A link, a click in the source, a heading in the contents: each of these takes
// the document out from under the reader, and a PDF has no address bar to get
// back with. So every jump records the spot it left behind, and the two arrows
// in the toolbar walk that list.
//
// Scrolling by hand is not a jump and pushes nothing. It only changes where the
// next jump will bring you back to, which is what "back" means to a reader.

const MAX_ENTRIES = 60;

/** Same page and within a fiftieth of it: the same place, for our purposes. */
function same(a, b) {
  return !!a && !!b && a.page === b.page && Math.abs(a.offsetRatio - b.offsetRatio) < 0.02;
}

export class ViewHistory {
  constructor({ viewer, back, forward }) {
    this.viewer = viewer;
    this.els = {
      back: document.getElementById(back),
      forward: document.getElementById(forward),
    };
    this.past = [];
    this.future = [];

    this.els.back?.addEventListener('click', () => this.back());
    this.els.forward?.addEventListener('click', () => this.forward());
    this._update();
  }

  /** Remember the spot about to be left. Called before a jump, not after it. */
  mark() {
    const place = this._here();
    if (!place || same(this.past.at(-1), place)) return;
    this.past.push(place);
    if (this.past.length > MAX_ENTRIES) this.past.shift();
    // Going somewhere new ends the road forward, as it does in a browser.
    this.future.length = 0;
    this._update();
  }

  back() {
    if (!this.past.length) return false;
    const here = this._here();
    const place = this.past.pop();
    if (here) this.future.push(here);
    this._go(place);
    return true;
  }

  forward() {
    if (!this.future.length) return false;
    const here = this._here();
    const place = this.future.pop();
    if (here) this.past.push(here);
    this._go(place);
    return true;
  }

  /** A different document: the places in the old one mean nothing in it. */
  reset() {
    this.past = [];
    this.future = [];
    this._update();
  }

  _here() {
    const state = this.viewer.getViewState();
    if (!state) return null;
    return {
      page: state.page,
      offsetRatio: state.offsetRatio,
      scrollLeft: state.scrollLeft ?? 0,
    };
  }

  _go(place) {
    // Deliberately not the zoom: coming back to where you were reading should
    // not undo a zoom you chose since.
    this.viewer.scrollToPage(place.page, place.offsetRatio);
    this.viewer.container.scrollLeft = place.scrollLeft ?? 0;
    this._update();
  }

  _update() {
    const { back, forward } = this.els;
    if (back) {
      back.disabled = !this.past.length;
      back.title = this.past.length
        ? `Back to page ${this.past.at(-1).page}`
        : 'Nowhere to go back to yet';
    }
    if (forward) {
      forward.disabled = !this.future.length;
      forward.title = this.future.length
        ? `Forward to page ${this.future.at(-1).page}`
        : 'Nowhere to go forward to';
    }
  }
}
