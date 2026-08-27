import { useMemo, useState } from "react";
import ActionBadge from "./ActionBadge.jsx";

export default function QueueView({ pairs, onSelect }) {
  const [sortDir, setSortDir] = useState("desc"); // by drift score
  const [actionFilter, setActionFilter] = useState("all");

  const actions = useMemo(
    () => Array.from(new Set(pairs.map((p) => p.final_action).filter(Boolean))),
    [pairs]
  );

  const rows = useMemo(() => {
    let out = pairs;
    if (actionFilter !== "all") {
      out = out.filter((p) => p.final_action === actionFilter);
    }
    out = [...out].sort((a, b) => {
      // nulls (no usable score) always sort last, regardless of direction
      if (a.drift_score == null && b.drift_score == null) return 0;
      if (a.drift_score == null) return 1;
      if (b.drift_score == null) return -1;
      return sortDir === "desc" ? b.drift_score - a.drift_score : a.drift_score - b.drift_score;
    });
    return out;
  }, [pairs, actionFilter, sortDir]);

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-base font-medium text-gray-900">
          Re-review queue
          <span className="ml-2 text-sm font-normal text-gray-400">
            {rows.length} of {pairs.length} pairs
          </span>
        </h2>
        <label className="text-sm text-gray-600 flex items-center gap-2">
          Filter by action
          <select
            className="border border-gray-300 rounded-md px-2 py-1 text-sm bg-white"
            value={actionFilter}
            onChange={(e) => setActionFilter(e.target.value)}
          >
            <option value="all">All</option>
            {actions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500 border-b border-gray-200">
            <tr>
              <th className="text-left font-medium px-4 py-2">Domain</th>
              <th
                className="text-left font-medium px-4 py-2 cursor-pointer select-none"
                onClick={() => setSortDir(sortDir === "desc" ? "asc" : "desc")}
              >
                Drift score {sortDir === "desc" ? "↓" : "↑"}
              </th>
              <th className="text-left font-medium px-4 py-2">Path</th>
              <th className="text-left font-medium px-4 py-2">VLM axis / confidence</th>
              <th className="text-left font-medium px-4 py-2">Final action</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const evidence = p.escalation;
              return (
                <tr
                  key={p.domain}
                  onClick={() => onSelect(p.domain)}
                  className="border-b border-gray-100 last:border-0 hover:bg-gray-50 cursor-pointer"
                >
                  <td className="px-4 py-3 font-medium text-gray-900">{p.domain}</td>
                  <td className="px-4 py-3 text-gray-700 tabular-nums">
                    {p.drift_score != null ? p.drift_score.toFixed(4) : (
                      <span className="text-gray-400">n/a</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{p.path}</td>
                  <td className="px-4 py-3 text-gray-700">
                    {evidence ? `${evidence.axis} / ${evidence.confidence}` : (
                      <span className="text-gray-400">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <ActionBadge value={p.final_action} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
