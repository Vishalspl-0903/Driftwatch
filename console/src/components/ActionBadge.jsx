// Every action policy.yaml can hand out, plus the two non-policy terminal
// outcomes escalation_check emits directly (no_action / auto_flag /
// insufficient_data). `approve` is kept deliberately: policy.yaml still
// defines it but no rule reaches it (see that file's comment and FAILURES.md),
// and the badge should render it correctly rather than fall through if a
// policy edit ever makes it reachable again.
const STYLES = {
  no_action: "bg-gray-100 text-gray-600 border-gray-200",
  approve: "bg-emerald-50 text-emerald-700 border-emerald-200",
  auto_flag: "bg-red-50 text-red-700 border-red-200",
  flag_for_review: "bg-amber-50 text-amber-800 border-amber-200",
  escalate_further: "bg-amber-50 text-amber-800 border-amber-200",
  needs_manual_review: "bg-amber-50 text-amber-800 border-amber-200",
  insufficient_data: "bg-slate-100 text-slate-600 border-slate-200",
  escalated: "bg-sky-50 text-sky-700 border-sky-200",
};

const LABELS = {
  no_action: "No action",
  approve: "Approve",
  auto_flag: "Auto-flag",
  flag_for_review: "Flag for review",
  escalate_further: "Escalate further",
  needs_manual_review: "Needs manual review",
  insufficient_data: "Insufficient data",
  escalated: "Escalated",
};

export default function ActionBadge({ value, className = "" }) {
  if (!value) return <span className="text-gray-400">—</span>;
  const style = STYLES[value] || "bg-gray-100 text-gray-600 border-gray-200";
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${style} ${className}`}
    >
      {LABELS[value] || value}
    </span>
  );
}
