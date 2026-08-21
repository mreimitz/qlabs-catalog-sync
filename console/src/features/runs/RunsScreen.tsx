// Runs (T13.7) -- the screen this feature exports for `../../app/routes.ts`'s `/runs` route.
// Board task summary: "Run history per pair with status and duration, run detail with counts
// and issues, the orphan and unresolved-reference report, and the run-now, pause and resume
// controls."
//
// One route, two peer views (`Tabs`, never sequential steps -- see that component's own
// anti-pattern note): "History" (`RunHistoryTab.tsx` -- the table, the run-detail sheet, and
// the run-now/pause/resume controls for whichever pair is selected) and "Configuration
// changes" (`ConfigChangesTab.tsx` -- `GET /api/config-changes`, also owned by this feature
// per the task's own route list, and `app-spec.md`'s "reachable from here or from the
// endpoint/pair screens it documents"). This feature owns exactly one nav entry
// (`../../app/routes.ts`'s `{ path: "/runs", ... }`), so both views live under it rather than
// each claiming a route this task is not allowed to add (`App.tsx`/`routes.ts` are T13.2's,
// out of this task's owned paths -- see this task's own report for the exact wiring the
// orchestrator adds).
import { SectionHeader, Tabs, TabsContent, TabsList, TabsTrigger, TooltipProvider } from "@elabs-ai/components-ui";

import { RunHistoryTab } from "./RunHistoryTab";
import { ConfigChangesTab } from "./ConfigChangesTab";

export function RunsScreen() {
  return (
    <TooltipProvider>
      <div className="flex flex-col gap-6">
        <SectionHeader
          title="Runs"
          description="What each sync pair's cycles actually did: status, duration, counts and any unresolved references or orphans they reported. The engine never deletes from Qlik -- an orphan here is a source object that vanished, reported for review, not removed."
        />

        <Tabs defaultValue="history">
          <TabsList>
            <TabsTrigger value="history">History</TabsTrigger>
            <TabsTrigger value="config-changes">Configuration changes</TabsTrigger>
          </TabsList>
          <TabsContent value="history">
            <RunHistoryTab />
          </TabsContent>
          <TabsContent value="config-changes">
            <ConfigChangesTab />
          </TabsContent>
        </Tabs>
      </div>
    </TooltipProvider>
  );
}
