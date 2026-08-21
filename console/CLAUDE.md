# CLAUDE.md — QLabs Catalog Sync Console

This app is built on **brand-ui** (`@elabs-ai/components-*`). It was
scaffolded from the **data-app** template; the spec is in `./app-spec.md` — read
it before making structural changes.

## Non-negotiable rules

- **Use brand-ui components first.** Before writing any UI markup, check
  `…-ui`, `…-data`, `…-ai`, `…-flow`, `…-charts`, `…-marketing` for an existing
  component (`pnpm exec brand-ui search <concept>`, or the `mcp__brand-ui__search`
  tool in Claude Code). Do not hand-roll tables, dialogs, chat bubbles, or KPI tiles.
- **Type is a role, not a size.** Use a `text-<role>` utility (`text-title`,
  `text-body`, `text-caption`, `text-display`, `text-kpi`, …) or the
  `<Heading>`/`<Text>` components. Never `text-2xl`, `text-sm`, or `text-[18px]`.
- **Semantic tokens only.** `bg-background`, `text-muted-foreground`,
  `bg-primary` (+ `text-primary-foreground`), `border-border`,
  `var(--chart-1..5)`. Never raw hex, `rgb()`, `bg-[#…]`, or a Tailwind palette
  (`text-gray-500`). Re-theming must stay a token swap.
- **Don't touch the theme mechanism.** The app is themed via
  `<ThemeProvider defaultTheme="light">` from `…-tokens` (see `src/main.tsx`).
  To change look-and-feel, change tokens/theme — not component styles.
- **Keep the existing shell.** Extend the sidebar/nav in place; don't rebuild it.
- **Icons:** generic glyphs from `lucide-react`; brand marks from `…-icons`.
  No other icon libraries.
- **States:** every async surface gets loading (`Skeleton`), empty
  (`StatePanel kind="empty"`), and error (`StatePanel kind="error"`) — never a
  blank region.
- **brand-ui is presentation-only.** Model calls, fetching, and transport live in
  this app's hooks/services — never inside shared UI components.
- **Audit after UI edits.** `pnpm lint` and `pnpm audit:ui` (= `brand-ui audit
src`) — the static token/anti-slop pass; the rendered cross-theme + contrast pass
  is the `brand-ui-audit` skill. The scaffold's own `.github/workflows/brand-ui.yml`
  was deleted (GitHub only reads workflows from the repository root, and a copy
  under `console/` was dead weight) — CI for this app lives at the repo root,
  `.github/workflows/console.yml` (WP13/T13.8, RM-06). Keep that job green.

## What exists (don't guess an API)

`./brand-ui-context.md` is the generated inventory of every component in every
`@elabs-ai/components-*` package — read it before inventing a
component. For the real props of one component: `pnpm exec brand-ui docs <Name>`
(or `mcp__brand-ui__docs`). Refresh the inventory after upgrading the packages
with `pnpm exec brand-ui context`.

## Run it

```bash
pnpm dev        # vite (index.html → src/main.tsx → src/App.tsx)
pnpm typecheck  # tsc --noEmit
pnpm lint
pnpm test       # vitest (watch mode) — `pnpm test --run` for one pass, CI-style
pnpm a11y       # vitest run a11y — axe-core over every *.a11y.test.tsx file
pnpm audit:ui   # brand-ui audit src
pnpm build      # tsc --noEmit && vite build -> dist/
```

`vitest.config.ts` + `src/test/setup.ts` are T13.1's minimum viable harness (jsdom
environment, jest-dom matchers, a `window.matchMedia` polyfill jsdom doesn't ship —
see the comment in `setup.ts`). `src/test/app-shell.test.tsx` and
`src/test/a11y.test.tsx` cover the composed real `App` (sign-in and the signed-in shell),
not any one feature screen — each feature owns its own `<Screen>.test.tsx` /
`<Screen>.a11y.test.tsx` beside it.

**Naming convention, load-bearing:** `pnpm a11y` runs `vitest run a11y`, and vitest
treats that positional argument as a *path substring filter*. So an accessibility test
is picked up if — and only if — its path contains `a11y`. Name every one
`<thing>.a11y.test.tsx` and put it beside the screen it covers. A screen whose
accessibility test is named anything else is silently not gated: the command still exits
0, having run the other files. (It fails closed in the other direction — a filter that
matches nothing exits 1 rather than passing vacuously.)

**The gate no longer trusts that convention blindly.** `src/test/a11y-coverage.a11y.test.ts`
(T13.8) enumerates every `*Screen.tsx` under `src/features/*` and `src/auth` via
`import.meta.glob` and asserts a matching `*.a11y.test.tsx` sits beside each one. It is
itself named `*.a11y.test.ts`, so it runs under both `pnpm test` (unfiltered) and `pnpm
a11y` (filtered) — a new feature screen that lands without its accessibility test fails
the gate immediately, rather than silently shrinking the set of files `pnpm a11y` covers.

## Install / make it runnable

Already done — `package.json` pins every `@elabs-ai/components-*` package to an
exact version (never `latest`; a floating tag has no lockfile-independent safety
net) and `pnpm-lock.yaml` is committed, so `pnpm install --frozen-lockfile` from
`console/` is all a fresh checkout needs. Adding a **new** `@elabs-ai/components-*`
package later: pin it to the same version as the others (`4.0.0` as of T13.1) with
`pnpm add "@elabs-ai/components-<name>@4.0.0"`, then add its `@source` line to
`src/styles.css` (below) in the same change.

`src/styles.css` already carries the token import and one `@source` line per
installed package — **do not delete them**, the components render unstyled without
them. Full recipe: docs/CONSUMING.md §1-4 in the brand-ui repo.

## Wiring points

Unfinished spots are marked `TODO(spec):` (what the spec did not answer) and
`WIRE:` (where real data plugs in). `grep -rn "TODO(spec):\|WIRE:" src` lists
what's left. Wire them; don't delete the guidance until each is wired.

## Themes

Two shipped themes: `light` and `dark`. Anything you build must read
correctly in **both** — that is an observed result (render it), never inferred from
"it uses tokens".

## Composition reference

This archetype's recipe: `docs/playbooks/data-app.md` in the brand-ui repo (building
blocks, wiring order, common mistakes). Follow it before inventing new structure.
