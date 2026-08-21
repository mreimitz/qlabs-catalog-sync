// Structural guard for the a11y gate's OWN coverage (T13.8, RM-06 DoD). `pnpm a11y` runs
// `vitest run a11y`, and vitest treats that positional argument as a *path substring filter*
// (console/CLAUDE.md's load-bearing naming convention). A screen whose accessibility test is
// named anything other than `<Name>.a11y.test.tsx` is silently unfiltered-in: the command still
// exits 0, having run every OTHER a11y file. That is a silent coverage regression waiting to
// happen the next time a feature folder is added and its author forgets the suffix.
//
// Rather than rely on everyone remembering the convention, this test enumerates the actual
// screen components and fails the build if any one of them lacks a matching a11y test file
// beside it. It is itself named `*.a11y.test.ts` so it is always exercised by the very filter it
// audits -- collected by both `pnpm test --run` (unfiltered) and `pnpm a11y` (filtered). If this
// file is ever deleted, renamed off the `.a11y.` suffix, or excluded, the coverage guarantee it
// enforces disappears with it -- the same failure mode as any other test, not a new one.
import { describe, expect, it } from "vitest";

// import.meta.glob, not node:fs/node:path: this project has no @types/node dependency (see
// tsconfig.json's `types: ["vite/client"]`), and Vite's own static glob -- resolved by the same
// bundler that resolves every other import in this app -- needs none. `eager: false` and never
// invoking the loaders: this test only inspects which module paths EXIST, it never imports them,
// so a screen with an unrelated broken import can't make this file fail for the wrong reason.
const screenModules = import.meta.glob(["../features/*/*Screen.tsx", "../auth/*Screen.tsx"], {
  eager: false,
});
const a11yTestModules = import.meta.glob(
  ["../features/*/*.a11y.test.tsx", "../auth/*.a11y.test.tsx"],
  { eager: false },
);

const screenPaths = Object.keys(screenModules).sort();
const a11yTestPaths = new Set(Object.keys(a11yTestModules));

describe("a11y coverage", () => {
  it("found at least one screen to check", () => {
    // Guards the guard: if `features/` or `auth/` were ever renamed or moved, the globs above
    // would silently match zero screens and every check below would pass vacuously -- exactly
    // the "matches nothing, exits happy" failure mode this file exists to rule out.
    expect(screenPaths.length).toBeGreaterThan(0);
  });

  it.each(screenPaths)("%s has a matching *.a11y.test.tsx beside it", (screenPath) => {
    const expected = screenPath.replace(/\.tsx$/, ".a11y.test.tsx");
    expect(a11yTestPaths.has(expected)).toBe(true);
  });
});
