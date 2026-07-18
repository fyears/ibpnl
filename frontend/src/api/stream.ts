// WebSocket client with auto-reconnect and declarative subscriptions.
//
// Consumers call subscribeQuotes()/subscribeBars() to declare what they want;
// the socket (re)sends the current subscription set on every (re)connect, so
// reconnects are transparent to pages.

import type { WsMessage } from "./types";
import { reportError } from "../lib/errors";

type Listener = (msg: WsMessage) => void;

class Stream {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private quoteIds = new Set<number>();
  private barId: number | null = null;
  private barSize = "1 min";
  private reconnectDelay = 1000;
  private closed = false;
  private pingTimer: number | undefined;

  connect(): void {
    if (this.ws || this.closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;

    ws.onopen = () => {
      this.reconnectDelay = 1000;
      this.pushSubscriptions();
      this.pingTimer = window.setInterval(() => {
        this.send({ action: "ping" });
      }, 25000);
    };
    ws.onmessage = (ev) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(ev.data);
      } catch (err) {
        reportError("dropped malformed WebSocket message", err);
        return;
      }
      for (const l of this.listeners) l(msg);
    };
    ws.onclose = () => {
      this.ws = null;
      window.clearInterval(this.pingTimer);
      if (!this.closed) {
        console.warn(
          `[ibpnl] WebSocket closed; reconnecting in ${Math.round(this.reconnectDelay)}ms`,
        );
        window.setTimeout(() => this.connect(), this.reconnectDelay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 1.6, 15000);
      }
    };
    ws.onerror = (ev) => {
      reportError("WebSocket error", ev);
      ws.close();
    };
  }

  private send(obj: unknown): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  private pushSubscriptions(): void {
    this.send({ action: "subscribe", con_ids: [...this.quoteIds] });
    if (this.barId !== null) {
      this.send({ action: "subscribe_bars", con_id: this.barId, bar_size: this.barSize });
    }
  }

  /** Replace the set of instruments to stream quotes/greeks/pnl for. */
  subscribeQuotes(conIds: number[]): void {
    this.quoteIds = new Set(conIds);
    this.send({ action: "subscribe", con_ids: [...this.quoteIds] });
  }

  /** Stream live bars for one instrument at a bar size (or null to stop). */
  subscribeBars(conId: number | null, barSize = "1 min"): void {
    this.barId = conId;
    this.barSize = barSize;
    if (conId === null) this.send({ action: "unsubscribe_bars" });
    else this.send({ action: "subscribe_bars", con_id: conId, bar_size: barSize });
  }

  onMessage(l: Listener): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }
}

export const stream = new Stream();
