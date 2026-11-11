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
  /* The server issues this. The client used to generate its own identifier
     and the server trusted it, which meant anyone who supplied another
     passenger's string inherited their conversation and their pending
     payment. It is now held only in memory, backed by an HttpOnly cookie
     the page itself cannot read. */
  var sessionId = null;

  function startSession() {
    return fetch("/api/chat/session", {
      method: "POST",
      credentials: "same-origin"
    })
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(function (d) {
        sessionId = d.session_id;
        return d;
      });
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
    var blocks = String(text).split(/\n{2,}/);
    blocks.forEach(function (block) {
      if (!block.trim()) { return; }
      var para = document.createElement("p");
      para.className = "msg-p";
      // A leading "- " marks a bullet in the payment offer; render it as one
      // rather than leaving the dash in the prose.
      var lines = block.split("\n");
      var allBullets = lines.length > 1 && lines.every(function (l) {
        return l.trim().indexOf("- ") === 0;
      });
      if (allBullets) {
        var ul = document.createElement("ul");
        ul.className = "msg-list";
        lines.forEach(function (l) {
          var li = document.createElement("li");
          appendInline(li, l.trim().slice(2));
          ul.appendChild(li);
        });
        parent.appendChild(ul);
        return;
      }
      lines.forEach(function (l, j) {
        if (j) { para.appendChild(document.createElement("br")); }
        appendInline(para, l);
      });
      parent.appendChild(para);
    });
  }

  function appendInline(parent, text) {
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

  function splitOutSources(text) {
    // The cited source can appear anywhere in the answer, not only at the end
    // - a payment offer is appended after it. Pulling every "Source:" line out
    // by position rather than assuming it is last keeps the remaining prose
    // formatted instead of dumping it into the citation.
    var cited = [];
    var kept = [];
    String(text).split("\n").forEach(function (line) {
      if (/^\s*Sources?:/i.test(line)) {
        cited.push(line.replace(/^\s*Sources?:\s*/i, "").trim());
      } else {
        kept.push(line);
      }
    });
    return { body: kept.join("\n").replace(/\n{3,}/g, "\n\n").trim(), cited: cited };
  }

  function buildSourceDisclosure(cited, retrieved) {
    // Citations stay one click away rather than on screen permanently. They
    // are never removed: an answer the user cannot trace back to a policy
    // section is the exact failure mode this project is built to avoid.
    var details = document.createElement("details");
    details.className = "src-details";

    var summary = document.createElement("summary");
    summary.className = "src-summary";
    summary.textContent = cited.length
      ? (cited.length === 1 ? "Source" : cited.length + " sources")
      : "Sources retrieved";
    details.appendChild(summary);

    var body = document.createElement("div");
    body.className = "src-body";

    cited.forEach(function (c) {
      var row = document.createElement("div");
      row.className = "src-cited";
      row.textContent = c;
      body.appendChild(row);
    });

    var others = (retrieved || []).filter(function (r) {
      return cited.indexOf(r) === -1;
    });
    if (others.length) {
      var lab = document.createElement("div");
      lab.className = "src-other-label";
      lab.textContent = "Also retrieved, not quoted";
      body.appendChild(lab);
      others.forEach(function (o) {
        var row = document.createElement("div");
        row.className = "src-other";
        row.textContent = o;
        body.appendChild(row);
      });
    }

    details.appendChild(body);
    return details;
  }

  function addBubble(role, text, meta) {
    var bubble = document.createElement("div");
    bubble.className = "bubble " + role;

    var speaker = document.createElement("span");
    speaker.className = "sr-only";
    speaker.textContent = role === "user" ? "You said: " : (role === "agent" ? "" : "Assistant: ");
    if (speaker.textContent) { bubble.appendChild(speaker); }

    if (role === "agent") {
      var who = document.createElement("span");
      who.className = "who";
      who.textContent = "Kenya Airways support team";
      bubble.appendChild(who);
    }

    var split = splitOutSources(text || "");
    appendRichText(bubble, split.body);

    var retrieved = (meta && meta.sources) || [];
    if (split.cited.length || retrieved.length) {
      bubble.appendChild(buildSourceDisclosure(split.cited, retrieved));
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

  var PAYMENT_STATUS_TEXT = {
    pending: "Waiting for confirmation - not paid yet.",
    paid: "Paid. Thank you - your payment has been confirmed.",
    failed: "Payment failed. You have not been charged.",
    cancelled: "Payment cancelled. You have not been charged.",
    expired: "No confirmation arrived in time. You have not been charged.",
    needs_review: "We received a payment that needs checking. Our team will follow up.",
    simulated: "Demonstration only - no money was requested or taken."
  };

  function showPaymentStatus(line, status) {
    line.textContent = PAYMENT_STATUS_TEXT[status] || "Payment status unknown.";
    line.className = "pay-status " + status;
  }

  /* A confirmed payment gets a PDF receipt. The link is served only to this
     session, and only once the ledger says paid. */
  function addReceiptLink(card, reference) {
    if (card.querySelector(".pay-receipt")) { return; }
    var link = document.createElement("a");
    link.className = "pay-receipt";
    link.href = "/api/chat/payments/" + encodeURIComponent(reference) + "/receipt";
    link.setAttribute("download", "KQ-Propel-receipt-" + reference + ".pdf");
    link.textContent = "Download receipt (PDF)";
    card.appendChild(link);
  }

  function pollPayment(reference, line, card) {
    var started = Date.now();
    var LIMIT_MS = 15 * 60 * 1000;
    function tick() {
      fetch("/api/chat/payments/" + encodeURIComponent(reference), {
        credentials: "same-origin"
      }).then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      }).then(function (d) {
        showPaymentStatus(line, d.status);
        if (d.status === "paid") { addReceiptLink(card, reference); }
        if (d.status === "failed" || d.status === "needs_review") {
          card.classList.add("failed");
        }
        if (!d.final && Date.now() - started < LIMIT_MS) {
          setTimeout(tick, 4000);
        }
      }).catch(function () {
        // A network hiccup is not a payment outcome: keep the last known
        // status and try again a little later.
        if (Date.now() - started < LIMIT_MS) { setTimeout(tick, 8000); }
      });
    }
    setTimeout(tick, 4000);
  }

  /* Staff replies arrive while a handover case is open. Polling starts when
     the assistant opens a case, and once on page load in case a reply came
     in while the page was closed; it stops when the case is closed. */
  var handoverPolling = false;
  var lastAgentMessageId = 0;

  function pollHandover() {
    if (handoverPolling) { return; }
    handoverPolling = true;
    function tick() {
      fetch("/api/chat/handover?after=" + lastAgentMessageId, {
        credentials: "same-origin"
      }).then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      }).then(function (d) {
        (d.messages || []).forEach(function (m) {
          lastAgentMessageId = Math.max(lastAgentMessageId, m.id);
          addBubble("agent", m.message);
        });
        if (d.handover) {
          setTimeout(tick, 5000);
        } else {
          handoverPolling = false;
        }
      }).catch(function () {
        setTimeout(tick, 10000);
      });
    }
    tick();
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

      // The request being sent is not the payment being made. The status
      // line follows the ledger until the payment reaches a final state.
      var statusLine = document.createElement("div");
      statusLine.className = "pay-status";
      statusLine.setAttribute("role", "status");
      card.appendChild(statusLine);
      showPaymentStatus(statusLine, simulated ? "simulated" : (p.status || "pending"));
      if (!simulated && p.reference && (p.status || "pending") === "pending") {
        pollPayment(p.reference, statusLine, card);
      } else if (!simulated && p.reference && p.status === "paid") {
        addReceiptLink(card, p.reference);
      }
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
    bubble.innerHTML = '<span class="sr-only">The assistant is typing</span>' +
      '<span class="typing" aria-hidden="true"><span></span><span></span><span></span></span>';
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
    traceToggle.setAttribute("aria-expanded", "true");
    if (!isDesktop()) { scrim.hidden = false; traceClose.focus(); }
  }

  function closeTrace() {
    var wasOverlay = !isDesktop() && tracePanel.classList.contains("open");
    tracePanel.classList.remove("open");
    if (isDesktop()) { tracePanel.classList.add("collapsed"); }
    traceToggle.setAttribute("aria-expanded", "false");
    scrim.hidden = true;
    if (wasOverlay) { traceToggle.focus(); }
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
    traceToggle.setAttribute("aria-expanded", "true");
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
    chatWindow.setAttribute("aria-busy", state ? "true" : "false");
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
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: message })
    })
      .then(function (res) {
        if (res.status === 429) {
          return res.json().then(function (body) {
            var e = new Error((body && body.detail) || "Too many messages. Please wait a moment.");
            e.limited = true;
            throw e;
          });
        }
        if (!res.ok) { throw new Error("Server responded with " + res.status); }
        return res.json();
      })
      .then(function (data) {
        if (data && data.session_id) { sessionId = data.session_id; }
        return data;
      })
      .then(function (data) {
        typing.remove();
        addBubble("bot", data.answer || "I could not produce an answer for that.", data);

        if (data.payment) {
          addPaymentCard(data.payment);
        }
        if (data.handover) {
          pollHandover();
        }

        renderTrace(data);
        setStatus(true, data.model_used || "Aviation Support Assistant");
        setBusy(false);
      })
      .catch(function (err) {
        typing.remove();
        if (err.limited) {
          addBubble("bot", "You're sending messages faster than I can take them. " + err.message);
          setBusy(false);
          return;
        }
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

  /* --------------------------- data erasure ---------------------------- */

  /* Deletes this conversation on the server (messages, memory, any payment
     offer or handover case; payment records keep their amounts but lose the
     phone number) and starts afresh. */
  var forgetBtn = document.getElementById("forget-btn");
  forgetBtn.addEventListener("click", function () {
    if (busy) { return; }
    if (!window.confirm("Delete this conversation and everything linked to it? " +
                        "This cannot be undone.")) { return; }
    setBusy(true);
    fetch(API + "/forget", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true })
    })
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(function (d) {
        sessionId = d.session_id;
        lastAgentMessageId = 0;
        chatWindow.innerHTML = "";
        traceBody.innerHTML = "";
        addBubble("bot",
          "Your conversation has been deleted. Any payment record keeps its amount and " +
          "reference, as the airline must, but no longer holds your phone number.\n\n" +
          "You can start a new question whenever you like.");
        setBusy(false);
      })
      .catch(function (err) {
        addBubble("bot", "I couldn't delete the conversation just now (" + err.message +
                         "). Please try again.");
        setBusy(false);
      });
  });

  /* ------------------------------ greeting ------------------------------ */

  addBubble("bot",
    "Hello, and welcome to KQ-Propel.\n\n" +
    "I can help you with baggage allowances and overweight fees, flight status, " +
    "delay and cancellation entitlements, refunds, check-in times, and M-Pesa payments.\n\n" +
    "What would you like to know?");

  /* The composer stays disabled until the server has issued a session, so a
     message can never be sent without one. */
  setBusy(true);
  startSession()
    .then(function (d) {
      setBusy(false);
      input.focus();
      if (d && d.resumed) { pollHandover(); }
    })
    .catch(function (err) {
      setBusy(false);
      setStatus(false, "Reconnecting...");
      addBubble("bot",
        "I couldn't start a secure session (" + err.message +
        "). Please reload the page.");
    });
})();
