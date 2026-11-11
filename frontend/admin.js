/* KQ-Propel administrative dashboard client.
   Separate from the chat assistant: consumes only /api/admin/* endpoints. */
(function () {
  "use strict";

  var kpis = document.getElementById("kpis");
  var triad = document.getElementById("triad");
  var sentimentDist = document.getElementById("sentiment-dist");
  var sentimentMetrics = document.getElementById("sentiment-metrics");
  var vectorstore = document.getElementById("vectorstore");
  var txBody = document.querySelector("#tx-table tbody");
  var cmpBody = document.querySelector("#cmp-table tbody");
  var cmpSummary = document.getElementById("comparison-summary");
  var cmpStatus = document.getElementById("comparison-status");
  var refreshBtn = document.getElementById("refresh");
  var runCmpBtn = document.getElementById("run-comparison");
  var auditBody = document.querySelector("#audit-table tbody");
  var loginScreen = document.getElementById("login-screen");
  var loginForm = document.getElementById("login-form");
  var loginUser = document.getElementById("login-user");
  var loginPass = document.getElementById("login-pass");
  var loginError = document.getElementById("login-error");
  var loginSubmit = document.getElementById("login-submit");
  var dashboard = document.getElementById("dashboard");
  var logoutBtn = document.getElementById("logout");
  var who = document.getElementById("who");

  /* ------------------------------- auth -------------------------------- */

  /* Every request carries the session cookie, and any 401 drops straight back
     to the sign-in screen. The server is the authority: hiding the dashboard
     in the client is presentation, not protection. */
  function api(path, options) {
    var opts = options || {};
    opts.credentials = "same-origin";
    return fetch(path, opts).then(function (r) {
      if (r.status === 401) {
        showLogin("Your session has expired. Please sign in again.");
        throw new Error("Not signed in");
      }
      return r;
    });
  }

  var passwordScreen = document.getElementById("password-screen");
  var me = null;

  function allowed(panel) { return !!(me && me.panels && me.panels.indexOf(panel) !== -1); }

  function showLogin(message) {
    me = null;
    dashboard.hidden = true;
    passwordScreen.hidden = true;
    loginScreen.hidden = false;
    loginError.textContent = message || "";
    if (loginUser) { loginUser.focus(); }
  }

  function showDashboard(info) {
    me = info;
    loginScreen.hidden = true;
    if (info.must_change_password) { showPasswordScreen(true); return; }
    passwordScreen.hidden = true;
    dashboard.hidden = false;
    if (who) {
      who.textContent = "Signed in as " + (info.display_name || info.username) +
        " (" + (info.role_label || info.role) + ")";
    }
    // Panels outside this person's role are not drawn. The server refuses
    // those routes anyway; this only keeps the page honest about it.
    Array.prototype.forEach.call(document.querySelectorAll("[data-panel]"), function (sec) {
      sec.hidden = !allowed(sec.getAttribute("data-panel"));
    });
    loadOverview();
    if (allowed("handovers") || allowed("requests")) { startHandoverPolling(); }
    if (allowed("privacy")) { loadPrivacy(); }
    if (allowed("staff")) { loadStaff(); }
  }

  function checkSession() {
    fetch("/api/admin/session", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.authenticated) { showDashboard(d); }
        else { showLogin(""); }
      })
      .catch(function () {
        showLogin("Cannot reach the server.");
      });
  }

  function submitLogin(ev) {
    ev.preventDefault();
    loginSubmit.disabled = true;
    loginError.textContent = "";
    fetch("/api/admin/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: loginUser.value.trim(),
        password: loginPass.value
      })
    })
      .then(function (r) {
        return r.json().then(function (body) { return { ok: r.ok, body: body }; });
      })
      .then(function (res) {
        if (!res.ok) {
          throw new Error((res.body && res.body.detail) || "Sign-in failed.");
        }
        loginPass.value = "";
        showDashboard(res.body);
      })
      .catch(function (err) {
        loginError.textContent = err.message;
      })
      .then(function () { loginSubmit.disabled = false; });
  }

  function doLogout() {
    logoutBtn.disabled = true;
    fetch("/api/admin/logout", { method: "POST", credentials: "same-origin" })
      .catch(function () { /* sign out locally regardless */ })
      .then(function () {
        logoutBtn.disabled = false;
        showLogin("You have been signed out.");
      });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined) { n.textContent = text; }
    return n;
  }

  function card(label, value, foot) {
    var c = el("div", "card");
    c.appendChild(el("div", "label", label));
    c.appendChild(el("div", "value", value));
    if (foot) { c.appendChild(el("div", "foot", foot)); }
    return c;
  }

  function meterRow(label, value, display) {
    var v = Math.max(0, Math.min(1, Number(value) || 0));
    var row = el("div", "meter-row");
    row.appendChild(el("span", "m-label", label));
    var track = el("span", "meter-track");
    var fill = el("span", "meter-fill");
    fill.style.width = (v * 100).toFixed(0) + "%";
    track.appendChild(fill);
    row.appendChild(track);
    row.appendChild(el("span", "m-val", display !== undefined ? display : v.toFixed(3)));
    return row;
  }

  function kvRow(key, value) {
    var row = el("div", "kv-row");
    row.appendChild(el("span", null, key));
    row.appendChild(el("span", null, String(value)));
    return row;
  }

  function emptyRow(table, cols, text) {
    var tr = el("tr");
    var td = el("td", "empty", text);
    td.colSpan = cols;
    tr.appendChild(td);
    table.appendChild(tr);
  }

  /* ------------------------------ overview ------------------------------ */

  function loadOverview() {
    refreshBtn.disabled = true;
    api("/api/admin/overview")
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(renderOverview)
      .then(function () { if (allowed("audit")) { return loadAudit(); } })
      .catch(function (err) {
        if (err.message === "Not signed in") { return; }
        kpis.innerHTML = "";
        kpis.appendChild(card("Connection", "Offline", err.message));
      })
      .then(function () { refreshBtn.disabled = false; });
  }

  function loadAudit() {
    return api("/api/admin/audit")
      .then(function (r) { return r.ok ? r.json() : { entries: [] }; })
      .then(function (d) {
        var entries = (d && d.entries) || [];
        auditBody.innerHTML = "";
        if (!entries.length) {
          emptyRow(auditBody, 5, "No administrative activity recorded yet.");
          return;
        }
        entries.forEach(function (e) {
          var tr = el("tr");
          tr.appendChild(el("td", null, e.created_at || "-"));
          tr.appendChild(el("td", null, e.username || "-"));
          tr.appendChild(el("td", null, (e.action || "-").replace(/_/g, " ")));
          tr.appendChild(el("td", null, e.detail || "-"));
          tr.appendChild(el("td", null, e.ip || "-"));
          auditBody.appendChild(tr);
        });
      })
      .catch(function () { /* audit is supplementary; never block the view */ });
  }

  // Exact mapping from ledger status to pill style and label. Matching on
  // substrings styled "unsuccessful" as a success because it contains
  // "success"; an unknown status must never look like a payment.
  var STATUS_LOOK = {
    paid: ["ok", "Paid"],
    pending: ["warn", "Pending"],
    failed: ["fail", "Failed"],
    cancelled: ["muted", "Cancelled"],
    expired: ["muted", "Expired"],
    needs_review: ["fail", "Needs review"],
    simulated: ["muted", "Simulated"],
    unknown: ["muted", "Unknown"]
  };

  function renderOverview(d) {
    var t = d.rag_triad || {};
    var tx = d.transactions || {};
    var vs = d.vector_store || {};
    var sm = d.sentiment_model_metrics || {};

    kpis.innerHTML = "";
    kpis.appendChild(card("Groundedness", (t.groundedness || 0).toFixed(3),
      "Sample size " + (t.sample_size || 0)));
    kpis.appendChild(card("Hallucination rate", (t.hallucination_rate || 0).toFixed(3),
      "1 - groundedness"));
    kpis.appendChild(card("Indexed chunks", vs.chunks !== undefined ? vs.chunks : "-",
      (vs.documents !== undefined ? vs.documents : 0) + " policy documents"));
    // Settled value counts paid transactions only. Pending money is shown
    // beneath it and never added in; failed and simulated attempts are counts.
    var settled = tx.settled_by_currency || {};
    var pendingMoney = tx.pending_by_currency || {};
    var byStatus = tx.counts_by_status || {};
    function moneyLabel(totals) {
      return Object.keys(totals).sort().map(function (c) {
        return c + " " + Number(totals[c]).toLocaleString();
      }).join("  |  ");
    }
    var totalLabel = Object.keys(settled).length ? moneyLabel(settled) : "Nothing settled";
    var parts = [(byStatus.paid || 0) + " paid"];
    if (Object.keys(pendingMoney).length) {
      parts.push(moneyLabel(pendingMoney) + " pending");
    }
    if (byStatus.failed) { parts.push(byStatus.failed + " failed"); }
    if (byStatus.simulated) { parts.push(byStatus.simulated + " simulated"); }
    kpis.appendChild(card("Settled value", totalLabel, parts.join(", ")));

    triad.innerHTML = "";
    triad.appendChild(meterRow("Context relevance", t.context_relevance));
    triad.appendChild(meterRow("Groundedness", t.groundedness));
    triad.appendChild(meterRow("Answer relevance", t.answer_relevance));
    triad.appendChild(meterRow("Hallucination rate", t.hallucination_rate));

    var dist = d.sentiment_distribution || {};
    var labels = Object.keys(dist);
    var total = 0;
    labels.forEach(function (k) { total += dist[k]; });

    sentimentDist.innerHTML = "";
    if (!labels.length) {
      sentimentDist.appendChild(el("p", "hint", "No conversations logged yet."));
    } else {
      labels.forEach(function (k) {
        sentimentDist.appendChild(meterRow(k, total ? dist[k] / total : 0,
          dist[k] + (total ? " (" + Math.round(dist[k] / total * 100) + "%)" : "")));
      });
    }

    sentimentMetrics.innerHTML = "";
    [["accuracy", "Accuracy"], ["precision", "Precision"],
     ["recall", "Recall"], ["f1_score", "F1 score"]].forEach(function (pair) {
      if (sm[pair[0]] !== undefined) {
        sentimentMetrics.appendChild(meterRow(pair[1], sm[pair[0]]));
      }
    });
    if (!sentimentMetrics.childNodes.length) {
      sentimentMetrics.appendChild(el("p", "hint", "Classifier metrics unavailable."));
    } else {
      if (sm.train_size !== undefined) {
        sentimentMetrics.appendChild(el("p", "hint",
          "In-distribution: trained on " + sm.train_size + " labelled samples, evaluated on a held-out " +
          (sm.test_size !== undefined ? sm.test_size : "?") + "-sample split of the same file."));
      }
      var pc = sm.per_class;
      if (pc && pc.calm && pc.errors) {
        sentimentMetrics.appendChild(el("p", "hint",
          "Calm passengers: precision " + pc.calm.precision.toFixed(3) + ", recall " +
          pc.calm.recall.toFixed(3) + ". Errors: " + pc.errors.calm_read_as_frustrated +
          " calm read as frustrated, " + pc.errors.frustrated_read_as_calm + " frustrated read as calm."));
      }
      // Reported alongside the figures above because a held-out split of one
      // synthetic file cannot detect an artifact in that file - an earlier
      // version of this classifier scored 0.96 while separating the classes
      // on punctuation alone.
      var ch = sm.challenge;
      if (ch && ch.accuracy !== undefined) {
        sentimentMetrics.appendChild(el("h3", "sub-head", "Challenge set (never trained on)"));
        sentimentMetrics.appendChild(meterRow("Accuracy", ch.accuracy));
        sentimentMetrics.appendChild(meterRow("F1 score", ch.f1_score));
        var gap = sm.generalization_gap;
        sentimentMetrics.appendChild(el("p", "hint",
          ch.size + " independently worded samples. Generalisation gap "
          + (gap === undefined ? "-" : (gap > 0 ? "+" : "") + gap.toFixed(3))
          + " - the drop from the held-out split is the part of the headline "
          + "score that was memorisation."));
      } else {
        sentimentMetrics.appendChild(el("p", "hint",
          "No challenge-set result: only in-distribution accuracy is available."));
      }
    }

    vectorstore.innerHTML = "";
    vectorstore.appendChild(kvRow("Backend", vs.backend || "local"));
    vectorstore.appendChild(kvRow("Documents", vs.documents !== undefined ? vs.documents : 0));
    vectorstore.appendChild(kvRow("Chunks", vs.chunks !== undefined ? vs.chunks : 0));
    if (vs.embedding_dimension !== undefined) {
      vectorstore.appendChild(kvRow("Embedding dimension", vs.embedding_dimension));
    }
    if (vs.embedding_provider) {
      vectorstore.appendChild(kvRow("Embedding provider", vs.embedding_provider));
    }
    var emb = vs.embedding || {};
    if (emb.semantic === false) {
      vectorstore.appendChild(kvRow("Retrieval mode",
        "lexical only - no semantic embedding model is active"));
    }
    if (emb.degraded_reason) {
      vectorstore.appendChild(kvRow("Embedding fallback reason", emb.degraded_reason));
    }
    if (vs.index_status) {
      vectorstore.appendChild(kvRow("Index status", vs.index_status));
    }
    // Corpus composition is disclosed here so the dashboard states how much of
    // the indexed knowledge base is published airline policy and how much is
    // prototype-authored, rather than leaving that to the documentation.
    var prov = vs.chunks_by_provenance;
    if (prov) {
      Object.keys(prov).sort().forEach(function (key) {
        var label = key.replace(/_/g, " ");
        var pct = vs.chunks ? Math.round((prov[key] / vs.chunks) * 100) : 0;
        vectorstore.appendChild(kvRow("Chunks - " + label,
          prov[key] + " (" + pct + "%)"));
      });
    }

    txBody.innerHTML = "";
    var recent = tx.recent || [];
    if (!recent.length) {
      emptyRow(txBody, 7, "No transactions recorded yet.");
    } else {
      recent.forEach(function (r) {
        var method = r.method || "mpesa";
        var currency = r.currency || "KES";
        var tr = el("tr");
        tr.appendChild(el("td", null, r.created_at || "-"));
        tr.appendChild(el("td", null, r.session_id || "-"));
        tr.appendChild(el("td", null, method === "card" ? "Card" : "M-Pesa"));
        tr.appendChild(el("td", null, r.phone_number || "-"));
        tr.appendChild(el("td", null, r.amount !== undefined && r.amount !== null
          ? currency + " " + Number(r.amount).toLocaleString() : "-"));
        var st = el("td");
        var look = STATUS_LOOK[r.status] || STATUS_LOOK.unknown;
        st.appendChild(el("span", "pill " + look[0], look[1]));
        if (r.status_detail) { st.title = r.status_detail; }
        if (r.status === "paid" && r.reference) {
          var receipt = el("a", "receipt-link", "Receipt");
          receipt.href = "/api/admin/transactions/" + encodeURIComponent(r.reference) + "/receipt";
          receipt.setAttribute("download", "");
          st.appendChild(document.createTextNode(" "));
          st.appendChild(receipt);
        }
        tr.appendChild(st);
        tr.appendChild(el("td", null, r.checkout_request_id || r.reference || "-"));
        txBody.appendChild(tr);
      });
    }
  }

  /* -------------------------- model comparison -------------------------- */

  function runComparison() {
    runCmpBtn.disabled = true;
    cmpStatus.textContent = "Running both models across the full evaluation dataset...";
    cmpBody.innerHTML = "";
    cmpSummary.innerHTML = "";

    fetch("/api/admin/model-comparison", { credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 401) {
          showLogin("Your session has expired. Please sign in again.");
          throw new Error("Not signed in");
        }
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(renderComparison)
      .catch(function (err) {
        if (err.message === "Not signed in") { return; }
        cmpStatus.textContent = "Comparison failed: " + err.message;
      })
      .then(function () { runCmpBtn.disabled = false; });
  }

  function renderComparison(data) {
    var results = data.results || [];
    var mode = data.comparison || {};

    // The panel must describe what was actually compared. Offline, both
    // columns are the same extractive composer, so presenting the run as a
    // model study would misstate the evidence.
    if (mode.title) {
      var heading = document.getElementById("comparison-title");
      if (heading) { heading.textContent = mode.title; }
      var subtitle = document.getElementById("comparison-subtitle");
      if (subtitle) { subtitle.textContent = mode.description || ""; }
    }

    var banner = document.getElementById("comparison-caveat");
    if (banner) {
      if (mode.caveat) {
        banner.textContent = mode.caveat;
        banner.style.display = "";
      } else {
        banner.textContent = "";
        banner.style.display = "none";
      }
    }

    var status = "Completed " + results.length + " queries from the evaluation dataset (" +
      (data.evaluation_dataset_size || results.length) + " total).";
    if (data.identical_response_count) {
      status += " " + data.identical_response_count + " of " + results.length +
        " queries returned byte-identical responses from both configurations.";
    }
    if (data.metric_version) {
      status += " Metric version " + data.metric_version + ".";
    }
    cmpStatus.textContent = status;

    var agg = {};
    cmpBody.innerHTML = "";

    results.forEach(function (row) {
      (row.models || []).forEach(function (m, i) {
        var tr = el("tr");
        if (row.responses_identical) { tr.className = "identical"; }
        tr.appendChild(el("td", null, i === 0 ? row.query : ""));
        tr.appendChild(el("td", null, m.model || m.name || "-"));
        tr.appendChild(el("td", null, fmt(m.context_relevance)));
        tr.appendChild(el("td", null, fmt(m.groundedness)));
        tr.appendChild(el("td", null, fmt(m.answer_relevance)));
        tr.appendChild(el("td", null, m.response_time_seconds !== undefined
          ? Number(m.response_time_seconds).toFixed(3)
          : (m.latency_seconds !== undefined ? Number(m.latency_seconds).toFixed(3) : "-")));
        cmpBody.appendChild(tr);

        var key = m.model || m.name || "model";
        if (!agg[key]) { agg[key] = { n: 0, cr: 0, g: 0, ar: 0 }; }
        agg[key].n += 1;
        agg[key].cr += Number(m.context_relevance) || 0;
        agg[key].g += Number(m.groundedness) || 0;
        agg[key].ar += Number(m.answer_relevance) || 0;
      });
    });

    if (!cmpBody.childNodes.length) {
      emptyRow(cmpBody, 6, "No comparison results returned.");
    }

    Object.keys(agg).forEach(function (k) {
      var a = agg[k];
      var mean = (a.cr / a.n + a.g / a.n + a.ar / a.n) / 3;
      cmpSummary.appendChild(meterRow(k, mean, mean.toFixed(3)));
    });
  }

  function fmt(v) {
    return v === undefined || v === null ? "-" : Number(v).toFixed(3);
  }

  /* --------------------------- handover queue --------------------------- */

  var hoBody = document.querySelector("#handover-table tbody");
  var hoHint = document.getElementById("handover-hint");
  var hoClosed = document.getElementById("handover-closed");
  var hoDetail = document.getElementById("handover-detail");
  var hoTitle = document.getElementById("handover-title");
  var hoTranscript = document.getElementById("handover-transcript");
  var hoForm = document.getElementById("handover-form");
  var hoReply = document.getElementById("handover-reply");
  var hoSend = document.getElementById("handover-send");
  var hoCloseBtn = document.getElementById("handover-close");
  var hoError = document.getElementById("handover-error");
  var hoHide = document.getElementById("handover-hide");
  var openCaseId = null;
  var hoTimer = null;

  var HANDOVER_LOOK = {
    open: ["fail", "Waiting"],
    claimed: ["warn", "In progress"],
    closed: ["muted", "Closed"]
  };
  var ROLE_NAME = { user: "Passenger", assistant: "Assistant", agent: "Staff" };

  /* ------------------------ name correction requests ------------------------ */

  var reqBody = document.querySelector("#requests-table tbody");
  var reqClosed = document.getElementById("requests-closed");
  var reqStatus = document.getElementById("requests-status");
  var REQUEST_LOOK = {
    submitted: ["warn", "Paid - to do"], awaiting_payment: ["muted", "Awaiting payment"],
    completed: ["ok", "Done"], cancelled: ["muted", "Cancelled"]
  };

  function loadRequests() {
    return api("/api/admin/requests?include_closed=" + (reqClosed.checked ? "true" : "false"))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        reqBody.innerHTML = "";
        var rows = d.requests || [];
        if (!rows.length) { emptyRow(reqBody, 7, "No name correction requests."); return; }
        rows.forEach(function (q) {
          var tr = el("tr");
          tr.appendChild(el("td", null, q.reference));
          tr.appendChild(el("td", null, q.booking_reference || "-"));
          tr.appendChild(el("td", null, (q.current_name || "-") + " → " + (q.new_name || "-")));
          tr.appendChild(el("td", null, q.fee_amount != null ? q.fee_currency + " " + Number(q.fee_amount).toFixed(2) : "-"));
          tr.appendChild(el("td", null, q.payment_reference || "-"));
          var look = REQUEST_LOOK[q.status] || ["muted", q.status];
          var st = el("td"); st.appendChild(el("span", "pill " + look[0], look[1])); tr.appendChild(st);
          var act = el("td");
          if (q.status === "submitted") {
            var btn = el("button", "btn btn-small", "Mark done");
            btn.type = "button";
            btn.addEventListener("click", function () {
              if (!window.confirm("Have you updated booking " + q.booking_reference + " to " + q.new_name + "?")) { return; }
              btn.disabled = true;
              api("/api/admin/requests/" + q.id + "/complete", {
                method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
              }).then(function (r) {
                return r.json().then(function (body) {
                  if (!r.ok) { throw new Error(body.detail || ("HTTP " + r.status)); }
                  reqStatus.textContent = q.reference + " marked done.";
                  return loadRequests();
                });
              }).catch(function (err) { reqStatus.textContent = err.message; btn.disabled = false; });
            });
            act.appendChild(btn);
          }
          tr.appendChild(act);
          reqBody.appendChild(tr);
        });
      }).catch(function () {});
  }
  reqClosed.addEventListener("change", loadRequests);

  function loadHandovers() {
    return api("/api/admin/handovers" + (hoClosed.checked ? "?include_closed=true" : ""))
      .then(function (r) { return r.ok ? r.json() : { cases: [] }; })
      .then(function (d) {
        if (!d.enabled) {
          hoHint.textContent = "Handover is switched off (HANDOVER_ENABLED=false), so the " +
            "assistant gives passengers the airline's contact details instead of opening cases.";
        }
        var cases = d.cases || [];
        hoBody.innerHTML = "";
        if (!cases.length) {
          emptyRow(hoBody, 6, "No passengers are waiting.");
          return;
        }
        cases.forEach(function (c) {
          var tr = el("tr");
          tr.appendChild(el("td", null, c.reference));
          tr.appendChild(el("td", null, (c.created_at || "-").replace("T", " ").slice(0, 16)));
          var look = HANDOVER_LOOK[c.status] || ["muted", c.status];
          var st = el("td");
          st.appendChild(el("span", "pill " + look[0], look[1]));
          tr.appendChild(st);
          tr.appendChild(el("td", null, c.frustration_score === null ? "-" :
                                      Number(c.frustration_score).toFixed(2)));
          tr.appendChild(el("td", null, c.last_passenger_message || "-"));
          var act = el("td");
          var btn = el("button", "btn btn-small", "Open");
          btn.type = "button";
          btn.addEventListener("click", function () { openCase(c.id); });
          act.appendChild(btn);
          tr.appendChild(act);
          hoBody.appendChild(tr);
        });
      })
      .catch(function () { /* the queue retries on the next tick */ });
  }

  function openCase(id) {
    openCaseId = id;
    hoError.textContent = "";
    return api("/api/admin/handovers/" + id)
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      })
      .then(function (c) {
        hoDetail.hidden = false;
        hoTitle.textContent = c.reference + " - " + (HANDOVER_LOOK[c.status] || [0, c.status])[1] +
          (c.claimed_by ? " (" + c.claimed_by + ")" : "");
        var closed = c.status === "closed";
        hoSend.disabled = closed;
        hoCloseBtn.disabled = closed;
        hoReply.disabled = closed;
        hoTranscript.innerHTML = "";
        (c.transcript || []).forEach(function (m) {
          var b = el("div", "t-msg " + m.role);
          b.appendChild(el("span", "t-who", (ROLE_NAME[m.role] || m.role) + " - " +
                                              (m.created_at || "").replace("T", " ").slice(0, 16)));
          b.appendChild(document.createTextNode(m.message));
          hoTranscript.appendChild(b);
        });
        hoTranscript.scrollTop = hoTranscript.scrollHeight;
      })
      .catch(function (err) {
        if (err.message !== "Not signed in") { hoError.textContent = "Could not open the case: " + err.message; }
      });
  }

  function postCase(action, body) {
    return api("/api/admin/handovers/" + openCaseId + "/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) { throw new Error(d.detail || ("HTTP " + r.status)); }
        return d;
      });
    });
  }

  hoForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = hoReply.value.trim();
    if (!text || openCaseId === null) { return; }
    hoSend.disabled = true;
    postCase("reply", { message: text })
      .then(function () {
        hoReply.value = "";
        return openCase(openCaseId).then(loadHandovers);
      })
      .catch(function (err) { hoError.textContent = err.message; })
      .then(function () { hoSend.disabled = false; });
  });

  hoCloseBtn.addEventListener("click", function () {
    if (openCaseId === null) { return; }
    var note = hoReply.value.trim();
    hoCloseBtn.disabled = true;
    postCase("close", { note: note || null })
      .then(function () {
        hoReply.value = "";
        return openCase(openCaseId).then(loadHandovers);
      })
      .catch(function (err) { hoError.textContent = err.message; hoCloseBtn.disabled = false; });
  });

  hoHide.addEventListener("click", function () {
    hoDetail.hidden = true;
    openCaseId = null;
  });
  hoClosed.addEventListener("change", loadHandovers);

  function startHandoverPolling() {
    if (hoTimer) { return; }
    loadHandovers();
    loadRequests();
    hoTimer = setInterval(function () {
      if (dashboard.hidden || document.hidden) { return; }
      if (!allowed("handovers")) { return; }
      loadHandovers();
      loadRequests();
      if (openCaseId !== null && !hoReply.value) { openCase(openCaseId); }
    }, 15000);
  }

  /* --------------------------- data protection --------------------------- */

  var eraseForm = document.getElementById("erase-form");
  var eraseCase = document.getElementById("erase-case");
  var erasePhone = document.getElementById("erase-phone");
  var eraseConfirm = document.getElementById("erase-confirm");
  var eraseStatus = document.getElementById("erase-status");
  var retentionLine = document.getElementById("privacy-retention");

  function loadPrivacy() {
    api("/api/admin/privacy").then(function (r) { return r.json(); }).then(function (d) {
      var p = d.retention || {};
      var last = d.last_purge && d.last_purge.at ? " Last automatic purge: " + d.last_purge.at + " UTC." : "";
      if (!p.enabled) {
        retentionLine.textContent = "Automatic deletion is switched off (PRIVACY_RETENTION_ENABLED). " +
          "Personal data is kept until it is erased on request. When switched on, conversations are " +
          "erased " + p.conversation_days + " days after the last message.";
        return;
      }
      retentionLine.textContent = "Conversations are erased " + p.conversation_days +
        " days after the last message; payment records lose their phone number after " +
        p.payment_contact_days + " days; this access log is kept for " + p.audit_days + " days." + last;
    }).catch(function () {});
  }

  function eraseRequest(confirm) {
    return api("/api/admin/privacy/erase", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_reference: eraseCase.value.trim() || null,
                             phone: erasePhone.value.trim() || null, confirm: confirm })
    }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) { throw new Error(typeof d.detail === "string" ? d.detail : "Check the details entered."); }
        return d;
      });
    });
  }

  function resetErase() { eraseConfirm.disabled = true; }
  eraseCase.addEventListener("input", resetErase);
  erasePhone.addEventListener("input", resetErase);

  eraseForm.addEventListener("submit", function (e) {
    e.preventDefault();
    eraseRequest(false).then(function (d) {
      eraseStatus.textContent = d.sessions_found
        ? d.sessions_found + " conversation(s) found. Press Erase to delete them."
        : "No conversations found for those details.";
      eraseConfirm.disabled = !d.sessions_found;
    }).catch(function (err) { eraseStatus.textContent = err.message; resetErase(); });
  });

  eraseConfirm.addEventListener("click", function () {
    if (!window.confirm("Erase this passenger's conversations? This cannot be undone.")) { return; }
    eraseRequest(true).then(function (d) {
      var c = d.erased || {};
      eraseStatus.textContent = "Erased " + (c.messages || 0) + " message(s) in " + (c.sessions || 0) +
        " conversation(s); " + (c.payment_contacts || 0) + " payment record(s) no longer hold a phone number.";
      eraseCase.value = ""; erasePhone.value = ""; resetErase();
      loadHandovers();
    }).catch(function (err) { eraseStatus.textContent = err.message; });
  });

  /* ------------------------------ passwords ------------------------------ */

  var pwForm = document.getElementById("password-form");
  var pwCurrent = document.getElementById("pw-current");
  var pwNew = document.getElementById("pw-new");
  var pwConfirm = document.getElementById("pw-confirm");
  var pwError = document.getElementById("pw-error");
  var pwCancel = document.getElementById("pw-cancel");
  var pwTitle = document.getElementById("password-title");
  var pwSub = document.getElementById("password-sub");

  function showPasswordScreen(required) {
    dashboard.hidden = true;
    passwordScreen.hidden = false;
    pwCancel.hidden = required;
    pwTitle.textContent = required ? "Choose your password" : "Change your password";
    pwSub.textContent = required
      ? "You signed in with a one-time password. Choose your own to continue (at least 10 characters)."
      : "Enter your current password and the new one (at least 10 characters).";
    pwError.textContent = "";
    pwCurrent.value = pwNew.value = pwConfirm.value = "";
    pwCurrent.focus();
  }

  pwForm.addEventListener("submit", function (e) {
    e.preventDefault();
    if (pwNew.value !== pwConfirm.value) {
      pwError.textContent = "The new passwords do not match.";
      pwConfirm.focus();
      return;
    }
    api("/api/admin/password", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_password: pwCurrent.value, new_password: pwNew.value })
    }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) { throw new Error(typeof d.detail === "string" ? d.detail : "Check the passwords entered."); }
        pwCurrent.value = pwNew.value = pwConfirm.value = "";
        showDashboard(d);
      });
    }).catch(function (err) { pwError.textContent = err.message; });
  });
  pwCancel.addEventListener("click", function () { showDashboard(me); });
  document.getElementById("change-password").addEventListener("click", function () {
    showPasswordScreen(false);
  });

  /* ---------------------------- staff accounts ---------------------------- */

  var staffForm = document.getElementById("staff-form");
  var staffBody = document.querySelector("#staff-table tbody");
  var staffStatus = document.getElementById("staff-status");
  var staffOtp = document.getElementById("staff-otp");
  var ROLE_NAMES = { admin: "Administrator", agent: "Customer service agent", analyst: "Analyst" };

  function staffCall(path, body) {
    return api(path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) { throw new Error(typeof d.detail === "string" ? d.detail : "Check the details entered."); }
        return d;
      });
    });
  }

  function showOneTimePassword(account) {
    staffOtp.hidden = false;
    staffOtp.innerHTML = "";
    staffOtp.appendChild(el("p", null, "One-time password for " + account.username +
      " (shown only now - give it to them privately):"));
    var code = el("code", "otp", account.one_time_password);
    staffOtp.appendChild(code);
    staffOtp.appendChild(el("p", "hint", "They will be asked to choose their own password when they sign in."));
  }

  function loadStaff() {
    return api("/api/admin/staff").then(function (r) { return r.json(); }).then(function (d) {
      staffBody.innerHTML = "";
      (d.staff || []).forEach(function (s) {
        var tr = el("tr");
        tr.appendChild(el("td", null, s.username));
        tr.appendChild(el("td", null, s.display_name || "-"));

        var roleCell = el("td");
        var select = el("select");
        select.setAttribute("aria-label", "Role for " + s.username);
        Object.keys(ROLE_NAMES).forEach(function (r) {
          var o = el("option", null, ROLE_NAMES[r]); o.value = r; if (r === s.role) { o.selected = true; }
          select.appendChild(o);
        });
        select.addEventListener("change", function () {
          var role = select.value;
          if (!window.confirm("Make " + s.username + " a " + ROLE_NAMES[role] + "?")) { select.value = s.role; return; }
          staffCall("/api/admin/staff/" + encodeURIComponent(s.username) + "/role", { role: role })
            .then(function () { staffStatus.textContent = s.username + " is now a " + ROLE_NAMES[role] + "."; loadStaff(); })
            .catch(function (err) { staffStatus.textContent = err.message; select.value = s.role; });
        });
        roleCell.appendChild(select);
        tr.appendChild(roleCell);

        var st = el("td");
        st.appendChild(el("span", "pill " + (s.active ? (s.must_change_password ? "warn" : "ok") : "muted"),
          s.active ? (s.must_change_password ? "Awaiting first sign-in" : "Active") : "Disabled"));
        tr.appendChild(st);
        tr.appendChild(el("td", null, s.last_login_at ? s.last_login_at.replace("T", " ") : "Never"));

        var act = el("td", "row-actions");
        var self = me && s.username.toLowerCase() === me.username.toLowerCase();
        var reset = el("button", "btn btn-small btn-quiet", "Reset password");
        reset.type = "button";
        reset.setAttribute("aria-label", "Reset password for " + s.username);
        reset.disabled = s.created_by === "environment";
        if (reset.disabled) { reset.title = "Set in the server's environment"; }
        reset.addEventListener("click", function () {
          if (!window.confirm("Issue a new one-time password for " + s.username + "? They will be signed out.")) { return; }
          staffCall("/api/admin/staff/" + encodeURIComponent(s.username) + "/reset-password")
            .then(function (acc) { showOneTimePassword(acc); loadStaff(); })
            .catch(function (err) { staffStatus.textContent = err.message; });
        });
        act.appendChild(reset);
        if (!self) {
          var toggle = el("button", "btn btn-small btn-quiet", s.active ? "Disable" : "Enable");
          toggle.type = "button";
          toggle.setAttribute("aria-label", (s.active ? "Disable " : "Enable ") + s.username);
          toggle.addEventListener("click", function () {
            if (s.active && !window.confirm("Disable " + s.username + "? They are signed out at once.")) { return; }
            staffCall("/api/admin/staff/" + encodeURIComponent(s.username) + (s.active ? "/disable" : "/enable"))
              .then(function () { staffStatus.textContent = s.username + (s.active ? " disabled." : " enabled."); loadStaff(); })
              .catch(function (err) { staffStatus.textContent = err.message; });
          });
          act.appendChild(toggle);
        }
        tr.appendChild(act);
        staffBody.appendChild(tr);
      });
    }).catch(function () {});
  }

  staffForm.addEventListener("submit", function (e) {
    e.preventDefault();
    staffCall("/api/admin/staff", {
      username: document.getElementById("staff-username").value.trim(),
      display_name: document.getElementById("staff-name").value.trim() || null,
      role: document.getElementById("staff-role").value
    }).then(function (acc) {
      staffForm.reset();
      staffStatus.textContent = "Account " + acc.username + " created.";
      showOneTimePassword(acc);
      loadStaff();
    }).catch(function (err) { staffStatus.textContent = err.message; });
  });

  refreshBtn.addEventListener("click", loadOverview);
  runCmpBtn.addEventListener("click", runComparison);
  loginForm.addEventListener("submit", submitLogin);
  logoutBtn.addEventListener("click", doLogout);

  checkSession();
})();
