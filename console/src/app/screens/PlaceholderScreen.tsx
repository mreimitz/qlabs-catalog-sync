import { StatePanel } from "@elabs-ai/components-ui";

interface PlaceholderScreenProps {
  title: string;
  builtBy: string;
}

/** An honest "not built yet" screen. T13.2's own DoD is to give every nav destination a
 * route and "a placeholder that is obviously a placeholder, not a fake-working screen" --
 * `StatePanel kind="empty"` is the right semantic for that: there genuinely is nothing here
 * yet, which is a different, calmer state than an error or a loading spinner
 * (`console/CLAUDE.md`'s state discipline: every async surface gets loading/empty/error, and
 * this is deliberately the "empty" one, never dressed up as either of the others). */
export function PlaceholderScreen({ title, builtBy }: PlaceholderScreenProps) {
  return (
    <StatePanel
      kind="empty"
      title={title}
      description={`This screen has not been built yet. ${builtBy} builds it.`}
    />
  );
}
