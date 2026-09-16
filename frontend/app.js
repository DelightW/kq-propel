const sessionId = "session-" + Math.random().toString(36).slice(2, 10);

function fillMessage(text) {
  document.getElementById("chat-input").value = text;
}

function appendBubble(role, text, metaText) {
  const win = document.getElementById("chat-window");
  const bubble = document.createElement("div");
  bubble.className = "bubble " + role;
  bubble.innerHTML = escapeHtml(text) + (metaText ? `<span class="meta">${metaText}</span>` : "");
  win.appendChild(bubble);
  win.scrollTop = win.scrollHeight;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.innerText = str;
  return div.innerHTML;
}

function renderTrace(data) {
  const panel = document.getElementById("trace-panel");
  let html = "";

  html += `<div class="trace-step"><span class="label">Sentiment</span><br/>
    <span class="badge ${data.sentiment.label}">${data.sentiment.label.toUpperCase()}</span>
    &nbsp; frustration score: ${data.sentiment.frustration_score}</div>`;

  data.trace.forEach(step => {
    if (step.step === "thought") {
      html += `<div class="trace-step"><span class="label">Thought</span><br/>${escapeHtml(step.content)}</div>`;
    } else if (step.step === "action") {
      html += `<div class="trace-step"><span class="label">Action → ${step.tool}</span><br/>
        <code>${escapeHtml(JSON.stringify(step.input))}</code><br/>
        <small>Observation: ${escapeHtml(JSON.stringify(step.observation)).slice(0, 220)}...</small></div>`;
    } else if (step.step === "final_answer") {
      html += `<div class="trace-step"><span class="label">Final Answer (${escapeHtml(step.model)})</span></div>`;
    }
  });

  html += `<div class="trace-step"><span class="label">RAG-Triad Scores</span><br/>
    Context Relevance: ${data.rag_metrics.context_relevance} <br/>
    Groundedness: ${data.rag_metrics.groundedness} <br/>
    Answer Relevance: ${data.rag_metrics.answer_relevance}</div>`;

  html += `<div class="trace-step"><span class="label">Sources</span><br/>${data.sources.join(", ") || "none"}</div>`;

  if (data.payment) {
    html += `<div class="trace-step"><span class="label">M-Pesa Daraja STK Push</span><br/>${escapeHtml(data.payment.message || "")}</div>`;
  }

  panel.innerHTML = html;
}

async function sendMessage() {
  const input = document.getElementById("chat-input");
  const message = input.value.trim();
  if (!message) return;
  appendBubble("user", message);
  input.value = "";
  appendBubble("assistant", "Thinking...", "");

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message }),
    });
    const data = await res.json();
    const win = document.getElementById("chat-window");
    win.removeChild(win.lastChild);
    appendBubble("assistant", data.answer, `model: ${data.model_used} · sentiment: ${data.sentiment.label}`);
    renderTrace(data);
  } catch (err) {
    const win = document.getElementById("chat-window");
    win.removeChild(win.lastChild);
    appendBubble("assistant", "Sorry, something went wrong contacting KQ-Propel.");
    console.error(err);
  }
}

document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendMessage();
});

appendBubble("assistant", "Hello! I'm KQ-Propel, your aviation support assistant. Ask me about baggage, refunds, delays, flight status, or pay ancillary fees via M-Pesa.");
