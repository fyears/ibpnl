// Entry point: shell (topbar + settings) and a tiny hash router.
//   #/           -> home (account + positions)
//   #/i/<conId>  -> instrument detail

import "./styles/tokens.css";
import "./styles/app.css";
import { api } from "./api/client";
import { stream } from "./api/stream";
import {
  applyColorConvention,
  getSettings,
  updateSettings,
} from "./state/settings";
import { renderHome } from "./pages/home";
import { renderInstrument } from "./pages/instrument";
import { renderCombo } from "./pages/combo";
import { mountSearchBox } from "./components/searchBox";
import { installErrorReporting, reportError } from "./lib/errors";

const app = document.getElementById("app")!;

let teardown: (() => void) | null = null;

function shell(): { outlet: HTMLElement } {
  app.innerHTML = `
    <header class="topbar">
      <span class="wordmark"><a href="#/">IB&nbsp;<span class="tick">PnL</span></a></span>
      <div class="topbar-search" id="search-host"></div>
      <span class="conn" id="conn"><span class="dot"></span><span id="conn-text">Connecting…</span></span>
      <button class="icon-btn" id="settings-btn" aria-label="Settings">Settings</button>
    </header>
    <div class="ib-warn" id="ib-warn" hidden></div>
    <main id="outlet"></main>
    <dialog class="settings" id="settings-dlg">
      <form method="dialog" class="settings-body">
        <h2>Settings</h2>
        <div class="settings-row">
          <span class="eyebrow">Profit color</span>
          <div class="seg" role="group" aria-label="Profit color convention">
            <button type="button" data-cc="green-up">Green = up</button>
            <button type="button" data-cc="red-up">Red = up</button>
          </div>
          <div class="hint">Applied to every P&amp;L figure and the chart candles.</div>
        </div>
        <div class="settings-row">
          <span class="eyebrow">Chart time</span>
          <div class="seg" role="group" aria-label="Chart timezone">
            <button type="button" data-tz="exchange">Exchange time</button>
            <button type="button" data-tz="local">My local time</button>
          </div>
          <div class="hint">Axis and crosshair times on instrument charts.</div>
        </div>
        <button class="settings-close" value="close">Close</button>
      </form>
    </dialog>
  `;

  const dlg = document.getElementById("settings-dlg") as HTMLDialogElement;
  document.getElementById("settings-btn")!.addEventListener("click", () => {
    dlg.showModal();
  });

  const sync = () => {
    const s = getSettings();
    dlg.querySelectorAll<HTMLButtonElement>("[data-cc]").forEach((b) => {
      b.classList.toggle("active", b.dataset.cc === s.colorConvention);
    });
    dlg.querySelectorAll<HTMLButtonElement>("[data-tz]").forEach((b) => {
      b.classList.toggle("active", b.dataset.tz === s.timezone);
    });
  };
  dlg.querySelectorAll<HTMLButtonElement>("[data-cc]").forEach((b) => {
    b.addEventListener("click", () => {
      updateSettings({ colorConvention: b.dataset.cc as "green-up" | "red-up" });
      sync();
    });
  });
  dlg.querySelectorAll<HTMLButtonElement>("[data-tz]").forEach((b) => {
    b.addEventListener("click", () => {
      updateSettings({ timezone: b.dataset.tz as "exchange" | "local" });
      sync();
    });
  });
  sync();

  // Global symbol search lives in the banner, available on every page.
  mountSearchBox(document.getElementById("search-host")!, {
    compact: true,
    placeholder: "Search symbol…",
  });

  return { outlet: document.getElementById("outlet")! };
}

function setIbWarn(html: string | null): void {
  const warn = document.getElementById("ib-warn");
  if (!warn) return;
  if (!html) {
    warn.hidden = true;
    warn.innerHTML = "";
    return;
  }
  warn.hidden = false;
  warn.innerHTML = html;
  warn.querySelector("#ib-retry")?.addEventListener("click", () => {
    void pollStatus();
  });
}

async function pollStatus(): Promise<void> {
  const conn = document.getElementById("conn");
  const text = document.getElementById("conn-text");
  if (!conn || !text) return;
  try {
    const st = await api.status();
    conn.className = `conn ${st.connected ? "ok" : "bad"}`;
    const label = st.provider === "mock" ? "Simulated" : "IB";
    text.textContent = st.connected
      ? `${label} · ${st.account || "connected"}`
      : "Disconnected";
    conn.title = st.detail;
    // Prominent banner when running against IB but not connected/logged in.
    if (st.provider === "ib" && !st.connected) {
      setIbWarn(
        `<span class="ib-warn-msg"><strong>IB Gateway / TWS not connected.</strong> ` +
          `${escapeHtml(st.detail || "Start IB Gateway or TWS, log in, and enable API access.")}</span>` +
          `<button type="button" class="ib-warn-btn" id="ib-retry">Retry now</button>`,
      );
    } else {
      setIbWarn(null);
    }
  } catch (err) {
    conn.className = "conn bad";
    text.textContent = "Backend unreachable";
    setIbWarn(
      `<span class="ib-warn-msg"><strong>Backend unreachable.</strong> ` +
        `The ibpnl server isn't responding. Check that it's running, then</span>` +
        `<button type="button" class="ib-warn-btn" id="ib-retry">Retry now</button>`,
    );
    reportError("status poll failed (backend unreachable?)", err);
  }
}

function escapeHtml(s: string): string {
  return s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function route(): void {
  teardown?.();
  teardown = null;
  const outlet = document.getElementById("outlet")!;
  const hash = location.hash || "#/";
  const instMatch = hash.match(/^#\/i\/(\d+)/);
  const comboMatch = hash.match(/^#\/combo\/(.+)$/);
  if (instMatch) {
    teardown = renderInstrument(outlet, Number(instMatch[1]));
  } else if (comboMatch) {
    teardown = renderCombo(outlet, decodeURIComponent(comboMatch[1]));
  } else {
    teardown = renderHome(outlet);
  }
}

applyColorConvention();
installErrorReporting();
shell();
stream.connect();
route();
window.addEventListener("hashchange", route);
pollStatus();
setInterval(pollStatus, 15000);
