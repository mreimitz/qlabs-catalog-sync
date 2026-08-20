// Vitest global setup: registers @testing-library/jest-dom's matchers
// (toBeInTheDocument, toHaveTextContent, ...) on vitest's `expect`, and cleans up the
// jsdom document between tests so one test's rendered tree never leaks into the next.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});

// jsdom does not implement window.matchMedia (it has no layout engine to evaluate media
// queries against). @elabs-ai/components-ui's `useIsMobile` hook (Sidebar's responsive
// behavior) calls it unconditionally on mount, so every component test that renders a
// Sidebar throws "matchMedia is not a function" without this — not a testing nicety,
// the shell literally cannot render in jsdom otherwise. `matches: false` fixes every
// query to "not mobile", which is the right default for a desktop-first admin console.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}
