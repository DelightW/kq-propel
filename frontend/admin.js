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

  function showLogin(message) {
    dashboard.hidden = true;
    loginScreen.hidden = false;
    loginError.textContent = message || "";
    if (loginUser) { loginUser.focus(); }
  }

  function showDashboard(username) {
    loginScreen.hidden = true;
    dashboard.hidden = false;
    if (who && username) { who.textContent = "Signed in as " + username; }
    loadOverview();
  }

  function checkSession() {
    fetch("/api/admin/session", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.authenticated) { showDashboard(d.username); }
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
        showDashboard(res.body.username);
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
      .then(loadAudit)
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
    var totals = tx.totals_by_currency || {};
    var rails = tx.counts_by_method || {};
    var totalLabel = Object.keys(totals).length
      ? Object.keys(totals).sort().map(function (c) {
          return c + " " + Number(totals[c]).toLocaleString();
        }).join("  |  ")
      : "KES 0";
    var railLabel = (tx.count || 0) + " transactions"
      + (Object.keys(rails).length
          ? " (" + Object.keys(rails).sort().map(function (m) {
              return rails[m] + " " + (m === "card" ? "card" : "M-Pesa");
            }).join(", ") + ")"
          : "");
    kpis.appendChild(card("Settled value", totalLabel, railLabel));

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
      // Reported alongside the figures above because a held-out split of one
      // synthetic file cannot detect an artifact in that file - an earlier
      // version of this classifier scored 0.96 while separating the classes
      // on punctuation alone.
      var ch = sm.challenge;
      if (ch && ch.accuracy !== undefined) {
        sentimentMetrics.appendChild(el("h4", "sub-head", "Challenge set (never trained on)"));
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
        var ok = String(r.status || "").toLowerCase().indexOf("success") !== -1 ||
                 String(r.status || "").toLowerCase().indexOf("accept") !== -1;
        st.appendChild(el("span", "pill " + (ok ? "ok" : ""), r.status || "unknown"));
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

  refreshBtn.addEventListener("click", loadOverview);
  runCmpBtn.addEventListener("click", runComparison);
  loginForm.addEventListener("submit", submitLogin);
  logoutBtn.addEventListener("click", doLogout);

  checkSession();
})();
