// Centralized error reporting.
//
// Every failure is logged to the browser devtools console with an `[ibpnl]`
// prefix and full detail. User-facing failures (uncaught errors, lost backend)
// also raise a small, self-dismissing toast so problems aren't silent.

let toastHost: HTMLElement | null = null;
const active = new Set<string>();

function ensureHost(): HTMLElement {
  if (toastHost) return toastHost;
  const el = document.createElement("div");
  el.className = "toast-host";
  el.setAttribute("aria-live", "polite");
  document.body.appendChild(el);
  toastHost = el;
  return el;
}

/** Show a small toast. Identical messages are de-duplicated while visible. */
export function toast(message: string, kind: "error" | "info" = "error"): void {
  if (active.has(message)) return;
  active.add(message);
  const host = ensureHost();
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = message;
  host.appendChild(t);
  window.setTimeout(() => {
    t.classList.add("leaving");
    window.setTimeout(() => {
      t.remove();
      active.delete(message);
    }, 300);
  }, 6000);
}

/** Log an error to the console (devtools). Optionally surface a toast. */
export function reportError(context: string, err: unknown, notify = false): void {
  console.error(`[ibpnl] ${context}:`, err);
  if (notify) toast(`${context} — see console for details.`);
}

/** Install global handlers so nothing fails silently. Call once at startup. */
export function installErrorReporting(): void {
  window.addEventListener("error", (e) => {
    console.error("[ibpnl] uncaught error:", e.error ?? e.message);
    toast("Something went wrong — see the console for details.");
  });
  window.addEventListener("unhandledrejection", (e) => {
    console.error("[ibpnl] unhandled promise rejection:", e.reason);
    toast("Something went wrong — see the console for details.");
  });
}
