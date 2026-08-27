import ActionBadge from "./ActionBadge.jsx";

function reasonLine(pair) {
  switch (pair.path) {
    case "no_action":
      return "Drift score below the no-action threshold — no re-review needed.";
    case "auto_flag":
      return "Drift score above the auto-flag threshold — the precision=1.00 zone from validation, no VLM needed to confirm.";
    case "insufficient_data":
      return "No usable image-drift score (too few catalog images extracted on one side) — not a threshold case, routed straight to a distinct outcome rather than guessed.";
    case "escalated":
      return pair.policy_reason
        ? `Boundary-zone score → sent to the VLM → policy_adjudicate ${pair.policy_reason}.`
        : "Boundary-zone score → sent to the VLM for evidence.";
    case "parse_failed":
      return "Boundary-zone score → sent to the VLM, which never returned evidence that validated against the schema (both attempts). Routed to mandatory human review — never approved, never silently dropped.";
    default:
      return "";
  }
}

function EvidenceCard({ evidence }) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4">
      <div className="flex items-center gap-4 mb-3 text-sm">
        <div>
          <span className="text-gray-400">axis</span>{" "}
          <span className="font-semibold text-gray-900">{evidence.axis}</span>
        </div>
        <div>
          <span className="text-gray-400">confidence</span>{" "}
          <span className="font-semibold text-gray-900">{evidence.confidence}</span>
        </div>
        {evidence.vlm_cost_usd != null && (
          <div className="text-gray-400 ml-auto">
            ${evidence.vlm_cost_usd.toFixed(5)} · {evidence.vlm_latency_s}s ·{" "}
            {evidence.vlm_input_tokens}in/{evidence.vlm_output_tokens}out
          </div>
        )}
      </div>

      <div>
        <div className="text-xs font-medium text-gray-500 mb-1">Evidence pointer</div>
        <p className="text-sm text-gray-800 leading-relaxed bg-gray-50 border border-gray-100 rounded p-2">
          {evidence.evidence_pointer}
        </p>
      </div>
    </div>
  );
}

export default function DetailView({ pair, onBack }) {
  const escalation = pair.escalation;

  return (
    <div>
      <button
        onClick={onBack}
        className="text-sm text-gray-500 hover:text-gray-900 mb-4 flex items-center gap-1"
      >
        ← Back to queue
      </button>

      <div className="flex items-start justify-between mb-6">
        <div>
          <h2 className="text-xl font-semibold text-gray-900">{pair.domain}</h2>
          <p className="text-sm text-gray-500">
            t0 {pair.t0_date} → t1 {pair.t1_date} · gap {pair.gap_days} days
          </p>
        </div>
        <div className="text-right">
          <div className="text-xs text-gray-400 mb-1">Final action</div>
          <ActionBadge value={pair.final_action} className="text-sm px-3 py-1" />
        </div>
      </div>

      <div className="bg-white border border-gray-200 rounded-lg p-4 mb-6">
        <div className="flex flex-wrap items-center gap-6">
          <div>
            <div className="text-xs text-gray-400">Drift score (CLIP image centroid)</div>
            <div className="text-lg font-semibold text-gray-900 tabular-nums">
              {pair.drift_score != null ? pair.drift_score.toFixed(4) : "n/a — no usable image score"}
            </div>
          </div>
          <div className="flex-1 min-w-[240px]">
            <div className="text-xs text-gray-400">Why this action</div>
            <div className="text-sm text-gray-700">{reasonLine(pair)}</div>
          </div>
        </div>
      </div>

      {escalation && (
        <div className="mb-6">
          <h3 className="text-sm font-medium text-gray-900 mb-3">VLM evidence packet</h3>
          <EvidenceCard evidence={escalation} />
        </div>
      )}

      {pair.ground_truth?.fits_axis && (
        <div className="mb-6 rounded-lg border border-gray-200 bg-white p-4">
          <div className="text-xs font-medium text-gray-500 mb-1">
            Hand-labeled ground truth (evals/eyeball_notes.csv)
          </div>
          <div className="text-sm text-gray-800">
            <span className="font-semibold">{pair.ground_truth.fits_axis}</span>{" "}
            <span className="text-gray-400">({pair.ground_truth.confidence} confidence)</span>
            {" — "}
            {pair.ground_truth.what_changed}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {["t0", "t1"].map((side) => (
          <div key={side}>
            <h3 className="text-sm font-medium text-gray-700 mb-2">
              {side.toUpperCase()} — {side === "t0" ? pair.t0_date : pair.t1_date}
            </h3>
            <div className="border border-gray-200 rounded-lg overflow-y-auto max-h-[640px] bg-black">
              {pair.screenshots[side] ? (
                <img
                  src={`/${pair.screenshots[side]}`}
                  alt={`${pair.domain} ${side}`}
                  className="w-full block"
                />
              ) : (
                <div className="p-8 text-center text-gray-400 text-sm">no screenshot</div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
