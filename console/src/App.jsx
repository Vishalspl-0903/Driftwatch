import { useEffect, useState } from "react";
import QueueView from "./components/QueueView.jsx";
import DetailView from "./components/DetailView.jsx";

export default function App() {
  const [pairs, setPairs] = useState(null);
  const [error, setError] = useState(null);
  const [selectedDomain, setSelectedDomain] = useState(null);

  useEffect(() => {
    fetch("/data/pairs.json")
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then(setPairs)
      .catch((e) => setError(String(e)));
  }, []);

  if (error) {
    return (
      <div className="p-8 text-red-600">
        Failed to load /data/pairs.json: {error}. Run{" "}
        <code className="bg-gray-100 px-1 rounded">
          python scripts/build_console_data.py
        </code>{" "}
        from the repo root first.
      </div>
    );
  }

  if (!pairs) {
    return <div className="p-8 text-gray-500">Loading…</div>;
  }

  const selected = pairs.find((p) => p.domain === selectedDomain);

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200 px-6 py-4">
        <h1 className="text-lg font-semibold text-gray-900">
          Driftwatch Console
        </h1>
        <p className="text-sm text-gray-500">
          Merchant re-review queue — deterministic drift score + VLM
          escalation evidence
        </p>
      </header>

      <main className="p-6">
        {selected ? (
          <DetailView pair={selected} onBack={() => setSelectedDomain(null)} />
        ) : (
          <QueueView pairs={pairs} onSelect={setSelectedDomain} />
        )}
      </main>
    </div>
  );
}
