/* Hindsight front end. No framework, no build step.
   Everything that reaches the DOM from the server (model text, log lines,
   uploaded bundles) goes through esc() first. */
(() => {
  "use strict";

  const API = "/api/copilot";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => `$${Number(n || 0).toFixed(4)}`;
  const shortModel = (m) => String(m || "").replace(/^nvidia\//i, "");

  let status = { ready: false, research: false, models: {} };
  let stream = null;
  let run = null; // state of the investigation on screen

  async function api(path, options) {
    const resp = await fetch(API + path, options);
    if (!resp.ok) {
      let detail = `Request failed (${resp.status}).`;
      try { detail = (await resp.json()).detail || detail; } catch { /* keep default */ }
      throw new Error(detail);
    }
    return resp.json();
  }

  const tagHtml = (ref) =>
    `<button type="button" class="tag${ref[0] === "R" ? " ref" : ""}" data-ref="${esc(ref)}">${esc(ref)}</button>`;
  const withTags = (html) => html.replace(/\[([ER]\d+)\]/g, (_, ref) => tagHtml(ref));

  // ---------- home ----------

  function setNotice(el, message, bad) {
    el.hidden = !message;
    el.textContent = message || "";
    el.classList.toggle("bad", Boolean(bad));
  }

  async function showHome() {
    closeStream();
    $("desk").hidden = true;
    $("home").hidden = false;
    document.title = "Hindsight";
    try {
      status = await api("/status");
      $("bar-note").textContent = status.ready
        ? "Running on NVIDIA Nemotron via Nebius Token Factory"
        : "";
      setNotice($("notice"), status.ready ? "" :
        "This server has no Nebius key, so new investigations cannot start. Recorded investigations below still open.");
      const [incidents, runs, results] = await Promise.all(
        [api("/incidents"), api("/investigations"), api("/benchmarks")]);
      renderCases(incidents);
      renderRuns(runs);
      renderBoard(results);
    } catch (err) {
      setNotice($("notice"), `Could not load incidents: ${err.message}`, true);
    }
  }

  function renderCases(incidents) {
    $("cases").innerHTML = incidents.map((inc, i) => `
      <li><article class="case-card">
        <div class="case-meta"><span>${esc(inc.difficulty)}</span><span>${esc(inc.services)} services</span>
          <span>${inc.source === "seed" ? `generated, seed ${esc(inc.seed)}` : "hand-written"}</span></div>
        <h3>${esc(inc.title)}</h3>
        <p>${esc(inc.description)}</p>
        <button type="button" data-case="${i}" ${status.ready ? "" : "disabled"}>Investigate</button>
      </article></li>`).join("");
    $("cases").onclick = (event) => {
      const button = event.target.closest("button[data-case]");
      if (!button) return;
      const inc = incidents[Number(button.dataset.case)];
      start(inc.source === "task"
        ? { source: "task", task_id: inc.task_id }
        : { source: "seed", seed: inc.seed, difficulty: inc.difficulty }, button);
    };
  }

  function renderRuns(runs) {
    $("history-empty").hidden = runs.length > 0;
    $("runs").innerHTML = runs.map((r) => {
      let pill = `<span class="pill">${esc(r.status)}</span>`;
      if (r.cause_correct === true) pill = '<span class="pill good">root cause correct</span>';
      else if (r.cause_correct === false) pill = '<span class="pill bad">root cause wrong</span>';
      else if (r.status === "error") pill = '<span class="pill bad">stopped</span>';
      else if (r.status === "done") pill = '<span class="pill">not graded</span>';
      return `<li>
        <a href="#/i/${esc(r.id)}">${esc(r.title)}</a>
        <span class="cell opt">${r.recorded ? "recorded" : new Date(r.started_at * 1000).toLocaleString()}</span>
        <span class="cell opt">${r.cost_usd == null ? "" : money(r.cost_usd)}</span>
        ${pill}</li>`;
    }).join("");
  }

  const BASELINE_NAMES = {
    random: "Guess at random",
    nearest: "Blame the latest change",
    reddest: "Blame the latest change on the worst-hit service",
  };
  const LEVELS = ["easy", "medium", "hard"];

  function meanAccuracy(row) {
    const values = Object.values(row.cells).map((s) => s.root_cause_accuracy || 0);
    return values.reduce((a, b) => a + b, 0) / (values.length || 1);
  }

  function renderBoard(results) {
    $("measured").hidden = results.length === 0;
    if (!results.length) return;
    const rows = new Map();
    for (const r of results) {
      const row = rows.get(r.label) || { label: r.label, kind: r.kind, models: r.models, cells: {} };
      row.cells[r.difficulty] = r.summary;
      rows.set(r.label, row);
    }
    const ordered = [...rows.values()].sort((a, b) =>
      (a.kind === "model") - (b.kind === "model") || meanAccuracy(a) - meanAccuracy(b));
    const cell = (s) => {
      if (!s || s.root_cause_accuracy == null) return '<td class="num">–</td>';
      const pct = Math.round(s.root_cause_accuracy * 100);
      return `<td class="num">${pct}%<i><b style="width:${pct}%"></b></i></td>`;
    };
    const body = ordered.map((row) => {
      const models = [...new Set(Object.values(row.models || {}).map(shortModel))];
      const costs = LEVELS.map((l) => (row.cells[l] || {}).mean_cost_usd).filter((n) => n != null);
      const cost = row.kind === "model" && costs.length
        ? money(costs.reduce((a, b) => a + b, 0) / costs.length) : "none";
      return `<tr class="${esc(row.kind)}"><td>${esc(BASELINE_NAMES[row.label] || row.label)}
        ${models.length ? `<small>${esc(models.join(" + "))}</small>` : ""}</td>
        ${LEVELS.map((l) => cell(row.cells[l])).join("")}<td class="cost">${cost}</td></tr>`;
    }).join("");
    $("board").innerHTML = `<thead><tr><th>Approach</th>${
      LEVELS.map((l) => `<th>${l}</th>`).join("")}<th>Cost per incident</th></tr></thead><tbody>${body}</tbody>`;
    const counts = results.map((r) => r.summary.incidents);
    const hasModel = ordered.some((row) => row.kind === "model");
    const span = Math.min(...counts) === Math.max(...counts)
      ? `${counts[0]}` : `${Math.min(...counts)} to ${Math.max(...counts)}`;
    $("board-note").textContent = `${span} incidents per cell.`
      + (hasModel ? "" : " Results for the Nemotron investigator have not been recorded yet.");
  }

  async function start(body, button) {
    if (button) button.disabled = true;
    try {
      const { id } = await api("/investigations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      location.hash = `#/i/${id}`;
    } catch (err) {
      setNotice($("notice"), err.message, true);
      $("notice").scrollIntoView({ block: "center" });
      if (button) button.disabled = !status.ready;
    }
  }

  $("bundle-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    let bundle;
    try {
      bundle = JSON.parse(await file.text());
    } catch {
      setNotice($("notice"), `${file.name} is not valid JSON.`, true);
      return;
    }
    start({ source: "bundle", bundle });
  });

  // ---------- investigation desk ----------

  function closeStream() {
    if (stream) { stream.close(); stream = null; }
  }

  async function showDesk(id) {
    closeStream();
    $("home").hidden = true;
    $("desk").hidden = false;
    run = { id, brief: null, entities: {}, lookups: 0, finished: false, report: "", positions: {} };

    $("desk-title").textContent = "Investigation";
    $("desk-id").textContent = "";
    $("brief").textContent = "";
    $("window").textContent = "";
    $("graph").innerHTML = "";
    $("changes").innerHTML = "";
    $("exhibits").innerHTML = "";
    $("verdict").className = "pending";
    $("verdict").textContent = "The diagnosis appears here once the evidence is in.";
    $("grade").innerHTML = "";
    $("refs").innerHTML = "";
    $("doc-wrap").hidden = true;
    $("meter-lookups").textContent = "0";
    $("meter-calls").textContent = "0";
    $("meter-cost").textContent = money(0);
    setNotice($("desk-notice"), "");
    document.querySelectorAll("#rail li").forEach((li) => { li.className = ""; });
    setWorking("Opening the incident");

    try {
      if (!status.models.triage) status = await api("/status");
      for (const role of ["triage", "reason", "writer"]) {
        $(`m-${role}`).textContent = shortModel(status.models[role]);
      }
      const record = await api(`/investigations/${id}`);
      $("desk-title").textContent = record.title;
      document.title = `${record.title} · Hindsight`;
    } catch (err) {
      setWorking("");
      setNotice($("desk-notice"), err.message, true);
      return;
    }

    stream = new EventSource(`${API}/investigations/${id}/events`);
    const handlers = { brief: onBrief, phase: onPhase, thought: onThought, evidence: onEvidence,
      usage: onUsage, diagnosis: onDiagnosis, grade: onGrade, research: onResearch,
      report: onReport, warning: onWarning, error: onError, done: onDone, _eof: onEof };
    for (const [type, handler] of Object.entries(handlers)) {
      stream.addEventListener(type, (event) => {
        // "error" also fires for connection problems, which carry no data.
        if (event.data) handler(JSON.parse(event.data));
        else if (type === "error" && !run.finished) onDisconnect();
      });
    }
  }

  function setWorking(text) {
    $("working").hidden = !text;
    $("working-text").textContent = text;
  }

  function onDisconnect() {
    closeStream();
    setWorking("");
    setNotice($("desk-notice"), "Lost the connection to the server. Reload the page to replay this investigation.", true);
  }

  function onBrief({ brief, graded }) {
    run.brief = brief;
    $("desk-id").textContent = `${brief.incident_id} · ${graded ? "graded against a known answer" : "not graded"}`;
    $("brief").textContent = brief.description;
    const w = brief.incident_window || {};
    $("window").textContent = `${w.start || "?"}  to  ${w.end || "?"}`;

    const changes = [];
    for (const c of brief.commits) {
      changes.push({ id: c.hash, when: c.timestamp, what: c.message, where: c.service });
    }
    for (const c of brief.config_changes) {
      changes.push({ id: c.config_id, when: c.timestamp, what: c.description, where: c.service });
    }
    for (const e of brief.infra_events) {
      changes.push({ id: e.event_id, when: e.timestamp, what: e.description, where: "infrastructure" });
    }
    changes.sort((a, b) => String(a.when).localeCompare(String(b.when)));
    for (const c of changes) run.entities[c.id] = c;
    $("changes").innerHTML = changes.map((c) => `
      <li data-change="${esc(c.id)}"><code>${esc(c.id)}</code><span>${esc(c.what)}</span>
      <span class="when">${esc(c.when)} · ${esc(c.where)}</span></li>`).join("");

    const onset = brief.error_onset || [];
    $("onset-wrap").hidden = onset.length === 0;
    $("onset").innerHTML = onset.map((row) => `<li><strong>${esc(row.service)}</strong>
      <span>${esc(String(row.first_error).slice(11, 19))} UTC · ${esc(row.errors)} errors</span></li>`).join("");

    drawGraph(brief);
  }

  const WORKING = {
    triage: "Triage: choosing the next lookup",
    diagnosis: "Diagnosis: weighing the evidence",
    research: "Research: searching for published guidance",
    report: "Writing the postmortem",
  };

  function onPhase({ phase }) {
    const items = [...document.querySelectorAll("#rail li")];
    const index = items.findIndex((li) => li.dataset.phase === phase);
    items.forEach((li, i) => {
      if (i < index) {
        if (!li.classList.contains("done") && !li.classList.contains("active")) li.classList.add("skipped");
        li.classList.remove("active");
        if (!li.classList.contains("skipped")) li.classList.add("done");
      }
      li.classList.toggle("active", i === index);
    });
    setWorking(WORKING[phase] || "");
  }

  function addToLedger(html) {
    $("exhibits").insertAdjacentHTML("beforeend", html);
  }

  function onThought({ role, model, text, tool }) {
    if (!text) return;
    const note = tool === "done" ? " Enough evidence; moving to diagnosis." : "";
    addToLedger(`<li class="thought"><b class="m-${esc(role)}">${esc(shortModel(model))}</b>${esc(text)}${note}</li>`);
  }

  function colourLines(result, tool) {
    return esc(result).split("\n").map((line) => {
      if (/\b(ERROR|CRITICAL|FATAL)\b/.test(line)) return `<span class="err">${line}</span>`;
      if (tool === "get_commit" && /^\+(?!\+\+)/.test(line)) return `<span class="add">${line}</span>`;
      if (tool === "get_commit" && /^-(?!--)/.test(line)) return `<span class="del">${line}</span>`;
      return line;
    }).join("\n");
  }

  function onEvidence(ev) {
    const args = Object.entries(ev.args || {}).map(([k, v]) => `${k}=${v}`).join(", ");
    const call = `${ev.tool}(${args})`;
    if (!ev.ok) {
      addToLedger(`<li class="rejected">${esc(call)}: ${esc(ev.result)}</li>`);
      return;
    }
    run.lookups += 1;
    $("meter-lookups").textContent = run.lookups;
    const long = ev.result.split("\n").length > 6 || ev.result.length > 420;
    addToLedger(`<li class="exhibit" id="ex-${esc(ev.id)}">
      <header>${tagHtml(ev.id)}<code>${esc(call)}</code></header>
      <pre>${colourLines(ev.result, ev.tool)}</pre>
      ${long ? '<button type="button" class="more" aria-expanded="false">Show all</button>' : ""}</li>`);
  }

  function onUsage({ by_role: byRole, total_cost_usd: total }) {
    let calls = 0;
    for (const [role, row] of Object.entries(byRole || {})) {
      calls += row.calls;
      const label = $(`m-${role}`);
      if (label) label.textContent = shortModel(row.model);
    }
    $("meter-calls").textContent = calls;
    $("meter-cost").textContent = money(total);
  }

  function onDiagnosis(d) {
    const verdict = $("verdict");
    verdict.className = "";
    if (!d.root_cause_ids.length) {
      verdict.innerHTML = `<p class="cause-id">No cause named</p>
        <p class="summary">The evidence gathered was not enough to name what started this outage.</p>
        ${listBlock("Open questions", d.open_questions.map(esc))}`;
      return;
    }
    const causes = d.root_cause_ids.map((id) => {
      const e = run.entities[id] || {};
      const row = document.querySelector(`[data-change="${CSS.escape(id)}"]`);
      if (row) {
        row.classList.add("culprit");
        row.parentElement.scrollTop = row.offsetTop - row.parentElement.offsetTop - 60;
      }
      return `<p class="cause-id">${esc(id)}</p><p class="cause-what">${esc(e.what || "")}${e.where ? ` · ${esc(e.where)}` : ""}</p>`;
    }).join("");
    const pct = Math.round(d.confidence * 100);
    const hops = d.chain.map((hop) => `<li><strong>${esc(hop.service)}</strong>
      <span class="effect">${esc(hop.effect.replace(/_/g, " "))}</span><br>
      ${esc(hop.because)} ${hop.evidence.map(tagHtml).join(" ")}</li>`).join("");
    verdict.innerHTML = `
      <h3>Root cause</h3>${causes}
      <div class="confidence"><i><b style="width:${pct}%"></b></i><span>confidence ${pct}%</span></div>
      <p class="summary">${esc(d.summary)}</p>
      ${hops ? `<h3>How it spread</h3><ol class="hops">${hops}</ol>` : ""}
      ${listBlock("Ruled out", d.ruled_out.map((r) => `<code>${esc(r.id)}</code> ${esc(r.why)}`))}
      ${listBlock("Open questions", d.open_questions.map(esc))}`;
    drawSpread(d.chain);
  }

  function listBlock(title, items) {
    if (!items.length) return "";
    return `<h3>${title}</h3><ul class="plain">${items.map((i) => `<li>${i}</li>`).join("")}</ul>`;
  }

  function onGrade(g) {
    const rows = (g.rubrics || []).map((r) =>
      `<tr><td>${esc(r.rubric.replace(/_/g, " "))}</td><td>${Number(r.raw_score).toFixed(2)} × ${Number(r.weight).toFixed(2)}</td></tr>`).join("");
    $("grade").innerHTML = `<div class="grade${g.cause_correct ? "" : " wrong"}">
      <h3 style="margin-top:0">Graded against the known answer</h3>
      <p class="score">${Number(g.score).toFixed(3)}</p>
      <p class="verdict">Root cause ${g.cause_correct ? "correct" : "wrong"}</p>
      ${g.cause_correct ? "" : `<p class="truth">Actual cause: <code>${esc(g.ground_truth_cause)}</code></p>`}
      <table>${rows}</table>
      <p class="truth">Scored by fixed rules, not by a model. The investigator never saw the answer. Grounding checks that it opened the change it blames and cited evidence that bears on the incident.</p>
      ${run.brief && !/^seed_/.test(run.brief.incident_id) ? '<p class="truth">This hand-written incident labels its chain in free text, so the failure modes score understates a correct answer. Read root cause and failure path.</p>' : ""}</div>`;
  }

  function onResearch({ queries, references, queries_dropped_as_private: dropped }) {
    const items = references.map((r) => `<li id="ref-${esc(r.id)}">${tagHtml(r.id)}
      <a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a></li>`).join("");
    $("refs").innerHTML = `<h3>Published guidance</h3>
      <p class="q">Searched: ${queries.map(esc).join(" · ") || "nothing"}${dropped ? ` · ${dropped} query withheld for naming internal systems` : ""}</p>
      <ul class="reflist">${items}</ul>`;
  }

  function onReport({ markdown }) {
    run.report = markdown;
    $("doc").innerHTML = renderMarkdown(markdown);
    $("download-md").href = `${API}/investigations/${run.id}/postmortem.md`;
    $("doc-wrap").hidden = false;
  }

  function onWarning({ message }) { setNotice($("desk-notice"), message); }

  function onError({ message }) {
    run.finished = true;
    setWorking("");
    setNotice($("desk-notice"), `The investigation stopped: ${message}`, true);
  }

  function onDone({ usage }) {
    if (usage) onUsage(usage);
    run.finished = true;
    setWorking("");
    document.querySelectorAll("#rail li").forEach((li) => {
      if (li.classList.contains("active")) { li.classList.remove("active"); li.classList.add("done"); }
    });
  }

  function onEof() {
    run.finished = true;
    setWorking("");
    closeStream();
  }

  // ---------- service map ----------

  const NODE_W = 148, NODE_H = 46, COL_GAP = 16, ROW_GAP = 30, PAD = 14, MAP_W = 360;

  function drawGraph(brief) {
    const graph = brief.service_graph || {};
    const info = Object.fromEntries((brief.services || []).map((s) => [s.name, s]));
    const names = new Set([...Object.keys(graph), ...Object.keys(info)]);
    Object.values(graph).forEach((deps) => deps.forEach((d) => names.add(d)));
    const dependants = {};
    for (const [svc, deps] of Object.entries(graph)) {
      for (const dep of deps) (dependants[dep] ||= []).push(svc);
    }
    // Row = longest chain of dependants above a service; user-facing services sit on top.
    const depth = {};
    const visit = (name, trail) => {
      if (depth[name] != null) return depth[name];
      if (trail.has(name)) return 0;
      trail.add(name);
      const above = dependants[name] || [];
      const d = above.length ? 1 + Math.max(...above.map((p) => visit(p, trail))) : 0;
      trail.delete(name);
      return (depth[name] = d);
    };
    names.forEach((n) => visit(n, new Set()));

    const rows = [];
    [...names].sort().forEach((n) => (rows[depth[n]] ||= []).push(n));
    const widest = Math.max(...rows.map((r) => (r ? r.length : 0)), 1);
    const width = Math.max(MAP_W, PAD * 2 + widest * NODE_W + (widest - 1) * COL_GAP);
    const height = PAD * 2 + rows.length * NODE_H + (rows.length - 1) * ROW_GAP;

    run.positions = {};
    rows.forEach((row, ri) => {
      const rowWidth = row.length * NODE_W + (row.length - 1) * COL_GAP;
      row.forEach((name, ci) => {
        run.positions[name] = {
          x: (width - rowWidth) / 2 + ci * (NODE_W + COL_GAP),
          y: PAD + ri * (NODE_H + ROW_GAP),
        };
      });
    });

    let html = `<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
      <path d="M0 0 L8 4 L0 8 z" fill="#9aa4be"/></marker>
      <marker id="spread-arrow" viewBox="0 0 8 8" refX="6" refY="4" markerWidth="5" markerHeight="5" orient="auto">
      <path d="M0 0 L8 4 L0 8 z" fill="#d4412a"/></marker></defs>`;
    for (const [svc, deps] of Object.entries(graph)) {
      for (const dep of deps) {
        const a = run.positions[svc], b = run.positions[dep];
        if (!a || !b) continue;
        const x1 = a.x + NODE_W / 2, y1 = a.y + NODE_H, x2 = b.x + NODE_W / 2, y2 = b.y;
        const mid = (y1 + y2) / 2;
        html += `<path class="edge" marker-end="url(#arrow)" d="M${x1} ${y1} C${x1} ${mid} ${x2} ${mid} ${x2} ${y2 - 2}"/>`;
      }
    }
    for (const [name, p] of Object.entries(run.positions)) {
      const s = info[name] || {};
      const rate = typeof s.error_rate_during_incident === "number"
        ? `${s.error_rate_during_incident.toFixed(1)}% errors` : "";
      html += `<g class="node ${esc(s.status || "")}" transform="translate(${p.x} ${p.y})">
        <title>${esc(name)}: ${esc(s.status || "status unknown")}</title>
        <rect width="${NODE_W}" height="${NODE_H}" rx="3"/>
        <text x="10" y="19">${esc(name.length > 19 ? `${name.slice(0, 18)}…` : name)}</text>
        <text class="rate" x="10" y="35">${esc([s.status, rate].filter(Boolean).join(" · "))}</text></g>`;
    }
    const svg = $("graph");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    // Wide topologies scroll sideways instead of shrinking until unreadable.
    svg.style.minWidth = width > MAP_W ? `${Math.round(width * 0.8)}px` : "";
    svg.innerHTML = html + '<g id="spread"></g>';
  }

  function drawSpread(chain) {
    const layer = document.getElementById("spread");
    if (!layer) return;
    let html = "";
    const pins = {};
    chain.forEach((hop, i) => {
      (pins[hop.service] ||= []).push(i + 1);
      const next = chain[i + 1];
      if (!next || next.service === hop.service) return;
      const a = run.positions[hop.service], b = run.positions[next.service];
      if (!a || !b) return;
      const up = b.y < a.y;
      html += `<line class="spread" marker-end="url(#spread-arrow)" x1="${a.x + NODE_W / 2 + 26}" y1="${up ? a.y : a.y + NODE_H}" x2="${b.x + NODE_W / 2 + 26}" y2="${up ? b.y + NODE_H + 3 : b.y - 3}"/>`;
    });
    for (const [service, numbers] of Object.entries(pins)) {
      const p = run.positions[service];
      if (!p) continue;
      const label = numbers.length > 1 ? `${numbers[0]}-${numbers[numbers.length - 1]}` : `${numbers[0]}`;
      const w = label.length > 1 ? 34 : 22;
      html += `<g class="pin" transform="translate(${p.x + NODE_W - w / 2 - 4} ${p.y})">
        <rect x="${-w / 2}" y="-11" width="${w}" height="22" rx="11" fill="#d4412a"/>
        <text y="5">${label}</text></g>`;
    }
    layer.innerHTML = html;
  }

  // ---------- markdown (the subset the postmortem writer emits) ----------

  function inline(text) {
    let html = esc(text);
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    html = html.replace(/(^|\s)_\(([^)]+)\)_/g, "$1<em>($2)</em>");
    return withTags(html);
  }

  function renderMarkdown(markdown) {
    const lines = markdown.split("\n");
    const out = [];
    let i = 0;
    const cells = (row) => row.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    while (i < lines.length) {
      const line = lines[i];
      if (line.startsWith("```")) {
        const code = [];
        for (i += 1; i < lines.length && !lines[i].startsWith("```"); i += 1) code.push(lines[i]);
        out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
        i += 1;
      } else if (/^#{1,3} /.test(line)) {
        const level = line.indexOf(" ");
        out.push(`<h${level}>${inline(line.slice(level + 1))}</h${level}>`);
        i += 1;
      } else if (line.startsWith("|") && /^\|[\s:|-]+\|$/.test(lines[i + 1] || "")) {
        const head = cells(line);
        const rows = [];
        for (i += 2; i < lines.length && lines[i].startsWith("|"); i += 1) rows.push(cells(lines[i]));
        out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
          rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      } else if (/^(- |\d+\. )/.test(line)) {
        const ordered = /^\d/.test(line);
        const items = [];
        for (; i < lines.length && /^(- |\d+\. )/.test(lines[i]); i += 1) {
          items.push(`<li>${inline(lines[i].replace(/^(- |\d+\. )/, ""))}</li>`);
        }
        out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      } else if (!line.trim()) {
        i += 1;
      } else {
        const para = [];
        for (; i < lines.length && lines[i].trim() && !/^(#{1,3} |- |\d+\. |\||```)/.test(lines[i]); i += 1) {
          para.push(inline(lines[i].replace(/\s+$/, "")) + (/ {2}$/.test(lines[i]) ? "<br>" : ""));
        }
        out.push(`<p>${para.join(" ")}</p>`);
      }
    }
    return out.join("\n");
  }

  // ---------- page-wide interactions ----------

  document.addEventListener("click", (event) => {
    const tag = event.target.closest(".tag[data-ref]");
    if (tag) {
      const ref = tag.dataset.ref;
      const target = document.getElementById(ref[0] === "R" ? `ref-${ref}` : `ex-${ref}`);
      if (target) {
        target.scrollIntoView({ block: "center" });
        target.classList.remove("flash");
        void target.offsetWidth;
        target.classList.add("flash");
      }
      return;
    }
    const more = event.target.closest(".exhibit .more");
    if (more) {
      const open = more.parentElement.classList.toggle("open");
      more.textContent = open ? "Show less" : "Show all";
      more.setAttribute("aria-expanded", String(open));
    }
  });

  $("copy-md").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(run.report);
      $("copy-md").textContent = "Copied";
    } catch {
      $("copy-md").textContent = "Copy failed";
    }
    setTimeout(() => { $("copy-md").textContent = "Copy Markdown"; }, 1600);
  });

  function route() {
    const match = location.hash.match(/^#\/i\/([\w-]+)/);
    if (match) showDesk(match[1]);
    else showHome();
  }
  window.addEventListener("hashchange", route);
  route();
})();
