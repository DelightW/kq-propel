/* KQ-Propel chat assistant client.
   Standalone: talks only to POST /api/chat. No admin coupling. */
(function () {
  "use strict";

  var API = "/api/chat";

  var chatWindow = document.getElementById("chat-window");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var sendBtn = document.getElementById("send-btn");
  var chips = document.getElementById("chips");
  var tracePanel = document.getElementById("trace-panel");
  var traceBody = document.getElementById("trace-body");
  var traceToggle = document.getElementById("trace-toggle");
  var traceClose = document.getElementById("trace-close");
  var scrim = document.getElementById("scrim");
  var statusDot = document.getElementById("status-dot");
  var statusText = document.getElementById("status-text");

  var busy = false;
  var sessionId = getSessionId();

  function getSessionId() {
    var key = "kqpropel_session";
    var id = null;
    try { id = window.localStorage.getItem(key); } catch (e) { /* private mode */ }
    if (!id) {
      id = "web-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
      try { window.localStorage.setItem(key, id); } catch (e) { /* ignore */ }
    }
    return id;
  }

  function isDesktop() {
    return window.matchMedia("(min-width: 1024px)").matches;
  }

  function scrollToBottom() {
    chatWindow.scrollTop = chatWindow.scrollHeight;
  }

  /* ----------------------------- rendering ----------------------------- */

  function appendRichText(parent, text) {
    // Minimal inline formatting: **bold** only. Built with text nodes so no
    // message content is ever injected as HTML.
    var parts = String(text).split(/(\*\*[^*]+\*\*)/g);
    parts.forEach(function (part) {
      if (!part) { return; }
      if (part.length > 4 && part.slice(0, 2) === "**" && part.slice(-2) === "**") {
        var b = document.createElement("strong");
        b.textContent = part.slice(2, -2);
        parent.appendChild(b);
      } else {
        parent.appendChild(document.createTextNode(part));
      }
    });
  }

  function addBubble(role, text, meta) {
    var bubble = document.createElement("div");
    bubble.className = "bubble " + role;

    var body = text || "";
    var source = "";
    var idx = body.lastIndexOf("\n\nSource:");
    if (idx !== -1) {
      source = body.slice(idx + 2);
      body = body.slice(0, idx);
    }

    appendRichText(bubble, body);

    if (source) {
      var s = document.createElement("span");
      s.className = "src";
      s.textContent = source;
      bubble.appendChild(s);
    }

    if (meta && meta.sentiment && meta.sentiment.label === "frustrated") {
      var tone = document.createElement("span");
      tone.className = "tone frustrated";
      tone.textContent = "Priority support - frustration detected";
      bubble.appendChild(tone);
    }

    chatWindow.appendChild(bubble);
    scrollToBottom();
    return bubble;
  }

  function addPaymentCard(p) {
    var card = document.createElement("div");
    card.className = "pay-card" + (p.success ? "" : " failed");

    var isCard = (p.method || "mpesa") === "card";
    var currency = p.currency || "KES";
    var simulated = String(p.source || "").indexOf("simulated") === 0;

    var head = document.createElement("div");
    head.className = "pay-head";
    var logo = document.createElement("span");
    logo.className = "pay-logo";
    logo.textContent = isCard ? "CARD" : "M-PESA";
    var title = document.createElement("span");
    title.className = "pay-title";
    if (p.success) {
      title.textContent = isCard ? "Checkout link ready" : "Payment request sent";
    } else {
      title.textContent = isCard ? "Checkout could not be created" : "Payment request failed";
    }
    head.appendChild(logo);
    head.appendChild(title);
    card.appendChild(head);

    if (p.success) {
      var amt = document.createElement("div");
      amt.className = "pay-amount";
      amt.textContent = currency === "KES"
        ? "Ksh " + Number(p.amount || 0).toLocaleString()
        : currency + " " + Number(p.amount || 0).toLocaleString(undefined,
            { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      card.appendChild(amt);

      var rows;
      if (isCard) {
        rows = [
          ["Reference", p.reference || "-"],
          ["Order ID", p.order_id || "-"],
          ["Charged in", currency + " (the published currency - no conversion applied)"],
          ["Channel", simulated ? "Card gateway (simulated)" : "PayPal card checkout"]
        ];
      } else {
        rows = [
          ["To", p.phone_number || "-"],
          ["Reference", p.reference || "-"],
          ["Checkout ID", p.checkout_request_id || "-"],
          ["Channel", simulated ? "Daraja (simulated)" : "Safaricom Daraja"]
        ];
      }
      var list = document.createElement("div");
      list.className = "pay-rows";
      rows.forEach(function (r) {
        var row = document.createElement("div");
        row.className = "pay-row";
        var k = document.createElement("span");
        k.textContent = r[0];
        var v = document.createElement("span");
        v.textContent = r[1];
        row.appendChild(k);
        row.appendChild(v);
        list.appendChild(row);
      });
      card.appendChild(list);

      if (isCard && p.approval_url) {
        var link = document.createElement("a");
        link.className = "pay-link";
        link.href = p.approval_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = simulated
          ? "Open simulated checkout"
          : "Open secure checkout";
        card.appendChild(link);
      }

      var note = document.createElement("div");
      note.className = "pay-note";
      if (isCard) {
        note.textContent = simulated
          ? "Demonstration mode: no card gateway credentials are configured, so this is a "
            + "simulated checkout rather than a live one."
          : "Enter your card details on the checkout page. Nothing is entered in this chat.";
      } else {
        note.textContent = simulated
          ? "Demonstration mode: no Daraja credentials are configured, so this prompt is "
            + "simulated rather than a live Safaricom request."
          : "Enter your M-Pesa PIN on your phone to authorise this payment.";
      }
      card.appendChild(note);
    } else {
      var err = document.createElement("div");
      err.className = "pay-note";
      err.textContent = p.error || "The payment prompt could not be delivered.";
      card.appendChild(err);
    }

    chatWindow.appendChild(card);
    scrollToBottom();
  }

  function addTyping() {
    var bubble = document.createElement("div");
    bubble.className = "bubble bot";
    bubble.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
    chatWindow.appendChild(bubble);
    scrollToBottom();
    return bubble;
  }

  function step(label, text, code) {
    var el = document.createElement("div");
    el.className = "step";
    var l = document.createElement("span");
    l.className = "label";
    l.textContent = label;
    el.appendChild(l);
    if (text) { el.appendChild(document.createTextNode(text)); }
    if (code) {
      var c = document.createElement("code");
      c.textContent = code;
      el.appendChild(c);
    }
    return el;
  }

  function renderTrace(data) {
    traceBody.innerHTML = "";

    var trace = data.trace || [];
    for (var i = 0; i < trace.length; i++) {
      var t = trace[i];
      if (t.step === "action") {
        var detail = "";
        if (Array.isArray(t.observation)) {
          for (var j = 0; j < t.observation.length; j++) {
            var o = t.observation[j];
            detail += (o.section || o.source || "result") +
                      (o.score !== undefined ? "  (score " + o.score + ")" : "") + "\n";
          }
        } else if (t.observation && typeof t.observation === "object") {
          detail = JSON.stringify(t.observation, null, 1);
        } else if (t.observation !== undefined) {
          detail = String(t.observation);
        }
        traceBody.appendChild(step("Action - " + t.tool, t.input ? String(t.input) : "", detail.trim()));
      } else if (t.step === "final_answer") {
        traceBody.appendChild(step("Final answer - " + (t.model || "model"), t.content || ""));
      } else {
        traceBody.appendChild(step(t.step || "step", t.content || ""));
      }
    }

    if (data.sentiment) {
      traceBody.appendChild(step(
        "Sentiment model",
        data.sentiment.label + " (confidence " + data.sentiment.confidence +
        ", frustration " + data.sentiment.frustration_score + ")"
      ));
    }

    var m = data.rag_metrics || {};
    if (m.context_relevance !== undefined) {
      var wrap = document.createElement("div");
      wrap.className = "step";
      var lab = document.createElement("span");
      lab.className = "label";
      lab.textContent = "RAG-Triad evaluation";
      wrap.appendChild(lab);

      var meter = document.createElement("div");
      meter.className = "meter";
      meter.appendChild(meterRow("Context rel.", m.context_relevance));
      meter.appendChild(meterRow("Groundedness", m.groundedness));
      meter.appendChild(meterRow("Answer rel.", m.answer_relevance));
      wrap.appendChild(meter);
      traceBody.appendChild(wrap);
    }

    if (!traceBody.childNodes.length) {
      traceBody.innerHTML = '<p class="trace-empty">No reasoning steps for this message.</p>';
    }
    traceBody.scrollTop = 0;
  }

  function meterRow(label, value) {
    var v = Math.max(0, Math.min(1, Number(value) || 0));
    var row = document.createElement("div");
    row.className = "meter-row";

    var l = document.createElement("span");
    l.className = "m-label";
    l.textContent = label;

    var track = document.createElement("span");
    track.className = "meter-track";
    var fill = document.createElement("span");
    fill.className = "meter-fill";
    fill.style.width = (v * 100).toFixed(0) + "%";
    track.appendChild(fill);

    var val = document.createElement("span");
    val.className = "m-val";
    val.textContent = v.toFixed(2);

    row.appendChild(l);
    row.appendChild(track);
    row.appendChild(val);
    return row;
  }

  /* --------------------------- trace panel UI --------------------------- */

  function openTrace() {
    tracePanel.classList.add("open");
    tracePanel.classList.remove("collapsed");
    if (!isDesktop()) { scrim.hidden = false; }
  }

  function closeTrace() {
    tracePanel.classList.remove("open");
    if (isDesktop()) { tracePanel.classList.add("collapsed"); }
    scrim.hidden = true;
  }

  function toggleTrace() {
    var shown = isDesktop()
      ? !tracePanel.classList.contains("collapsed")
      : tracePanel.classList.contains("open");
    if (shown) { closeTrace(); } else { openTrace(); }
  }

  traceToggle.addEventListener("click", toggleTrace);
  traceClose.addEventListener("click", closeTrace);
  scrim.addEventListener("click", closeTrace);

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !isDesktop()) { closeTrace(); }
  });

  // On phones the panel starts hidden; on laptops it starts docked open.
  if (isDesktop()) {
    tracePanel.classList.remove("open");
  } else {
    tracePanel.classList.add("collapsed");
  }

  window.addEventListener("resize", function () {
    if (isDesktop()) {
      scrim.hidden = true;
      tracePanel.classList.remove("open");
    } else if (!tracePanel.classList.contains("open")) {
      scrim.hidden = true;
    }
  });

  /* ------------------------------ sending ------------------------------ */

  function setBusy(state) {
    busy = state;
    sendBtn.disabled = state;
    input.disabled = state;
    if (!state) { input.focus(); }
  }

  function send(text) {
    var message = (text !== undefined ? text : input.value).trim();
    if (!message || busy) { return; }

    addBubble("user", message);
    input.value = "";
    setBusy(true);

    var typing = addTyping();

    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: message })
    })
      .then(function (res) {
        if (!res.ok) { throw new Error("Server responded with " + res.status); }
        return res.json();
      })
      .then(function (data) {
        typing.remove();
        addBubble("bot", data.answer || "I could not produce an answer for that.", data);

        if (data.payment) {
          addPaymentCard(data.payment);
        }

        renderTrace(data);
        setStatus(true, data.model_used || "Aviation Support Assistant");
        setBusy(false);
      })
      .catch(function (err) {
        typing.remove();
        addBubble("bot",
          "I couldn't reach the support service just now (" + err.message +
          "). Please check your connection and try again.");
        setStatus(false, "Reconnecting...");
        setBusy(false);
      });
  }

  function setStatus(online, label) {
    statusText.textContent = label;
    if (online) { statusDot.classList.remove("offline"); }
    else { statusDot.classList.add("offline"); }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    send();
  });

  chips.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-q]");
    if (!btn) { return; }
    send(btn.getAttribute("data-q"));
  });

  /* ------------------------------ greeting ------------------------------ */

  addBubble("bot",
    "Hello, and welcome to KQ-Propel.\n\n" +
    "I can help you with baggage allowances and overweight fees, flight status, " +
    "delay and cancellation entitlements, refunds, check-in times, and M-Pesa payments.\n\n" +
    "What would you like to know?");

  input.focus();
})();
