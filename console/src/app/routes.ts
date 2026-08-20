import { ArrowRightLeft, FlaskConical, History, ListFilter, Plug } from "lucide-react";
import type { ComponentType, SVGProps } from "react";

/** One entry per top-level screen `app-spec.md` lists for the enterprise-admin shell
 * (archetype B): "collapsible icon sidebar with the five top-level views". T13.2 owns the
 * shell and the routing; T13.3-T13.7 each replace their own `PlaceholderScreen` with the
 * real thing -- the route and the nav entry are not theirs to add, only the content is. */
export interface NavRoute {
  path: string;
  label: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  /** The board task that builds the real screen at this route -- shown by the placeholder so
   * the shell never pretends a screen exists before it does. */
  builtBy: string;
}

export const NAV_ROUTES: readonly NavRoute[] = [
  { path: "/endpoints", label: "Endpoints", icon: Plug, builtBy: "T13.3" },
  { path: "/pairs", label: "Sync pairs", icon: ArrowRightLeft, builtBy: "T13.4" },
  { path: "/selection", label: "Selection", icon: ListFilter, builtBy: "T13.5" },
  { path: "/dry-run", label: "Dry run", icon: FlaskConical, builtBy: "T13.6" },
  { path: "/runs", label: "Runs", icon: History, builtBy: "T13.7" },
];

/** Where "/" redirects, and the shell's own home link. Endpoints is first because nothing
 * else in the console has meaning before at least one endpoint is registered. */
export const DEFAULT_ROUTE = NAV_ROUTES[0]!.path;
