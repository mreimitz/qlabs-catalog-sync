import js from "@eslint/js";
import tseslint from "typescript-eslint";

// NOTE: the shared `@elabs-ai/components-eslint-config` (which ships
// `brand/no-raw-font-size` + `brand/no-raw-color`) is a PRIVATE, unpublished
// package — a standalone app cannot install it. Until it is published, the two
// taxonomy rules are NOT machine-enforced here; they are still non-negotiable and
// are spelled out in CLAUDE.md, and `brand-ui audit <dir>` catches raw colours
// and other token violations statically. Run it in CI.
export default [
  js.configs.recommended,
  ...tseslint.configs.recommended,
  { ignores: ["dist/**"] },
];
