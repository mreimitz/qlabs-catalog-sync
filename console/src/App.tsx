/* QLabs Catalog Sync Console — application shell, sign-in and session handling (T13.2).
 *
 * Archetype: data-app, shell B (enterprise admin) · theme: light/dark, System-aware.
 * Replaces T13.1's scaffolded placeholder (a demo data table with fake nav) with the real
 * shell described in `app-spec.md`. See `console/CLAUDE.md`'s "Wiring points" section and
 * this task's own report for what later tasks (T13.3-T13.7) still need to fill in.
 */
import type { ComponentType } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Toaster } from "@elabs-ai/components-ui";

import { AuthGate } from "./app/AuthGate";
import { NotFoundScreen } from "./app/screens/NotFoundScreen";
import { DEFAULT_ROUTE, NAV_ROUTES } from "./app/routes";
import { Shell } from "./app/Shell";
import { EndpointsScreen } from "./features/endpoints/EndpointsScreen";
import { PairsScreen } from "./features/pairs/PairsScreen";
import { RunsScreen } from "./features/runs/RunsScreen";
import { DryRunScreen } from "./features/dry-run/DryRunScreen";
import { SelectionScreen } from "./features/selection/SelectionScreen";

// Every nav route in `app/routes.ts` renders a real screen now (the last of them landed in
// T13.6) -- there is no longer a route that falls through to a placeholder, so the dispatch is a
// straight lookup rather than a chain that has to account for one. Falling back to
// `NotFoundScreen` (already the catch-all for unmatched paths below) rather than throwing keeps
// this safe if `routes.ts` -- which this task does not own -- ever grows a path before its
// screen exists, without reintroducing a "some assembly required" placeholder.
const SCREENS: Record<string, ComponentType> = {
  "/endpoints": EndpointsScreen,
  "/pairs": PairsScreen,
  "/selection": SelectionScreen,
  "/dry-run": DryRunScreen,
  "/runs": RunsScreen,
};

function App() {
  return (
    <BrowserRouter>
      {/* Mounted once at the root (enterprise baseline): later screens call `toast()` from
          `@elabs-ai/components-ui` for command results (rule reordered, healthcheck run, pair
          paused, ...) without any of them needing to mount their own Toaster. */}
      <Toaster />
      <AuthGate>
        <Routes>
          <Route element={<Shell />}>
            {NAV_ROUTES.map((route) => {
              const Screen = SCREENS[route.path] ?? NotFoundScreen;
              return <Route key={route.path} path={route.path} element={<Screen />} />;
            })}
            <Route path="/" element={<Navigate to={DEFAULT_ROUTE} replace />} />
            <Route path="*" element={<NotFoundScreen />} />
          </Route>
        </Routes>
      </AuthGate>
    </BrowserRouter>
  );
}

export default App;
