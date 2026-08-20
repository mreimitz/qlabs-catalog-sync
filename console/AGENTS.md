# AGENTS.md — QLabs Catalog Sync Console

**Read `./CLAUDE.md` first — it is the full contract for this app** (component
reuse, the type/colour taxonomy, theming, state coverage, the presentation-layer
boundary). This file exists so agents that look for `AGENTS.md` find the same rules.

The short version:

- Compose from `@elabs-ai/components-*`; don't hand-roll tables, dialogs,
  chat bubbles or KPI tiles.
- Type is a **role** (`text-title`/`text-body`/…), colour is a **token**
  (`bg-primary`, `text-muted-foreground`) — never a raw size or hex.
- The theme is `light`, applied by `<ThemeProvider>` in `src/main.tsx`;
  change tokens, not component styles. Two shipped themes here — `light` and
  `dark` — everything must read correctly in both.
- The spec is `./app-spec.md`; `grep -rn "TODO(spec):" src` is the to-do list.
- `./brand-ui-context.md` lists every component in every package — read it instead
  of inventing one; `brand-ui docs <Name>` gives the real props.
- brand-ui renders models — it never calls them. Fetching/transport lives in this app.
