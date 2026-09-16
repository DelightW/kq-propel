function metricBox(value, label) {
  return `<div class="metric-box"><div class="value">${value}</div><div class="label">${label}</div></div>`;
}

function barRow(label, value) {
  const pct = Math.round(value * 100);
  return `<div class="bar-row"><div style="width:160px;">${label}</div>
    <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
    <div style="width:50px;text-align:right;">${pct}%</div></div>`;
}

async function loadOverview() {
  const res = await fetch("/api/admin/overview");
  const data = await res.json();

  document.getElementById("metrics-grid").innerHTML = [
    metricBox(data.vector_store.documents, "Policy Documents Indexed"),
    metricBox(data.vector_store.chunks, "Vector Chunks (RAG Index)"),
    metricBox(data.transactions.count, "M-Pesa Transactions"),
    metricBox("Ksh " + Number(data.transactions.total_amount_ksh).toLocaleString(), "Total Value Processed"),
  ].join("");

  document.getElementById("rag-bars").innerHTML = [
    barRow("Context Relevance", data.rag_triad.context_relevance),
    barRow("Groundedness", data.rag_triad.groundedness),
    barRow("Answer Relevance", data.rag_triad.answer_relevance),
    barRow("Hallucination Rate", data.rag_triad.hallucination_rate),
  ].join("") + `<p style="font-size:12px;color:#777;">Based on ${data.rag_triad.sample_size} logged conversation turns.</p>`;

  const sm = data.sentiment_model_metrics;
  document.getElementById("sentiment-metrics").innerHTML = [
    metricBox((sm.accuracy * 100).toFixed(1) + "%", "Accuracy"),
    metricBox((sm.precision * 100).toFixed(1) + "%", "Precision"),
    metricBox((sm.recall * 100).toFixed(1) + "%", "Recall"),
    metricBox((sm.f1_score * 100).toFixed(1) + "%", "F1-Score"),
  ].join("");

  const dist = data.sentiment_distribution;
  const total = Object.values(dist).reduce((a, b) => a + b, 0) || 1;
  let distHtml = "<h4 style='color:var(--kq-red-dark);'>Live Passenger Sentiment Distribution</h4>";
  for (const [label, count] of Object.entries(dist)) {
    distHtml += barRow(label, count / total);
  }
  document.getElementById("sentiment-dist").innerHTML = distHtml || "<p>No conversations logged yet.</p>";

  const tbody = document.querySelector("#transactions-table tbody");
  tbody.innerHTML = data.transactions.recent.map(t => `
    <tr>
      <td>${t.created_at.replace("T", " ").slice(0, 19)}</td>
      <td>${t.reference || "-"}</td>
      <td>${t.phone_number || "-"}</td>
      <td>${Number(t.amount || 0).toLocaleString()}</td>
      <td>${t.status}</td>
    </tr>`).join("") || "<tr><td colspan='5'>No transactions yet.</td></tr>";
}

async function runComparison() {
  const container = document.getElementById("model-comparison-results");
  container.innerHTML = "<p>Running both models across the evaluation dataset...</p>";
  const res = await fetch("/api/admin/model-comparison");
  const data = await res.json();
  container.innerHTML = data.results.map(row => `
    <div class="model-compare-card">
      <h4>${row.query}</h4>
      ${row.models.map(m => `
        <div class="model-answer">
          <strong>${m.model}</strong> — Context Relevance: ${m.context_relevance}, Groundedness: ${m.groundedness}, Answer Relevance: ${m.answer_relevance}
          <p>${m.response || m.error || ""}</p>
        </div>`).join("")}
    </div>`).join("");
}

loadOverview();
