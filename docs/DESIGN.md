# Design

The visual identity of the dashboard. Read this before touching styles.

## Brief

Subject: a professional monitor for a cross-market IBKR derivatives book.
Audience: the account owner, checking risk and PnL several times a day, often
on a phone. The home page's single job: **"how is my book doing right now?"**
The instrument page's job: "what is this leg doing, in context?"

Direction: broker-ledger / terminal-tape vernacular translated to a crisp
light sheet. Data is the decoration; nothing else decorates.

## Tokens (`frontend/src/styles/tokens.css`)

| Token | Value | Role |
| --- | --- | --- |
| `--paper` | `#F6F7F9` | page background (cold, not cream) |
| `--card` | `#FFFFFF` | table/chart surfaces |
| `--ink` | `#131820` | primary text |
| `--muted` | `#5D6673` | secondary text, labels |
| `--hairline` | `#E2E6EC` | rules and borders |
| `--accent` | `#16508F` | broker blue: links, focus, the group rail |
| `--up` / `--down` | `#0E7C4A` / `#C93B36` | PnL pair; **swapped** by the color-convention setting (red-up markets style) |

Type roles:
- **Numeric voice (the personality):** monospace — `"Cascadia Mono", "SF Mono",
  "Roboto Mono", Consolas, monospace`, tabular figures, used for every number.
- **UI voice:** system sans (`"Segoe UI", system-ui, sans-serif`) for labels/nav.
- **Eyebrows:** 11px uppercase, `letter-spacing: .08em`, muted.

## Signature element

**The group rail.** Each underlying group in the positions table is a ledger
section: a 3px accent-colored left rail, the underlying symbol set large in
mono, and aggregate chips (leg count, net Δ, day P&L, unrealized P&L). The rail
encodes the brief's core requirement (positions grouped per underlying) —
structure as information.

## Rules of restraint

- One accent color. PnL green/red appears **only** on PnL/price-change values,
  never as decoration.
- The account summary is a hairline-divided **tape**, not a grid of cards.
- Motion: cell background flashes (decaying tint) on live price changes;
  skeleton shimmer while loading; nothing else animates. `prefers-reduced-motion`
  disables both.
- Badges (LIVE / DELAYED / FROZEN / NO DATA, LONG / SHORT) are typographic —
  small caps, hairline border — not colored pills, except NO DATA which uses a
  muted fill to read as "inactive".
- Quality floor: responsive to 360px, visible keyboard focus (accent outline),
  first table column sticky on horizontal scroll.
