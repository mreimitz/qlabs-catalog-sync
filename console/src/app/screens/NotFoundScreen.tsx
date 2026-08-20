import { Link } from "react-router-dom";
import { Button, StatePanel } from "@elabs-ai/components-ui";

import { DEFAULT_ROUTE } from "../routes";

/** The client-side router's own catch-all. `api/static.py`'s SPA history fallback means the
 * server already answered any unknown path with this bundle's `index.html`; this is what the
 * bundle shows once it takes over and finds no route matches either -- a stray URL, not a
 * server error, so it renders as `StatePanel kind="empty"`, not an error state. */
export function NotFoundScreen() {
  return (
    <StatePanel
      kind="empty"
      title="Nothing here"
      description="This page doesn't exist."
      actions={
        <Button asChild size="sm">
          <Link to={DEFAULT_ROUTE}>Go to Endpoints</Link>
        </Button>
      }
    />
  );
}
