// Reusable symbol search: debounced query, keyboard nav, click/Enter to open a
// chart. Mounted on the home page and in the instrument banner so you can jump
// straight to any symbol's chart from either place.

import { api } from "../api/client";
import type { SearchResult } from "../api/types";
import { assetClassLabel } from "../lib/format";

export interface SearchBoxOptions {
  /** Placeholder text for the input. */
  placeholder?: string;
  /** Tighter styling for the instrument banner. */
  compact?: boolean;
}

const DEFAULT_PLACEHOLDER =
  "Search any symbol — stock, future or index — to open its chart…";

/**
 * Mount a symbol search box into `host` (its contents are replaced). Renders its
 * own input + results dropdown; picking a hit navigates via the hash router.
 * Returns a teardown that clears timers and listeners.
 */
export function mountSearchBox(
  host: HTMLElement,
  opts: SearchBoxOptions = {},
): () => void {
  host.innerHTML = `
    <div class="search-bar${opts.compact ? " compact" : ""}">
      <input class="search-input" type="search" autocomplete="off"
             aria-label="Search symbols" />
      <div class="search-results" hidden></div>
    </div>
  `;
  const input = host.querySelector<HTMLInputElement>(".search-input")!;
  const box = host.querySelector<HTMLElement>(".search-results")!;
  input.placeholder = opts.placeholder ?? DEFAULT_PLACEHOLDER;

  let results: SearchResult[] = [];
  let active = -1;
  let seq = 0;
  let debounce: number | undefined;

  const close = () => {
    box.hidden = true;
    box.innerHTML = "";
    results = [];
    active = -1;
  };

  const go = (r: SearchResult) => {
    close();
    input.value = "";
    location.hash = `#/i/${r.con_id}`;
  };

  const draw = () => {
    if (!results.length) {
      box.innerHTML = `<div class="search-empty">No matches</div>`;
      box.hidden = false;
      return;
    }
    box.innerHTML = results
      .map(
        (r, i) => `
        <button type="button" class="search-hit ${i === active ? "active" : ""}" data-i="${i}">
          <span class="hit-sym mono">${escape(r.symbol)}</span>
          <span class="hit-desc">${escape(r.description || r.sec_type)}</span>
          <span class="hit-meta">${escape(assetClassLabel(r.asset_class))} · ${escape(r.exchange)}</span>
        </button>`,
      )
      .join("");
    box.hidden = false;
    box.querySelectorAll<HTMLButtonElement>(".search-hit").forEach((b) => {
      b.addEventListener("mousedown", (e) => {
        e.preventDefault();
        go(results[Number(b.dataset.i)]);
      });
    });
  };

  const query = async (q: string) => {
    const mine = ++seq;
    try {
      const hits = await api.search(q);
      if (mine !== seq) return; // a newer query superseded this one
      results = hits;
      active = -1;
      draw();
    } catch {
      /* ignore transient search errors */
    }
  };

  const onInput = () => {
    const q = input.value.trim();
    window.clearTimeout(debounce);
    if (q.length < 1) {
      close();
      return;
    }
    debounce = window.setTimeout(() => void query(q), 200);
  };

  const onKey = (e: KeyboardEvent) => {
    if (box.hidden) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      active = Math.min(active + 1, results.length - 1);
      draw();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      active = Math.max(active - 1, 0);
      draw();
    } else if (e.key === "Enter") {
      if (active >= 0 && results[active]) go(results[active]);
      else if (results.length) go(results[0]);
    } else if (e.key === "Escape") {
      close();
    }
  };

  input.addEventListener("input", onInput);
  input.addEventListener("keydown", onKey);
  input.addEventListener("blur", () => window.setTimeout(close, 120));
  input.addEventListener("focus", onInput);

  return () => {
    window.clearTimeout(debounce);
    input.removeEventListener("input", onInput);
    input.removeEventListener("keydown", onKey);
  };
}

function escape(s: string): string {
  return s.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}
