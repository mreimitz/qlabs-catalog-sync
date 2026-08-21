/* The ordered rule editor (C3). Rules are rendered in EVALUATION order and nothing else.
 *
 * Order is the meaning, and last match wins
 * -----------------------------------------
 *
 * Evaluation runs top to bottom and the **last** matching rule decides. A UI that showed these
 * rows in insertion order, or that implied the first match wins, would be actively lying about
 * what the engine is going to do -- so the list is sorted on the server's explicit `ordinal`
 * (`draft.ts`'s `sortByOrdinal`, applied when the draft is seeded), every row states its
 * evaluation position, and the rule that decides a given object is the LAST one that matched,
 * which this screen never re-derives: it comes back on `SelectionResultOut.rule_id`.
 *
 * An override is not a rule at the top of this list
 * -------------------------------------------------
 *
 * Per-object overrides beat every rule outright -- no rule is consulted at all when one applies
 * (C3). They are pinned from the source tree, are saved immediately rather than drafted, and
 * are deliberately not editable here, so nothing about this list can suggest an override is
 * just another row that a later row could out-rank.
 *
 * Reordering is drafted, then sent as one complete ordered id list
 * ----------------------------------------------------------------
 *
 * Dragging a row, or moving it with the keyboard, changes the draft only. Saving sends
 * `POST /rules/reorder` with the COMPLETE ordered list of that `(pair, scope)`'s rule ids --
 * never a "move rule X to index N" delta, which the route deliberately does not accept.
 *
 * Drag has a keyboard equal, not a keyboard afterthought
 * ------------------------------------------------------
 *
 * The grip is a mouse affordance and nothing else -- it is `aria-hidden` and not focusable, so
 * it can never become a focusable control with no keyboard behavior. Every reorder is equally
 * reachable through the per-row "Move up"/"Move down" buttons, which are real buttons in normal
 * tab order, and every move announces the row's new evaluation position through one polite live
 * region.
 */
import { useRef, useState, type DragEvent } from "react";
import {
  Alert,
  AlertDescription,
  Badge,
  Button,
  FieldRow,
  IconButton,
  Input,
  SegmentedField,
  StatePanel,
  Text,
  type SegmentedFieldOption,
} from "@elabs-ai/components-ui";
import { ChevronDown, ChevronUp, GripVertical, Plus, Trash2, Undo2 } from "lucide-react";

import {
  DECISION_LABEL,
  MATCHER_HINT,
  MATCHER_KINDS,
  MATCHER_LABEL,
  SCOPE_DESCRIPTION,
  SCOPE_LABEL,
  SCOPE_QUALIFIED_NAME_SHAPE,
} from "./labels";
import { isMatcherSelectable, type MatcherSupport } from "./matcherSupport";
import { moveRule, newDraftKey, type DraftRule } from "./draft";
import type { MatcherKind, RuleScope, SelectionDecision } from "./selectionApi";

const DECISION_OPTIONS: SegmentedFieldOption[] = [
  { value: "include", label: DECISION_LABEL.include },
  { value: "exclude", label: DECISION_LABEL.exclude },
];

function matcherOptions(support: MatcherSupport): SegmentedFieldOption[] {
  return MATCHER_KINDS.map((kind) => ({
    value: kind,
    // The disabled segment keeps its label AND says it is unavailable, so the state is in the
    // control's own accessible name rather than only in a colour or a tooltip. The reason
    // itself is printed once, in full, above the list.
    label: isMatcherSelectable(support[kind])
      ? MATCHER_LABEL[kind]
      : `${MATCHER_LABEL[kind]} — unavailable`,
    disabled: !isMatcherSelectable(support[kind]),
  }));
}

export interface RuleEditorProps {
  scope: RuleScope;
  onScopeChange: (scope: RuleScope) => void;
  rules: readonly DraftRule[];
  onRulesChange: (next: DraftRule[]) => void;
  support: MatcherSupport;
  dirty: boolean;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
  /** Draft key of the rule that decided the object currently selected in the source tree. */
  highlightKey: string | null;
  /** Per-row server-reported pattern errors, keyed by draft key, from the last failed save. */
  patternErrors: Readonly<Record<string, string>>;
  disabled?: boolean;
}

export function RuleEditor({
  scope,
  onScopeChange,
  rules,
  onRulesChange,
  support,
  dirty,
  saving,
  onSave,
  onDiscard,
  highlightKey,
  patternErrors,
  disabled = false,
}: RuleEditorProps) {
  const [announcement, setAnnouncement] = useState("");
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null);
  const dragFromRef = useRef<number | null>(null);

  const unavailable = MATCHER_KINDS.map((kind) => ({ kind, availability: support[kind] })).filter(
    (entry) => entry.availability.state !== "available",
  );

  function commitMove(from: number, to: number) {
    if (to < 0 || to >= rules.length) return;
    onRulesChange(moveRule(rules, from, to));
    const moved = rules[from];
    setAnnouncement(
      `Rule moved to evaluation position ${to + 1} of ${rules.length}${
        moved ? ` (${moved.decision} ${moved.matcherKind} ${moved.pattern || "no pattern yet"})` : ""
      }. Evaluation runs top to bottom and the last matching rule decides.`,
    );
  }

  function patch(index: number, change: Partial<DraftRule>) {
    onRulesChange(rules.map((rule, at) => (at === index ? { ...rule, ...change } : rule)));
  }

  function addRule() {
    onRulesChange([
      ...rules,
      { key: newDraftKey(), ruleId: null, scope, decision: "include", matcherKind: "glob", pattern: "" },
    ]);
    setAnnouncement(
      `Rule added at evaluation position ${rules.length + 1}. It is the last rule, so it out-ranks every rule above it for anything it matches.`,
    );
  }

  function removeRule(index: number) {
    onRulesChange(rules.filter((_, at) => at !== index));
    setAnnouncement(`Rule at evaluation position ${index + 1} removed from the draft.`);
  }

  function handleDragStart(event: DragEvent<HTMLElement>, index: number) {
    dragFromRef.current = index;
    // Guarded rather than assumed: `dataTransfer` is what a real browser needs (Firefox will
    // not start a drag without a payload), and it is absent under a synthesized event.
    const transfer: DataTransfer | null = event.dataTransfer;
    transfer?.setData("text/plain", String(index));
    if (transfer) transfer.effectAllowed = "move";
  }

  function handleDrop(event: DragEvent<HTMLElement>, index: number) {
    event.preventDefault();
    const from = dragFromRef.current;
    dragFromRef.current = null;
    setDragOverIndex(null);
    if (from === null || from === index) return;
    commitMove(from, index);
  }

  return (
    <div className="flex flex-col gap-4">
      <SegmentedField
        label="Rule scope"
        value={scope}
        onValueChange={(next) => onScopeChange(next as RuleScope)}
        options={[
          { value: "object", label: SCOPE_LABEL.object },
          { value: "dataset", label: SCOPE_LABEL.dataset },
        ]}
        disabled={disabled}
      />
      <Text variant="caption" tone="muted">
        {SCOPE_DESCRIPTION[scope]}
      </Text>

      <Alert>
        <AlertDescription>
          Rules are evaluated top to bottom and the <strong>last</strong> matching rule decides —
          not the first. Anything no rule matches is excluded. A per-object override, pinned from
          the source tree, beats every rule here outright.
        </AlertDescription>
      </Alert>

      {unavailable.length > 0 ? (
        <Alert variant={unavailable.some((e) => e.availability.state === "unavailable") ? "warning" : "default"}>
          <AlertDescription>
            <ul className="flex flex-col gap-1">
              {unavailable.map(({ kind, availability }) => (
                <li key={kind}>
                  <strong>{MATCHER_LABEL[kind]}:</strong>{" "}
                  {availability.state === "available" ? null : availability.reason}
                </li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      ) : null}

      {rules.length === 0 ? (
        <StatePanel
          kind="empty"
          title={`No ${SCOPE_LABEL[scope].toLowerCase()} rules yet`}
          description="Nothing is selected until a rule says so — an empty rule set syncs nothing. Add an include rule to start."
          actions={
            <Button onClick={addRule} disabled={disabled}>
              <Plus aria-hidden className="mr-1.5 size-4" />
              Add rule
            </Button>
          }
        />
      ) : (
        <ol className="flex flex-col gap-2" aria-label={`${SCOPE_LABEL[scope]} rules in evaluation order`}>
          {rules.map((rule, index) => {
            const currentUnavailable = !isMatcherSelectable(support[rule.matcherKind]);
            const currentAvailability = support[rule.matcherKind];
            return (
              <li
                key={rule.key}
                data-rule-key={rule.key}
                aria-current={highlightKey === rule.key ? "true" : undefined}
                onDragOver={(event) => {
                  event.preventDefault();
                  setDragOverIndex(index);
                }}
                onDragLeave={() => setDragOverIndex((prev) => (prev === index ? null : prev))}
                onDrop={(event) => handleDrop(event, index)}
                className={[
                  "flex flex-col gap-2 rounded-md border p-3",
                  highlightKey === rule.key ? "border-primary bg-accent" : "border-border bg-card",
                  dragOverIndex === index ? "outline outline-2 outline-primary" : "",
                ].join(" ")}
              >
                <div className="flex flex-wrap items-end gap-3">
                  <span
                    aria-hidden="true"
                    data-drag-handle={rule.key}
                    draggable={!disabled}
                    onDragStart={(event) => handleDragStart(event, index)}
                    onDragEnd={() => {
                      dragFromRef.current = null;
                      setDragOverIndex(null);
                    }}
                    className="flex cursor-grab items-center self-center text-muted-foreground"
                  >
                    <GripVertical className="size-4" />
                  </span>

                  <Badge variant="secondary" className="self-center tabular-nums">
                    {index + 1} of {rules.length}
                  </Badge>

                  <SegmentedField
                    label={<span className="sr-only">Decision for rule {index + 1}</span>}
                    value={rule.decision}
                    onValueChange={(next) => patch(index, { decision: next as SelectionDecision })}
                    options={DECISION_OPTIONS}
                    disabled={disabled}
                  />

                  <SegmentedField
                    label={<span className="sr-only">Matcher for rule {index + 1}</span>}
                    value={rule.matcherKind}
                    onValueChange={(next) => patch(index, { matcherKind: next as MatcherKind })}
                    options={matcherOptions(support)}
                    disabled={disabled}
                  />

                  <div className="min-w-56 flex-1">
                    <FieldRow
                      label={<span className="sr-only">Pattern for rule {index + 1}</span>}
                      description={`${MATCHER_HINT[rule.matcherKind]}${
                        rule.matcherKind === "glob"
                          ? ` Shape: ${SCOPE_QUALIFIED_NAME_SHAPE[scope]}.`
                          : ""
                      }`}
                      error={patternErrors[rule.key]}
                    >
                      <Input
                        value={rule.pattern}
                        disabled={disabled}
                        placeholder={rule.matcherKind === "glob" ? SCOPE_QUALIFIED_NAME_SHAPE[scope] : ""}
                        onChange={(event) => patch(index, { pattern: event.target.value })}
                      />
                    </FieldRow>
                  </div>

                  <div className="flex items-center gap-1 self-center">
                    <IconButton
                      label={`Move rule ${index + 1} up`}
                      icon={<ChevronUp />}
                      variant="ghost"
                      disabled={disabled || index === 0}
                      onClick={() => commitMove(index, index - 1)}
                    />
                    <IconButton
                      label={`Move rule ${index + 1} down`}
                      icon={<ChevronDown />}
                      variant="ghost"
                      disabled={disabled || index === rules.length - 1}
                      onClick={() => commitMove(index, index + 1)}
                    />
                    <IconButton
                      label={`Remove rule ${index + 1}`}
                      icon={<Trash2 />}
                      variant="ghost"
                      disabled={disabled}
                      onClick={() => removeRule(index)}
                    />
                  </div>
                </div>

                {currentUnavailable && currentAvailability.state !== "available" ? (
                  <Alert variant="warning">
                    <AlertDescription>
                      This saved rule uses a matcher this source cannot evaluate, so it comes back
                      undetermined for every object rather than matching or not matching.{" "}
                      {currentAvailability.reason}
                    </AlertDescription>
                  </Alert>
                ) : null}

                {rule.pattern.trim() === "" ? (
                  <Text variant="caption" tone="muted">
                    This rule needs a pattern before it can be previewed or saved.
                  </Text>
                ) : null}
              </li>
            );
          })}
        </ol>
      )}

      {rules.length > 0 ? (
        <div>
          <Button variant="outline" onClick={addRule} disabled={disabled}>
            <Plus aria-hidden className="mr-1.5 size-4" />
            Add rule
          </Button>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={onSave} disabled={disabled || !dirty || saving}>
          {saving ? "Saving…" : "Save rules"}
        </Button>
        <Button variant="outline" onClick={onDiscard} disabled={disabled || !dirty || saving}>
          <Undo2 aria-hidden className="mr-1.5 size-4" />
          Discard draft
        </Button>
        <Text variant="caption" tone="muted">
          {dirty
            ? "Unsaved draft. The preview above is evaluating this draft; the source tree still shows the saved rules."
            : "No unsaved changes. The preview and the source tree both show the saved rules."}
        </Text>
      </div>

      {/* One polite live region for reorder/add/remove. Its text is a complete sentence and only
          changes when a move actually happens, so it announces once per action instead of on
          every render. */}
      <p role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </p>
    </div>
  );
}
