/* KQ-Propel chat assistant client.
   Standalone: talks only to POST /api/chat. No admin coupling. */
(function () {
  "use strict";

  var API = "/api/chat";

  var chatWindow = document.getElementById("chat-window");
  var chatScroll = document.getElementById("chat-scroll");
  var welcome = document.getElementById("welcome");
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
    chatScroll.scrollTop = chatScroll.scrollHeight;
  }

  /* The suggested questions are shown until the first message is sent, and
     again after the conversation is deleted. */
  function showWelcome(show) {
    welcome.hidden = !show;
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
    // The cited sources can appear anywhere in the answer, not only at the end
    // - a payment offer is appended after them. The "Source:" lines are taken
    // out by position, and the text before and after them is kept apart so
    // the source list can sit between the answer and anything that follows.
    var cited = [];
    var before = [];
    var after = [];
    String(text).split("\n").forEach(function (line) {
      if (/^\s*Sources?:/i.test(line)) {
        splitCitations(line.replace(/^\s*Sources?:\s*/i, "")).forEach(function (c) {
          if (cited.indexOf(c) === -1) { cited.push(c); }
        });
      } else if (cited.length) {
        after.push(line);
      } else {
        before.push(line);
      }
    });
    function tidy(lines) { return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim(); }
    return { before: tidy(before), after: tidy(after), cited: cited };
  }

  // "A (version 1.0, ...); B (version 1.0, ...)" -> ["A (...)", "B (...)"]
  function splitCitations(line) {
    var parts = String(line).split(/\)\s*;\s*/);
    return parts.map(function (p, i) {
      p = p.trim();
      return i < parts.length - 1 ? p + ")" : p;
    }).filter(function (p) { return p; });
  }

  // "Baggage Allowance Policy - Section 1: Free Checked ... (version 1.0, as published 23 Sep 2026)"
  function parseSource(s) {
    var m = /^(.*?)\s+-\s+(Section\s+\d+:.*?)\s*\(([^()]*)\)\s*$/.exec(s);
    if (!m) { return { doc: s, section: "", edition: "" }; }
    return { doc: m[1], section: m[2], edition: m[3] };
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined && text !== null) { n.textContent = text; }
    return n;
  }

  /* Draws `text`, putting each card where the passage it stands for was. */
  function renderWithCards(parent, text, cards) {
    var pieces = [{ text: text }];
    (cards || []).forEach(function (card) {
      if (!card || !card.replaces) { return; }
      for (var i = 0; i < pieces.length; i++) {
        var p = pieces[i];
        if (p.text === undefined) { continue; }
        var at = p.text.indexOf(card.replaces);
        if (at === -1) { continue; }
        pieces.splice(i, 1, { text: p.text.slice(0, at) }, { card: card },
                      { text: p.text.slice(at + card.replaces.length) });
        card.placed = true;
        break;
      }
    });
    pieces.forEach(function (p) {
      if (p.card) {
        var built = buildCard(p.card);
        if (built) { parent.appendChild(built); }
      } else if (p.text && p.text.trim()) {
        appendRichText(parent, p.text.trim());
      }
    });
  }

  function addCitationMarkers(parent, count, idBase) {
    var paras = parent.querySelectorAll(".msg-p, .msg-list li");
    if (!paras.length || !count) { return; }
    var last = paras[paras.length - 1];
    for (var i = 1; i <= count; i++) {
      var a = el("a", "cite", String(i));
      a.href = "#" + idBase + "-" + i;
      a.setAttribute("aria-label", "Source " + i);
      last.appendChild(a);
    }
  }

  function buildSources(cited, idBase) {
    var box = el("div", "sources");
    box.appendChild(el("div", "sources-label", cited.length === 1 ? "Source" : "Sources"));
    cited.forEach(function (c, i) {
      var s = parseSource(c);
      var card = el("div", "source-card");
      card.id = idBase + "-" + (i + 1);
      card.appendChild(el("span", "source-num", String(i + 1)));
      var body = el("div");
      var doc = el("div", "source-doc", s.doc);
      if (/prototype/i.test(s.edition)) { doc.appendChild(el("span", "source-badge", "Prototype document")); }
      body.appendChild(doc);
      if (s.section) { body.appendChild(el("div", "source-sec", s.section)); }
      if (s.edition) {
        body.appendChild(el("div", "source-ed",
          s.edition.charAt(0).toUpperCase() + s.edition.slice(1)));
      }
      card.appendChild(body);
      box.appendChild(card);
    });
    return box;
  }

  function buildOthers(others, anyCited) {
    var details = el("details", "src-details");
    details.appendChild(el("summary", "src-summary",
      (anyCited ? "Also retrieved, not quoted" : "Sections retrieved") + " (" + others.length + ")"));
    var body = el("div", "src-body");
    others.forEach(function (o) { body.appendChild(el("div", "src-other", o)); });
    details.appendChild(body);
    return details;
  }

  /* ------------------------------- cards ------------------------------- */

  var PLANE_SVG = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" ' +
    'd="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z"/></svg>';
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function fmtUtc(s) {
    if (!s) { return "-"; }
    var d = new Date(s);   // "2026-11-12T08:36Z" and "2026-11-11T09:10:00+00:00" both parse
    if (isNaN(d.getTime())) { return String(s); }
    var hh = ("0" + d.getUTCHours()).slice(-2), mm = ("0" + d.getUTCMinutes()).slice(-2);
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + ", " + hh + ":" + mm + " UTC";
  }

  function fmtTimeUtc(s) {
    var full = fmtUtc(s);
    var m = /, (\d\d:\d\d UTC)$/.exec(full);
    return m ? m[1] : full;
  }

  function fmtDelay(min) {
    if (min === null || min === undefined) { return "-"; }
    if (Number(min) === 0) { return "None"; }
    var h = Math.floor(min / 60), m = min % 60;
    return h ? h + " h " + (m ? m + " min" : "") : m + " min";
  }

  function parseRoute(route) {
    return String(route || "").split(/\s*->\s*/).map(function (x) {
      var m = /^(.*?)\s*\((\w{3})\)\s*$/.exec(x.trim());
      return m ? { city: m[1], code: m[2] } : { city: x.trim(), code: "" };
    });
  }

  function statusPill(status, delay) {
    var s = String(status || "").toLowerCase();
    var late = Number(delay) > 0;
    var look = {
      landed: ["ok", "Landed"], arrived: ["ok", "Arrived"],
      active: ["info", "In the air"], "en-route": ["info", "In the air"], airborne: ["info", "In the air"],
      boarding: ["info", "Boarding"], departed: ["info", "Departed"],
      cancelled: ["bad", "Cancelled"], canceled: ["bad", "Cancelled"], diverted: ["bad", "Diverted"],
      delayed: ["warn", "Delayed"], scheduled: late ? ["warn", "Delayed"] : ["ok", "On time"]
    }[s] || ["", s ? s.charAt(0).toUpperCase() + s.slice(1) : "Unknown"];
    var pill = el("span", "pill " + look[0]);
    pill.appendChild(el("span", "pulse"));
    pill.appendChild(document.createTextNode(look[1]));
    return pill;
  }

  function routeBlock(fromCode, fromCity, toCode, toCity) {
    var route = el("div", "route");
    var a = el("div", "route-end");
    a.appendChild(el("span", "iata", fromCode || "-"));
    a.appendChild(el("span", "city", fromCity || ""));
    var path = el("div", "route-path");
    path.innerHTML = PLANE_SVG;
    var b = el("div", "route-end");
    b.appendChild(el("span", "iata", toCode || "-"));
    b.appendChild(el("span", "city", toCity || ""));
    route.appendChild(a);
    route.appendChild(path);
    route.appendChild(b);
    route.setAttribute("aria-label", "From " + (fromCity || fromCode) + " to " + (toCity || toCode));
    return route;
  }

  function facts(pairs) {
    var dl = el("dl", "facts");
    pairs.forEach(function (p) {
      var d = el("div");
      d.appendChild(el("dt", null, p[0]));
      d.appendChild(el("dd", null, p[1]));
      dl.appendChild(d);
    });
    return dl;
  }

  function cardShell(label, strong, right) {
    var card = el("section", "kq-card");
    card.setAttribute("aria-label", label + " " + strong);
    var head = el("div", "kq-card-head");
    var title = el("div", "kq-card-title");
    title.appendChild(document.createTextNode(label));
    title.appendChild(el("strong", null, strong));
    head.appendChild(title);
    if (right) { head.appendChild(right); }
    card.appendChild(head);
    var body = el("div", "kq-card-body");
    card.appendChild(body);
    return { card: card, body: body };
  }

  function flightCard(c) {
    var shell = cardShell("Flight", c.flight_number || "", statusPill(c.status, c.delay_minutes));
    var ends = parseRoute(c.route);
    var from = ends[0] || {}, to = ends[1] || {};
    shell.body.appendChild(routeBlock(from.code, from.city, to.code, to.city));
    shell.body.appendChild(facts([
      ["Gate", c.gate || "-"],
      ["Delay", fmtDelay(c.delay_minutes)],
      ["Est. departure", fmtUtc(c.estimated_departure_utc)]
    ]));
    var foot = el("div", "kq-card-foot");
    if (c.is_live) {
      foot.appendChild(el("span", "pill ok", "Live"));
      foot.appendChild(document.createTextNode(c.source_label +
        (c.retrieved_at_utc ? " · retrieved " + fmtTimeUtc(c.retrieved_at_utc) : "")));
    } else {
      foot.appendChild(el("span", "pill warn", "Demo"));
      foot.appendChild(document.createTextNode("Demonstration data, not live flight information"));
    }
    shell.card.appendChild(foot);
    return shell.card;
  }

  function bookingCard(c) {
    var shell = cardShell("Booking", c.reference || "",
      el("span", "pill " + (c.is_live ? "ok" : "warn"), c.is_live ? "Confirmed" : "Demo booking"));
    if (c.passengers && c.passengers.length) {
      var who = el("p", "passenger");
      who.appendChild(document.createTextNode("Passenger "));
      who.appendChild(el("strong", null, c.passengers.join(", ")));
      shell.body.appendChild(who);
    }
    (c.segments || []).forEach(function (s) {
      var seg = el("div", "segment");
      seg.appendChild(routeBlock(s.origin_code, s.origin, s.destination_code, s.destination));
      var bag = s.checked_baggage || {};
      seg.appendChild(facts([
        ["Flight", s.flight_number || "-"],
        ["Departs", fmtUtc(s.departure_utc)],
        ["Checked bags", bag.pieces && bag.weight_kg ? bag.pieces + " × " + bag.weight_kg + " kg" : "-"]
      ]));
      seg.appendChild(el("div", "segment-meta",
        [s.cabin, s.fare_brand].filter(Boolean).join(" · ")));
      shell.body.appendChild(seg);
    });
    if (!c.is_live) {
      var foot = el("div", "kq-card-foot");
      foot.appendChild(document.createTextNode("Sample data for the demonstration, not a real booking"));
      shell.card.appendChild(foot);
    }
    return shell.card;
  }

  function nameChangeCard(c) {
    var shell = cardShell("Name correction", c.reference || "",
      el("span", "pill info", "Waiting for you"));
    var names = el("div", "nc-names");
    names.appendChild(el("span", "nc-old", c.current_name));
    names.appendChild(el("span", "nc-arrow", "→"));
    names.appendChild(el("span", "nc-new", c.new_name));
    names.setAttribute("aria-label", "From " + c.current_name + " to " + c.new_name);
    shell.body.appendChild(names);
    shell.body.appendChild(el("div", "nc-detail",
      "Booking " + c.booking_reference + " (" + c.scope + ") · " + c.characters_changed +
      " character(s) changed, so " + (c.change_kind === "correction"
        ? "a correction of up to three characters" : "a full name change")));
    var fee = el("div", "nc-fee");
    fee.appendChild(el("span", "nc-fee-label", "Fee"));
    fee.appendChild(el("span", "nc-amount", c.fee_currency + " " + Number(c.fee_amount).toFixed(2)));
    shell.body.appendChild(fee);
    shell.body.appendChild(el("div", "nc-source", "From " + c.fee_source));
    shell.body.appendChild(el("p", "nc-question", "Shall I go ahead?"));
    var actions = el("div", "nc-actions");
    var yes = el("button", "card-btn primary", "Yes, pay and submit");
    var no = el("button", "card-btn", "Cancel");
    yes.type = no.type = "button";
    yes.addEventListener("click", function () { send("yes"); });
    no.addEventListener("click", function () { send("no"); });
    actions.appendChild(yes);
    actions.appendChild(no);
    shell.body.appendChild(actions);
    return shell.card;
  }

  function buildCard(card) {
    if (card.type === "flight") { return flightCard(card); }
    if (card.type === "booking") { return bookingCard(card); }
    if (card.type === "name_change") { return nameChangeCard(card); }
    return null;
  }

  var bubbleSeq = 0;

  function addBubble(role, text, meta) {
    var bubble = document.createElement("div");
    bubble.className = "bubble " + role;
    bubbleSeq += 1;
    var idBase = "src-" + bubbleSeq;

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

    if (role === "user") {
      appendRichText(bubble, text || "");
    } else {
      var split = splitOutSources(text || "");
      var cards = (meta && meta.cards) || [];
      renderWithCards(bubble, split.before, cards);
      addCitationMarkers(bubble, split.cited.length, idBase);
      if (split.cited.length) { bubble.appendChild(buildSources(split.cited, idBase)); }
      var others = ((meta && meta.sources) || []).filter(function (r) {
        return split.cited.indexOf(r) === -1;
      });
      if (others.length) { bubble.appendChild(buildOthers(others, split.cited.length > 0)); }
      if (split.after) { renderWithCards(bubble, split.after, cards); }
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

  function step(label, text, code, kind) {
    var el = document.createElement("div");
    el.className = "step" + (kind ? " step-" + kind : "");
    var l = document.createElement("span");
    l.className = "label";
    l.textContent = label;
    el.appendChild(l);
    // The trace is plain text: drop the answer's **bold** markers.
    if (text) { el.appendChild(document.createTextNode(String(text).replace(/\*\*/g, ""))); }
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
        var toolInput = t.input;
        if (toolInput && typeof toolInput === "object") { toolInput = JSON.stringify(toolInput); }
        traceBody.appendChild(step("Action · " + t.tool, toolInput ? String(toolInput) : "",
                                   detail.trim(), "action"));
      } else if (t.step === "final_answer") {
        traceBody.appendChild(step("Answer · " + (t.model || "model"), t.content || "", "", "final"));
      } else {
        traceBody.appendChild(step(t.step || "step", t.content || "", "", "thought"));
      }
    }

    if (data.sentiment) {
      traceBody.appendChild(step(
        "Sentiment model",
        data.sentiment.label + " (confidence " + data.sentiment.confidence +
        ", frustration " + data.sentiment.frustration_score + ")", "", "metrics"
      ));
    }

    var m = data.rag_metrics || {};
    if (m.context_relevance !== undefined) {
      var wrap = document.createElement("div");
      wrap.className = "step step-metrics";
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
    Array.prototype.forEach.call(chips.querySelectorAll("button"), function (b) {
      b.disabled = state;
    });
    chatWindow.setAttribute("aria-busy", state ? "true" : "false");
    if (!state) { input.focus(); }
  }

  function send(text) {
    var message = (text !== undefined ? text : input.value).trim();
    if (!message || busy) { return; }

    showWelcome(false);
    Array.prototype.forEach.call(document.querySelectorAll(".nc-actions button"), function (b) {
      b.disabled = true;
    });
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
        // The model that answered is shown in the reasoning panel; the header
        // only says whether the service is reachable.
        setStatus(true, "Policy assistant");
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
        traceBody.innerHTML = '<p class="trace-empty">Ask a question to see each step.</p>';
        showWelcome(true);
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

  // The welcome screen (index.html) greets the passenger; it is shown until
  // the first message and again after the conversation is deleted.
  showWelcome(true);

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
