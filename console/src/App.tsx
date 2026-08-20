/* QLabs Catalog Sync Console — application shell, sign-in and session handling (T13.2).
 *
 * Archetype: data-app, shell B (enterprise admin) · theme: light/dark, System-aware.
 * Replaces T13.1's scaffolded placeholder (a demo data table with fake nav) with the real
 * shell described in `app-spec.md`. See `console/CLAUDE.md`'s "Wiring points" section and
 * this task's own report for what later tasks (T13.3-T13.7) still need to fill in.
 */
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Toaster } from "@elabs-ai/components-ui";

import { AuthGate } from "./app/AuthGate";
import { NotFoundScreen } from "./app/screens/NotFoundScreen";
import { PlaceholderScreen } from "./app/screens/PlaceholderScreen";
import { DEFAULT_ROUTE, NAV_ROUTES } from "./app/routes";
import { Shell } from "./app/Shell";
import { EndpointsScreen } from "./features/endpoints/EndpointsScreen";

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
            {NAV_ROUTES.map((route) => (
              <Route
                key={route.path}
                path={route.path}
                element={
                  route.path === "/endpoints" ? (
                    <EndpointsScreen />
                  ) : (
                    <PlaceholderScreen title={route.label} builtBy={route.builtBy} />
                  )
                }
              />
            ))}
            <Route path="/" element={<Navigate to={DEFAULT_ROUTE} replace />} />
            <Route path="*" element={<NotFoundScreen />} />
          </Route>
        </Routes>
      </AuthGate>
    </BrowserRouter>
  );
}

export default App;
