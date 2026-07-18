// Global user settings, persisted to localStorage.
//
// colorConvention:
//   "green-up" — green means gains/up (US convention)
//   "red-up"   — red means gains/up (CN/HK/KR/TW/JP convention)
// timezone:
//   "exchange" — chart times in the instrument's exchange timezone
//   "local"    — chart times in the viewer's timezone

export interface Settings {
  colorConvention: "green-up" | "red-up";
  timezone: "exchange" | "local";
}

const KEY = "ibkr-deck-settings";

const defaults: Settings = {
  colorConvention: "green-up",
  timezone: "exchange",
};

let current: Settings = load();
const listeners = new Set<(s: Settings) => void>();

function load(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...defaults, ...JSON.parse(raw) };
  } catch {
    /* corrupted storage — fall back to defaults */
  }
  return { ...defaults };
}

export function getSettings(): Settings {
  return current;
}

export function updateSettings(patch: Partial<Settings>): void {
  current = { ...current, ...patch };
  try {
    localStorage.setItem(KEY, JSON.stringify(current));
  } catch {
    /* private mode etc. — setting just won't persist */
  }
  applyColorConvention();
  for (const l of listeners) l(current);
}

export function onSettingsChange(l: (s: Settings) => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** Sets CSS vars --up/--down according to the convention. */
export function applyColorConvention(): void {
  const root = document.documentElement;
  const green = "#0E7C4A";
  const red = "#C93B36";
  if (current.colorConvention === "red-up") {
    root.style.setProperty("--up", red);
    root.style.setProperty("--down", green);
  } else {
    root.style.setProperty("--up", green);
    root.style.setProperty("--down", red);
  }
}
