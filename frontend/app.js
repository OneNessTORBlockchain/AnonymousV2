"use strict";

/* ------------------------------------------------------------------ *
 * Anonymous V2 operator console (vanilla JS, no build step)
 * ------------------------------------------------------------------ */

const $ = (selector) => document.querySelector(selector);

const state = {
  sessionId: null,
  agent: "captain",
  busy: false,
  controller: null,
  run: null,        // current .run element of the active turn
  segment: null,    // current .segment element
  bubble: null,     // current streaming .bubble element
  thinking: null,   // current <details> for reasoning tokens
  toolCards: new Map(),
  lastSeq: 0,
  // Fleet turns interleave several agents in one stream: every agent gets its
  // own segment, bubble, reasoning block and tool cards.
  activeAgent: "",
  segmentByAgent: new Map(),
  bubbleByAgent: new Map(),
  thinkingByAgent: new Map(),
};

function agentKey(agent) {
  return String(agent || "agent").trim() || "agent";
}


const transcript = $("#transcript");
const emptyState = $("#empty");
const composer = $("#composer");
const messageBox = $("#message");
const agentSelect = $("#agentSelect");
const modeSelect = $("#modeSelect");
const sendButton = $("#send");
const stopButton = $("#stop");
const ticker = $("#ticker");
const statusBar = $("#status");
const roundsInput = $("#roundsInput");
const untilComplete = $("#untilComplete");
const agentsSelect = $("#agentsSelect");
const chatFileInput = $("#chatFileInput");
const chatFolderInput = $("#chatFolderInput");
const sidebarToggle = $("#sidebarToggle");
const sidebarResizer = $("#sidebarResizer");

/* ---------------- small helpers ---------------- */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function markdown(raw) {
  if (!raw) return "";
  let html = escapeHtml(raw);
  const blocks = [];

  html = html.replace(/```[a-zA-Z0-9_+-]*\n?([\s\S]*?)```/g, (_match, code) => {
    blocks.push("<pre><code>" + String(code).replace(/\n$/, "") + "</code></pre>");
    return "\u0000B" + (blocks.length - 1) + "\u0000";
  });
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  html = html.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>'
  );
  html = html.replace(/^#{1,4}\s+(.*)$/gm, "<h3>$1</h3>");
  html = html.replace(/^[ \t]*[-*][ \t]+(.*)$/gm, "<li>$1</li>");
  html = html.replace(/(?:<li>[\s\S]*?<\/li>\s*)+/g, (chunk) => "<ul>" + chunk.trim() + "</ul>");
  html = html
    .split(/\n{2,}/)
    .map((block) => {
      const trimmed = block.trim();
      if (!trimmed) return "";
      if (/^<(h3|ul|pre|li|br)/.test(trimmed)) return trimmed;
      return "<p>" + trimmed.replace(/\n/g, "<br>") + "</p>";
    })
    .join("");
  return html.replace(/\u0000B(\d+)\u0000/g, (_match, index) => blocks[Number(index)] || "");
}

async function api(path, options = {}) {
  const request = { headers: { "Content-Type": "application/json" }, ...options };
  const response = await fetch(path, request);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ? JSON.stringify(body.detail) : detail;
    } catch (_err) {
      /* keep statusText */
    }
    throw new Error(`${response.status} ${detail}`);
  }
  return response.status === 204 ? null : response.json();
}

function scrollDown() {
  transcript.scrollTop = transcript.scrollHeight;
}

function showEmpty(show) {
  if (emptyState) emptyState.hidden = !show;
}

function setTicker(text) {
  ticker.textContent = text || "";
}

/* ---------------- labels & refs ---------------- */
const MODE_LABELS = { exploit: "exploit", intrusive: "hands-on", recon: "discovery-first" };
const SESSION_KEY = "av2.session";

const modelName = $("#modelName");
const containerName = $("#containerName");
const agentsBox = $("#agents");
const fleetLine = $("#fleetLine");
const targetsList = $("#targets");
const targetForm = $("#targetForm");
const targetInput = $("#targetInput");
const clearTargets = $("#clearTargets");
const sessionsList = $("#sessions");
const newSessionButton = $("#newSession");

let agentSpecs = [];

function oneLine(value, limit) {
  const text = String(value === undefined || value === null ? "" : value)
    .replace(/\s+/g, " ")
    .trim();
  if (!limit || text.length <= limit) return text;
  return text.slice(0, limit - 1) + "…";
}

function shortJson(value, limit = 160) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return oneLine(value, limit);
  try {
    return oneLine(JSON.stringify(value), limit);
  } catch (_error) {
    return oneLine(String(value), limit);
  }
}

/* ---------------- status ---------------- */
function setPill(key, status, title) {
  const pill = statusBar.querySelector(`.pill[data-key="${key}"]`);
  if (!pill) return;
  pill.classList.remove("ok", "bad", "busy");
  if (status) pill.classList.add(status);
  if (title !== undefined) pill.title = title;
}

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    setPill("api", "ok", `v${health.version}`);
    setPill("provider", health.ollama ? "ok" : "bad", `${health.provider} · godmode ${health.godmode}`);
    setPill("ollama", health.ollama ? "ok" : "bad", health.ollama_detail || "no listener");
    setPill("sandbox", health.sandbox ? "ok" : "bad", health.sandbox_detail || "container unavailable");
    modelName.textContent = health.planner_model || "—";
    modelName.title = `${health.provider} · tools ${health.planner_tools ? "native" : "text"} · ctx ${health.context_window || "?"} (−${health.context_reserve ?? 0}) · godmode ${health.godmode} · skill ${health.godmode_skill ? "installed" : "missing"} · tx gate ${health.transaction_gate ? "on" : "off"}`;
    const match = /container\s+([^=;\s]+)=(\S+)/.exec(health.sandbox_detail || "");
    containerName.textContent = match ? `${match[1]} · ${match[2]}` : "container ?";
  } catch (error) {
    setPill("api", "bad", String(error.message || error));
    setPill("provider", null, "unknown");
    setPill("ollama", null, "unreachable");
    setPill("sandbox", null, "unknown");
    modelName.textContent = "offline";
    containerName.textContent = "container ?";
  }
}

async function refreshFleet() {
  if (!fleetLine) return;
  try {
    const fleet = await api("/api/fleet");
    const roster = fleet.roster || [];
    const models = (fleet.models || []).map((name) => String(name).split("/").pop());
    const short = (name) => String(name || "").split("/").pop();
    const groups = Number(fleet.groups || 1);
    const size = Number(fleet.size || roster.length || 5);
    fleetLine.textContent = `fleet ${size} · ${groups}×${fleet.group_size || 5} squads · ${models.join(" + ")} · ${fleet.rounds || 1} round(s)`;
    fleetLine.title = roster
      .map(
        (agent) =>
          `${agent.label || agent.lane_id} · ${agent.category} · ${short(agent.model)} · ${agent.workdir}`
      )
      .join("\n");
    fleetLine.dataset.ready = "1";
  } catch (error) {
    fleetLine.textContent = `fleet: ${String(error.message || error)}`;
  }
}

/* ---------------- provider: endpoint, model, key ---------------- */
const providerForm = $("#providerForm");
const providerLabel = $("#providerLabel");
const providerBase = $("#providerBase");
const providerModel = $("#providerModel");
const providerKey = $("#providerKey");
const providerHint = $("#providerHint");
const providerRefresh = $("#providerRefresh");

function providerSummary(info) {
  const shape = info.openai_style ? "openai · /chat/completions" : "ollama · /api/chat";
  const state = info.reachable ? "reachable" : "unreachable";
  return `${info.provider} · ${shape} · ${state}${info.detail ? ` · ${info.detail}` : ""}`;
}

async function loadProvider() {
  if (!providerHint) return;
  try {
    const info = await api("/api/provider");
    providerLabel.value = info.provider || "";
    providerBase.value = info.base_url || "";
    providerModel.value = info.model || "";
    providerKey.value = "";
    providerKey.placeholder = info.api_key_set ? `unchanged (${info.api_key_hint})` : "paste the key";
    providerHint.textContent = providerSummary(info);
    providerHint.title = `${info.env_path || ""}\nfleet: ${(info.fleet_models || []).join(", ")}`;
  } catch (error) {
    providerHint.textContent = `provider: ${String(error.message || error)}`;
  }
}

async function saveProvider() {
  if (!providerForm) return;
  const body = {
    provider: providerLabel.value.trim(),
    base_url: providerBase.value.trim(),
    model: providerModel.value.trim(),
    api_key: providerKey.value.trim(),
  };
  if (!body.provider && !body.base_url && !body.model && !body.api_key) {
    setTicker("nothing to apply - fill a field first");
    return;
  }
  providerHint.textContent = "applying and testing…";
  try {
    const info = await api("/api/provider", { method: "POST", body: JSON.stringify(body) });
    providerKey.value = "";
    providerKey.placeholder = info.api_key_set ? `unchanged (${info.api_key_hint})` : "paste the key";
    providerHint.textContent = `applied · ${providerSummary(info)}`;
    providerHint.title = `${info.env_path || ""}\nfleet: ${(info.fleet_models || []).join(", ")}`;
    setTicker(`provider ${info.provider} → ${info.base_url}`);
    modelName.textContent = info.model || modelName.textContent;
    await refreshHealth();
    await refreshFleet();
  } catch (error) {
    providerHint.textContent = `apply failed: ${String(error.message || error)}`;
    setTicker("provider update failed");
  }
}

/* ---------------- agents ---------------- */
function renderAgents() {
  agentsBox.textContent = "";
  for (const spec of agentSpecs) {
    const button = el("button", "agent");
    button.type = "button";
    button.dataset.agent = spec.name;
    button.classList.toggle("active", state.agent === spec.name);
    button.append(el("b", null, spec.name), el("small", null, spec.title));
    button.title = `${spec.mission}\ntools: ${spec.tools.join(", ") || "none"}`;
    button.addEventListener("click", () => chooseAgent(spec.name));
    agentsBox.append(button);
  }
}

function chooseAgent(name) {
  state.agent = name;
  if (agentSelect) agentSelect.value = name;
  renderAgents();
  setTicker(`${name} selected`);
}

async function loadAgents() {
  try {
    agentSpecs = await api("/api/agents");
  } catch (error) {
    agentsBox.append(el("p", "hint", `agents unavailable: ${error.message || error}`));
    return;
  }
  if (!agentSpecs.length) return;
  if (!agentSpecs.some((spec) => spec.name === state.agent)) {
    state.agent = agentSpecs[0].name;
  }
  agentSelect.textContent = "";
  for (const spec of agentSpecs) {
    const option = el("option", null, `${spec.name} — ${spec.title}`);
    option.value = spec.name;
    option.title = spec.mission;
    agentSelect.append(option);
  }
  agentSelect.value = state.agent;
  renderAgents();
  refreshFleet();
}

/* ---------------- targets ---------------- */
function renderScope(entries) {
  targetsList.textContent = "";
  if (!entries.length) {
    targetsList.append(el("li", "none", "no targets noted"));
    return;
  }
  for (const entry of entries) {
    const item = el("li");
    item.append(el("span", null, entry.value));
    item.append(el("em", null, String(entry.mode || entry.kind || "note").toUpperCase()));
    const remove = el("button", null, "×");
    remove.type = "button";
    remove.title = `remove ${entry.value}`;
    remove.addEventListener("click", async () => {
      try {
        await api(`/api/scope?value=${encodeURIComponent(entry.value)}`, { method: "DELETE" });
        await loadScope();
      } catch (error) {
        setTicker(String(error.message || error));
      }
    });
    item.append(remove);
    item.title = entry.note || entry.kind || "";
    targetsList.append(item);
  }
}

async function loadScope() {
  try {
    const payload = await api("/api/scope");
    renderScope(payload.entries || []);
  } catch (error) {
    targetsList.textContent = "";
    targetsList.append(el("li", "none", "targets unavailable"));
    setTicker(String(error.message || error));
  }
}

/* ---------------- sessions ---------------- */
function renderSessions(items) {
  sessionsList.textContent = "";
  if (!items.length) {
    sessionsList.append(el("li", "none", "no sessions yet"));
    return;
  }
  for (const item of items) {
    const row = el("li");
    const load = el("button", "load");
    load.type = "button";
    load.dataset.session = item.session_id;
    load.classList.toggle("active", item.session_id === state.sessionId);
    const sid = el("span", "sid", item.session_id);
    const preview = el("span", "prev", item.preview || `${item.messages} messages`);
    load.append(sid, preview);
    load.title = item.updated_at ? `updated ${item.updated_at}` : item.session_id;
    load.addEventListener("click", () => openSession(item.session_id));

    const remove = el("button", "del", "×");
    remove.type = "button";
    remove.title = `delete ${item.session_id}`;
    remove.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        await api(`/api/sessions/${encodeURIComponent(item.session_id)}`, { method: "DELETE" });
        if (state.sessionId === item.session_id) startNewSession();
        await loadSessions();
      } catch (error) {
        setTicker(String(error.message || error));
      }
    });

    row.append(load, remove);
    sessionsList.append(row);
  }
}

async function loadSessions() {
  try {
    const payload = await api("/api/sessions");
    renderSessions(payload.sessions || []);
  } catch (error) {
    sessionsList.textContent = "";
    sessionsList.append(el("li", "none", "sessions unavailable"));
    setTicker(String(error.message || error));
  }
}

function resetTranscript() {
  transcript.textContent = "";
  if (emptyState) transcript.append(emptyState);
  showEmpty(true);
  state.run = null;
  state.segment = null;
  state.bubble = null;
  state.thinking = null;
  state.toolCards = new Map();
  state.lastSeq = 0;
  state.activeAgent = "";
  state.segmentByAgent = new Map();
  state.bubbleByAgent = new Map();
  state.thinkingByAgent = new Map();
}

function renderHistory(messages) {
  let run = null;
  for (const message of messages) {
    if (!message || typeof message !== "object") continue;
    const content = String(message.content || "");
    if (message.role === "user") {
      const turn = el("div", "turn");
      run = el("div", "run");
      turn.append(el("div", "user-msg", content), run);
      transcript.append(turn);
    } else if (message.role === "assistant" && content.trim()) {
      if (!run) {
        const turn = el("div", "turn");
        run = el("div", "run");
        turn.append(run);
        transcript.append(turn);
      }
      const bubble = el("div", "bubble final");
      bubble.innerHTML = markdown(content);
      run.append(bubble);
    }
  }
  showEmpty(!transcript.querySelector(".turn"));
  scrollDown();
}

async function openSession(sessionId) {
  if (!sessionId) return;
  if (state.busy) {
    setTicker("stop the running turn first");
    return;
  }
  try {
    const payload = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
    resetTranscript();
    renderHistory(payload.messages || []);
    state.sessionId = sessionId;
    localStorage.setItem(SESSION_KEY, sessionId);
    setTicker(`session ${sessionId}`);
    await loadSessions();
  } catch (error) {
    setTicker(String(error.message || error));
  }
}

function startNewSession(clearStored = true) {
  if (state.busy) {
    setTicker("stop the running turn first");
    return;
  }
  state.sessionId = null;
  if (clearStored) localStorage.removeItem(SESSION_KEY);
  resetTranscript();
  loadSessions();
  setTicker("new session");
}

/* ---------------- transcript builders ---------------- */
function ensureRun() {
  if (state.run && state.run.isConnected) return state.run;
  const turn = el("div", "turn");
  const run = el("div", "run");
  turn.append(run);
  transcript.append(turn);
  state.run = run;
  state.segment = null;
  state.bubble = null;
  state.thinking = null;
  return run;
}

function newSegment(agent, task, depth, data = {}) {
  const run = ensureRun();
  const key = agentKey(agent);
  const level = Number(depth) || 0;
  const segment = el("div", level ? "segment deep" : "segment");
  segment.dataset.agent = key;
  const head = el("div", "seg-head");
  const badge = el("span", /reporter/i.test(key) ? "badge reporter" : "badge", key);
  head.append(badge);
  if (data && data.model) {
    const chip = el("span", "model-chip", String(data.model).split("/").pop());
    chip.title = `model: ${data.model}`;
    head.append(chip);
  }
  if (data && data.terminal) {
    const term = el("span", "terminal-chip", `▸ ${data.terminal}`);
    term.title = data.workdir ? `terminal ${data.terminal} · ${data.workdir}` : `terminal ${data.terminal}`;
    head.append(term);
  }
  if (task) head.append(el("span", "task", oneLine(task, 110)));
  if (level) head.append(el("span", null, `depth ${level}`));
  segment.append(head);
  run.append(segment);
  state.segmentByAgent.set(key, segment);
  state.bubbleByAgent.delete(key);
  state.thinkingByAgent.delete(key);
  state.activeAgent = key;
  state.segment = segment;
  state.bubble = null;
  state.thinking = null;
  scrollDown();
  return segment;
}

function currentSegment() {
  const key = agentKey(state.activeAgent);
  const existing = state.segmentByAgent.get(key);
  if (existing && existing.isConnected) return existing;
  return newSegment(state.activeAgent || "agent", "", 0);
}

function ensureBubble() {
  const key = agentKey(state.activeAgent);
  const current = state.bubbleByAgent.get(key);
  if (current && current.isConnected) {
    state.bubble = current;
    return current;
  }
  const bubble = el("div", "bubble");
  bubble._raw = "";
  bubble._raf = 0;
  bubble.dataset.agent = key;
  currentSegment().append(bubble);
  state.bubbleByAgent.set(key, bubble);
  state.bubble = bubble;
  return bubble;
}

function flushBubble(bubble) {
  if (!bubble) return;
  if (bubble._raf) {
    cancelAnimationFrame(bubble._raf);
    bubble._raf = 0;
  }
  bubble.innerHTML = markdown(bubble._raw || "");
}

function appendToken(text) {
  if (!text) return;
  const bubble = ensureBubble();
  bubble._raw += text;
  if (bubble._raf) return;
  bubble._raf = requestAnimationFrame(() => {
    bubble._raf = 0;
    bubble.innerHTML = markdown(bubble._raw);
    scrollDown();
  });
}

function appendThinking(text) {
  if (!text) return;
  const key = agentKey(state.activeAgent);
  let details = state.thinkingByAgent.get(key);
  if (!details || !details.isConnected) {
    details = el("details", "thinking");
    details.append(el("summary", null, "reasoning"), el("pre", null, ""));
    currentSegment().append(details);
    state.thinkingByAgent.set(key, details);
    state.thinking = details;
  }
  const pre = details.querySelector("pre");
  if (pre) pre.textContent += text;
  scrollDown();
}

function toolCaption(name, args) {
  const payload = args && typeof args === "object" ? args : {};
  if (name === "delegate") {
    const who = payload.agent || "specialist";
    return `${who} ← ${oneLine(payload.task || "", 140)}`;
  }
  if (name === "verify_transaction") {
    return `${payload.chain || "ethereum"} · ${oneLine(payload.tx_hash || "", 90)}`;
  }
  const command = payload.command || payload.cmd || payload.script || payload.query || payload.url;
  if (command) return oneLine(command, 220);
  return shortJson(payload, 220) || "no arguments";
}

function addToolCall(data) {
  const segment = currentSegment();
  const name = data.name || "tool";
  const key = agentKey(state.activeAgent);
  const card = el("details", "tool run");
  card.dataset.name = name;
  card.dataset.agent = key;
  if (data.call_id) card.dataset.callId = data.call_id;
  const summary = el("summary");
  const cmd = toolCaption(name, data.args);
  summary.append(
    el("span", null, name),
    el("span", "cmd", cmd),
    el("span", "status", "running")
  );
  card.title = cmd;
  card.append(summary);
  segment.append(card);
  state.toolCards.set(`${key}|${data.call_id || name}`, card);
  state.bubble = null;
  scrollDown();
  return card;
}

function findToolCard(name, callId) {
  const key = agentKey(state.activeAgent);
  if (callId && state.toolCards.has(`${key}|${callId}`)) {
    return state.toolCards.get(`${key}|${callId}`);
  }
  for (const card of state.toolCards.values()) {
    if (
      card.dataset.agent === key &&
      card.dataset.name === name &&
      !card.dataset.done &&
      card.isConnected
    ) {
      return card;
    }
  }
  return null;
}

const MAX_TOOL_VIEW = 12_000;

function finishTool(data) {
  const name = data.name || "tool";
  const card = findToolCard(name) || addToolCall({ name, args: data.args || {} });
  card.dataset.done = "1";
  card.classList.remove("run");
  const ok = data.ok !== false;
  card.classList.add(ok ? "ok" : "err");
  const status = card.querySelector(".status");
  if (status) status.textContent = ok ? (data.delegate ? `delegated · ${data.delegate}` : "done") : "failed";
  if (!ok) card.open = true;
  const output = String(data.content || data.text || "").trim();
  if (output) {
    const trimmed = output.length > MAX_TOOL_VIEW ? `${output.slice(0, MAX_TOOL_VIEW)}\n… (truncated)` : output;
    card.append(el("pre", null, trimmed));
  }
  state.thinking = null;
  state.bubble = null;
  scrollDown();
}

function addNote(text, isError) {
  const note = el("div", isError ? "note error" : "note", String(text || "").trim());
  currentSegment().append(note);
  scrollDown();
}

/* ---------------- turn events ---------------- */
function onStart(event) {
  const data = event.data || {};
  ensureRun();
  if (Array.isArray(data.models) && data.models.length) {
    modelName.textContent = data.models.join(" + ");
  } else if (data.model) {
    modelName.textContent = data.model;
  }
  const stance = MODE_LABELS[data.mode] || data.mode || "turn";
  setPill("ollama", "busy", "generating…");
  if (data.fleet) {
    const lanes = (Array.isArray(data.models) && data.models.length) || 2;
    const size = data.fleet_size || 5;
    const groups = data.fleet_groups || Math.round(size / 5) || 1;
    setTicker(
      `fleet ${size} agents · ${groups} squad${groups === 1 ? "" : "s"} of 5 · ${lanes} model${lanes === 1 ? "" : "s"} · warming up`
    );
  } else {
    setTicker(`${stance} · warming up`);
  }
}

function onAgent(event) {
  const data = event.data || {};
  const agent = event.agent || "agent";
  state.activeAgent = agentKey(agent);
  newSegment(agent, data.task || "", data.depth || 0, data);
  const prefix = Number(data.depth) > 0 ? "delegated · " : "";
  const model = data.model ? ` · ${String(data.model).split("/").pop()}` : "";
  setTicker(`${prefix}${agent}${model} working`);
}

function onDone(event) {
  const data = event.data || {};
  const text = String(event.text || "").trim();

  // Flush every agent bubble that streamed during this turn: in a fleet turn
  // several lanes write at once, so one shared bubble would lose text.
  for (const bubble of state.bubbleByAgent.values()) {
    if (bubble && bubble.isConnected) flushBubble(bubble);
  }
  const lastAgent = agentKey(event.agent || state.agent);
  const lastBubble = state.bubbleByAgent.get(lastAgent);

  if (!data.error && text) {
    const streamed = lastBubble && lastBubble.isConnected ? String(lastBubble._raw || "").trim() : "";
    if (streamed && streamed === text) {
      lastBubble.classList.add("final");
    } else {
      state.activeAgent = data.fleet ? "fleet report" : lastAgent;
      const segment = newSegment(
        state.activeAgent,
        data.fleet ? `${data.fleet_size || 5} agents · ${data.run_id || ""}` : "",
        0,
        data.fleet ? { ...data, model: "" } : data
      );
      const final = el("div", "bubble final");
      final.innerHTML = markdown(text);
      segment.append(final);
    }
  } else if (data.error) {
    addNote(String(data.error), true);
  }

  if (data.status === "exhausted") {
    const findings = Number(data.findings || 0);
    addNote(
      `mission exhausted after ${data.rounds || "?"} round(s): ` +
        (findings
          ? `${findings} finding(s) were recorded but the completion gate still says the objective ` +
            "is not proven, so this task is NOT complete."
          : "no finding was recorded, so this task is NOT complete.") +
        " Everything the fleet learned is in the Learning Board panel; narrow the objective or run again " +
        "with until-complete ticked.",
      true
    );
  } else if (data.status === "terminated") {
    addNote(
      "every agent was terminated for stopping without evidence - this mission produced no " +
        "result. Re-run with a narrower objective or check the board for what blocked them.",
      true
    );
  } else if (data.detached) {
    addNote(
      `the chat stream detached, but fleet ${data.run_id || ""} is still working until the mission ` +
        "is proven or all rounds are over. Open the swarm panel to follow it, or type a message " +
        "to steer it.",
      false
    );
  } else if (data.fleet && data.enforced) {
    addNote(
      `enforcement kept the fleet working ${data.enforced} time(s); ${data.findings || 0} finding(s) ` +
        `recorded, ${data.learned || 0} lesson(s) written to the knowledge base` +
        (data.terminated && data.terminated.length
          ? `, ${data.terminated.length} agent(s) terminated for stopping empty.`
          : "."),
      false
    );
  }

  state.bubble = null;
  state.thinking = null;

  if (data.session_id) {
    state.sessionId = data.session_id;
    localStorage.setItem(SESSION_KEY, data.session_id);
  } else if (event.session_id) {
    state.sessionId = event.session_id;
    localStorage.setItem(SESSION_KEY, event.session_id);
  }

  const bits = [];
  if (data.iterations) bits.push(`${data.iterations} step${data.iterations === 1 ? "" : "s"}`);
  if (data.tool_calls !== undefined) bits.push(`${data.tool_calls} tools`);
  if (data.model) bits.push(data.model);
  if (data.error) bits.push("error");
  setTicker(bits.join(" · ") || "done");
  refreshHealth();
  loadSessions();
  loadWorkspace();
  scrollDown();
}

function handleEvent(event) {
  if (typeof event.seq === "number" && event.seq > 0) state.lastSeq = event.seq;
  if (event.agent) state.activeAgent = agentKey(event.agent);
  switch (event.type) {
    case "start":
      onStart(event);
      break;
    case "agent":
      onAgent(event);
      break;
    case "token":
      appendToken(event.text || "");
      break;
    case "thinking":
      appendThinking(event.text || "");
      break;
    case "tool_call": {
      const data = event.data || {};
      addToolCall(data);
      const what = data.command || data.url || toolCaption(data.name, data.args);
      setTicker(`${event.agent || "agent"} running · ${oneLine(what || data.name || "tool", 120)}`);
      break;
    }
    case "tool_result": {
      const data = event.data || {};
      finishTool(data);
      setTicker(`${event.agent || "agent"} · ${data.name || "tool"} ${data.ok === false ? "failed" : "done"}`);
      break;
    }
    case "notice":
      addNote(event.text || "notice", false);
      break;
    case "error":
      addNote(event.text || "error", true);
      break;
    case "done":
      onDone(event);
      break;
    default:
      break;
  }
}

function handleFrame(frame) {
  const dataLines = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;
  const raw = dataLines.join("\n");
  if (!raw) return;
  let event;
  try {
    event = JSON.parse(raw);
  } catch (_error) {
    return;
  }
  if (!event || typeof event.type !== "string") return;
  handleEvent(event);
}

/* ---------------- SSE consumer ---------------- */
async function streamTurn(payload) {
  const controller = new AbortController();
  state.controller = controller;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail ? JSON.stringify(body.detail) : detail;
      } catch (_error) {
        /* keep statusText */
      }
      throw new Error(`${response.status} ${detail}`);
    }
    if (!response.body) throw new Error("this browser cannot stream responses");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        handleFrame(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) handleFrame(buffer);
  } finally {
    if (state.controller === controller) state.controller = null;
  }
}

function setBusy(busy) {
  state.busy = busy;
  sendButton.disabled = busy;
  stopButton.hidden = !busy;
}

const CHAT_HELP = [
  "### chat commands — they work at any time, even while a fleet is running",
  "",
  "- `/squads 3` — squads of five for the next task (1-5 => 5/10/15/20/25 agents)",
  "- `/rounds 3` — rounds for the next task (1-50; ignored when until-done is on)",
  "- `/until on|off` — keep re-planning with no round cap until the mission is proven",
  "- `/stop` — stop the live fleet **and** abort the streaming turn",
  "- `/steer <text>` — send a new direction to the live fleet (typing a message while it",
  "  runs does the same: the directive outranks the plan and every agent switches)",
  "- `/files [path]` — list the sandbox workspace",
  "- `/download <path>` — download a file, or a zip of a folder, from the agents",
  "- `/delete <path>` — delete one file or folder inside the sandbox",
  "- `/wipe` — delete every file and folder in the workspace",
  "- `/memory [query]` — search the Learning Board; `/memory forget <id>` deletes a lesson",
  "- `/skill` — list skills; `/skill run <name> [args]`; `/skill delete <name>`",
  "- `/swarm` — latest run: lanes, findings, enforcement; `/swarm stop`",
  "",
  "Anything else is sent to the fleet as the next objective. Attach files with the `attach`",
  "button, or drop them on the window, and the paths are written into your message.",
].join("\n");

async function handleCommand(raw) {
  const text = String(raw || "").trim();
  const match = text.match(/^\/([a-zA-Z]+)\s*([\s\S]*)$/);
  if (!match) return false;
  const command = match[1].toLowerCase();
  const rest = match[2].trim();

  if (command === "help" || command === "?") {
    systemTurn(CHAT_HELP);
    return true;
  }
  if (command === "agents" || command === "squads" || command === "groups") {
    const wanted = Number(rest);
    if (wanted >= 1 && wanted <= 5) {
      agentsSelect.value = String(wanted);
      systemTurn(`squads set to **${wanted}** → **${wanted * 5} agents** on the task.`);
    } else {
      systemTurn(
        `squads are **${agentsSelect.value || 1}** of five. Use \`/squads 1\` … \`/squads 5\` (5-25 agents).`
      );
    }
    return true;
  }
  if (command === "rounds") {
    const wanted = Number(rest);
    if (wanted >= 1 && wanted <= 50) {
      roundsInput.value = String(wanted);
      systemTurn(`rounds set to **${wanted}**.`);
    } else {
      systemTurn(`rounds are **${roundsInput.value || 2}**. Use \`/rounds 1\` … \`/rounds 50\`.`);
    }
    return true;
  }
  if (command === "until") {
    const on = /^(on|yes|true|1)$/i.test(rest);
    const off = /^(off|no|false|0)$/i.test(rest);
    if (on || off) {
      untilComplete.checked = on;
      if (on) roundsInput.value = "50";
      systemTurn(
        on
          ? "until-done is **on**: the fleet re-plans until the completion gate approves, with no round cap."
          : "until-done is **off**: the rounds field is the cap again."
      );
    } else {
      systemTurn(
        `until-done is **${untilComplete.checked ? "on" : "off"}**. Use \`/until on\` or \`/until off\`.`
      );
    }
    return true;
  }
  if (command === "stop") {
    const stopped = await stopFleet();
    const streaming = Boolean(state.controller);
    if (streaming) state.controller.abort();
    if (stopped.length) {
      systemTurn(
        `stopped **${stopped.length}** live fleet run(s): ${stopped.join(", ")}. ` +
          "The next message starts a fresh task with no continuation of the old one."
      );
    } else if (streaming) {
      systemTurn("stopping the running turn…");
    } else if (!swarmPanel.hidden) {
      await stopSwarm();
      systemTurn("swarm stop requested.");
    } else {
      systemTurn("nothing is running right now.");
    }
    return true;
  }
  if (command === "steer") {
    if (!rest) {
      systemTurn(
        "usage: `/steer <new direction>` — or simply type a message while the fleet is " +
          "running: it is delivered as a directive that supersedes the current plan."
      );
      return true;
    }
    await steerFleet(rest);
    return true;
  }
  if (command === "download") {
    if (!rest) {
      systemTurn("usage: `/download <path>` — e.g. `/download /work/out/report.md`");
      return true;
    }
    const path = rest.startsWith("/") ? rest : workspaceAbsolute(rest);
    window.location.href = `/api/files/download?path=${encodeURIComponent(path)}`;
    systemTurn(`downloading \`${path}\`…`);
    return true;
  }
  if (command === "delete") {
    if (!rest) {
      systemTurn("usage: `/delete <path>` — one file or folder inside the workspace");
      return true;
    }
    const path = rest.startsWith("/") ? rest : workspaceAbsolute(rest);
    await deleteWorkspacePath(path, false);
    return true;
  }
  if (command === "wipe") {
    await wipeWorkspace();
    return true;
  }
  if (command === "files") {
    try {
      const payload = await api(`/api/files${rest ? `?path=${encodeURIComponent(rest)}` : ""}`);
      const entries = (payload.entries || []).slice(0, 40);
      const lines = entries.length
        ? entries
            .map(
              (entry) =>
                `- \`${entry.path}\`${entry.dir ? "/ (folder)" : ` · ${entry.human_size || humanSize(entry.size)}`}`
            )
            .join("\n")
        : "*(empty)*";
      const card = systemTurn(`### workspace ${payload.root}\n\n${lines}`);
      if (entries.length) {
        const box = el("div", "artifacts");
        for (const entry of entries.slice(0, 8)) {
          const link = el("a", "artifact", entry.path);
          link.href = `/api/files/download?path=${encodeURIComponent(workspaceAbsolute(entry.path))}`;
          if (!entry.dir) link.download = entry.name;
          box.append(link);
        }
        card.append(box);
      }
    } catch (error) {
      systemTurn(`workspace unavailable: ${(error && error.message) || error}`);
    }
    return true;
  }
  if (command === "memory") {
    if (/^forget\s+/i.test(rest)) {
      const id = Number(rest.replace(/^forget\s+/i, ""));
      if (id) await deleteMemoryNote(id);
      return true;
    }
    try {
      const params = new URLSearchParams({ limit: "25" });
      if (rest) params.set("query", rest);
      const payload = await api(`/api/learning?${params.toString()}`);
      const notes = payload.notes || [];
      const stats = payload.stats || {};
      const body = notes.length
        ? notes
            .map(
              (note) =>
                `- **#${note.id} ${note.status}/${note.category}** — ${note.title}\n  ` +
                String(note.procedure || note.body || "").slice(0, 220)
            )
            .join("\n")
        : "*(nothing stored yet)*";
      systemTurn(
        `### memory · ${stats.total || 0} note(s)\n\n${body}\n\nEdit, validate or delete lessons in the Learning Board panel, or \`/memory forget <id>\`.`
      );
      await loadMemory();
    } catch (error) {
      systemTurn(`memory unavailable: ${(error && error.message) || error}`);
    }
    return true;
  }
  if (command === "skill") {
    const runMatch = rest.match(/^run\s+(\S+)\s*([\s\S]*)$/i);
    const deleteMatch = rest.match(/^delete\s+(\S+)$/i);
    if (runMatch) {
      await runSkillFromPanel(runMatch[1], runMatch[2]);
      return true;
    }
    if (deleteMatch) {
      await deleteSkillFromPanel(deleteMatch[1]);
      return true;
    }
    try {
      const payload = await api("/api/skills?limit=100");
      const skills = payload.skills || [];
      systemTurn(
        `### skills · ${skills.length}\n\n` +
          (skills.length
            ? skills
                .map(
                  (skill) =>
                    `- **${skill.slug}** (${skill.language}, ${skill.runs || 0} runs) — ${String(
                      skill.description || ""
                    ).slice(0, 140)}`
                )
                .join("\n")
            : "*(none yet — agents create them with skill_create)*")
      );
      await loadSkills();
    } catch (error) {
      systemTurn(`skills unavailable: ${(error && error.message) || error}`);
    }
    return true;
  }
  if (command === "swarm") {
    if (/^stop$/i.test(rest)) {
      await stopSwarm();
      systemTurn("swarm stop requested.");
      return true;
    }
    try {
      const payload = await api("/api/swarm?limit=1");
      const run = (payload.runs || [])[0];
      if (!run) {
        systemTurn("no swarm runs yet.");
        return true;
      }
      const status = await api(`/api/swarm/${encodeURIComponent(run.run_id)}`);
      const enforcement = status.enforcement || {};
      const knowledge = status.knowledge || {};
      systemTurn(
        `### fleet ${status.run_id}\n\n- status: **${status.status}** · round ${status.round}/${status.rounds}\n` +
          `- lanes: ${(status.lanes || []).length} · findings: ${(status.memory && status.memory.finding) || 0}\n` +
          `- enforcement pushes: ${enforcement.pushes || 0} · unresolved: ${(enforcement.unresolved || []).join(", ") || "none"}\n` +
          `- lessons learned: ${knowledge.learned || 0}`
      );
      await openSwarm(status.run_id);
    } catch (error) {
      systemTurn(`swarm status unavailable: ${(error && error.message) || error}`);
    }
    return true;
  }
  return null;
}

async function steerFleet(text, quiet = false) {
  // "Stop and do this instead": while a fleet is live, a plain chat message is
  // delivered as a directive that outranks the plan, not queued behind it.
  messageBox.value = "";
  try {
    const payload = await api("/api/chat/steer", {
      method: "POST",
      body: JSON.stringify({ message: text, session_id: state.sessionId }),
    });
    const turn = el("div", "turn");
    turn.append(el("div", "user-msg", text));
    const run = el("div", "run");
    run.append(
      el(
        "div",
        "bubble final",
        `**Directive delivered** to fleet ${payload.run_id} at round ${payload.round}/${payload.rounds}. ` +
          "It supersedes the current plan: every agent picks it up at its next step, and the " +
          "strategist re-plans around it."
      )
    );
    turn.append(run);
    transcript.append(turn);
    showEmpty(false);
    setTicker(`directive delivered · ${payload.run_id}`);
    loadSwarm();
    return true;
  } catch (error) {
    messageBox.value = text;
    // A 409 means the run just finished: clear the stale live state so the next
    // send starts a fresh turn instead of trying to steer a dead run.
    swarmState.status = null;
    if (!quiet) {
      systemTurn(
        `directive not delivered (${(error && error.message) || error}). ` +
          "If the fleet just finished, send it again to start a new turn."
      );
    }
    return false;
  } finally {
    messageBox.focus();
    scrollDown();
  }
}


async function stopFleet() {
  // Stop the backend run, not just this browser's stream: a stream abort leaves
  // the fleet working, which looks like the agent ignoring a "stop".
  try {
    const query = new URLSearchParams();
    if (state.sessionId) query.set("session_id", state.sessionId);
    const payload = await api(`/api/chat/stop?${query.toString()}`, { method: "POST" });
    return payload.stopped || [];
  } catch (error) {
    setTicker(`stop failed: ${(error && error.message) || error}`);
    return [];
  }
}

async function sendMessage() {

  const text = messageBox.value.trim();
  if (!text) return;
  // Chat commands are always available, even while a turn is streaming, so the
  // operator can steer agents, files and memory without waiting.
  if (text.startsWith("/")) {
    messageBox.value = "";
    await handleCommand(text);
    messageBox.focus();
    return;
  }
  if (state.busy) {
    // A message typed while the fleet is working is a new direction, not a
    // queued prompt: deliver it as a directive that outranks the plan.
    await steerFleet(text);
    return;
  }
  if (swarmState.status && SWARM_LIVE.has(swarmState.status.status)) {
    // The chat stream detached (or a run was started from the swarm panel) and
    // the fleet is still working.  Steer it instead of silently starting a
    // second fleet on top of it; a stale status falls through to a new turn.
    if (await steerFleet(text, true)) return;
  }

  messageBox.value = "";
  const turn = el("div", "turn");
  const run = el("div", "run");
  turn.append(el("div", "user-msg", text), run);
  transcript.append(turn);
  showEmpty(false);

  state.run = run;
  state.segment = null;
  state.bubble = null;
  state.thinking = null;
  state.toolCards = new Map();
  state.lastSeq = 0;
  state.activeAgent = "";
  state.segmentByAgent = new Map();
  state.bubbleByAgent = new Map();
  state.thinkingByAgent = new Map();
  scrollDown();

  setBusy(true);
  setTicker("connecting…");
  try {
    await streamTurn({
      message: text,
      session_id: state.sessionId,
      agent: state.agent,
      mode: modeSelect.value || "exploit",
      rounds: Number(roundsInput.value || 0) || undefined,
      groups: Number(agentsSelect.value || 0) || undefined,
      until_complete: Boolean(untilComplete && untilComplete.checked),
      unlimited: Boolean(untilComplete && untilComplete.checked),
      stream: true,
    });
  } catch (error) {
    if (error && error.name === "AbortError") {
      addNote("stopped by operator", false);
      setTicker("stopped");
    } else {
      addNote(`request failed: ${error.message || error}`, true);
      setTicker("error");
    }
  } finally {
    if (state.bubble && state.bubble.isConnected) flushBubble(state.bubble);
    setBusy(false);
    // "download from the agent through the chat box": after each turn the newest
    // files the agents produced are offered as download links in that turn.
    if (state.run) appendArtifacts(state.run);
    messageBox.focus();
  }
}

/* ---------------- durable learning board ---------------- */
const memoryForm = $("#memoryForm");
const memoryKind = $("#memoryKind");
const memoryCategory = $("#memoryCategory");
const memoryRoles = $("#memoryRoles");
const memoryTools = $("#memoryTools");
const memoryTitle = $("#memoryTitle");
const memoryBody = $("#memoryBody");
const memoryReview = $("#memoryReview");
const memoryList = $("#memoryList");
const memoryStats = $("#memoryStats");
const memoryRefresh = $("#memoryRefresh");
const memoryQuery = $("#memoryQuery");
const memoryFilterCategory = $("#memoryFilterCategory");
const memoryFilterRole = $("#memoryFilterRole");
const memoryFilterStatus = $("#memoryFilterStatus");
const memoryFilterResult = $("#memoryFilterResult");
let memoryEditing = 0;

function memorySubmitLabel(text) {
  const button = memoryForm ? memoryForm.querySelector('button[type="submit"]') : null;
  if (button) button.textContent = text;
}

function memoryFilterQuery() {
  const params = new URLSearchParams({ limit: "60" });
  const query = memoryQuery ? memoryQuery.value.trim() : "";
  if (query) params.set("query", query);
  const filters = [
    ["category", memoryFilterCategory],
    ["role", memoryFilterRole],
    ["status", memoryFilterStatus],
    ["result", memoryFilterResult],
  ];
  for (const [key, select] of filters) {
    if (select && select.value) params.set(key, select.value);
  }
  return params.toString();
}

function memoryBadge(text, className) {
  return el("span", `mem-badge ${className || ""}`.trim(), text);
}

function renderMemory(payload) {
  const notes = (payload && payload.notes) || [];
  const stats = (payload && payload.stats) || {};
  memoryStats.textContent = stats.total
    ? `learning board: ${stats.total} lesson(s) · ${stats.validated || 0} validated · ` +
      `${stats.pending_reviews || 0} pending review · ${stats.failures || 0} failed approach(es) · ` +
      `${stats.flagged || 0} flagged · ${stats.uses || 0} references`
    : "learning board: empty — every agent reports here after its task";
  memoryList.textContent = "";
  if (!notes.length) {
    memoryList.append(el("li", "none", "no lessons match these filters"));
    return;
  }
  for (const note of notes.slice(0, 40)) {
    const row = el("li");
    row.dataset.kind = note.kind || "";
    row.dataset.category = note.category || "";
    row.dataset.status = note.status || "";
    row.dataset.result = note.result || "";
    const head = el("div", "mem-head");
    head.append(
      memoryBadge(note.status || "proposed", `status-${note.status || "proposed"}`),
      memoryBadge(note.category || "general-lessons", "category"),
      el("span", "mem-title", note.title || "(untitled)")
    );
    if (note.result === "failure") head.append(memoryBadge("failed method", "result-failure"));
    if (note.result === "success") head.append(memoryBadge("worked", "result-success"));
    const meta = el(
      "div",
      "mem-meta",
      `#${note.id} · ${note.agent || "operator"} · conf ${Number(note.confidence || 0).toFixed(2)} · ` +
        `used ${note.uses || 0} · ${note.updated_at || note.created_at || ""}` +
        (note.score ? ` · relevance ${Number(note.score).toFixed(2)}` : "")
    );
    const body = el(
      "p",
      "mem-body",
      String(note.procedure || note.body || "").slice(0, 280)
    );
    const facets = [];
    if ((note.roles || []).length) facets.push(`roles: ${note.roles.join(", ")}`);
    if ((note.tools || []).length) facets.push(`tools: ${note.tools.join(", ")}`);
    if ((note.tags || []).length) facets.push(`tags: ${note.tags.join(", ")}`);
    const metaLine = facets.length ? el("div", "mem-meta", facets.join(" · ")) : null;
    const actions = el("div", "mem-actions");
    const edit = el("button", "ghost mini", "edit");
    edit.type = "button";
    edit.addEventListener("click", () => startMemoryEdit(note));
    actions.append(edit);
    if (note.status !== "validated") {
      const validate = el("button", "ghost mini", "validate");
      validate.type = "button";
      validate.addEventListener("click", () => reviewMemory(note.id, "validated"));
      actions.append(validate);
    }
    if (note.status !== "deprecated") {
      const deprecate = el("button", "ghost mini", "deprecate");
      deprecate.type = "button";
      deprecate.addEventListener("click", () => reviewMemory(note.id, "deprecated"));
      actions.append(deprecate);
    }
    const related = el("button", "ghost mini", "related");
    related.type = "button";
    related.addEventListener("click", () => showMemoryRelated(note.id));
    const forget = el("button", "ghost mini danger", "delete");
    forget.type = "button";
    forget.addEventListener("click", () => deleteMemoryNote(note.id));
    actions.append(related, forget);
    row.append(head, meta);
    if (metaLine) row.append(metaLine);
    row.append(body, actions);
    memoryList.append(row);
  }
}

function startMemoryEdit(note) {
  memoryEditing = Number(note.id) || 0;
  memoryKind.value = note.kind || "technique";
  if (memoryCategory) memoryCategory.value = note.category || "";
  if (memoryRoles) memoryRoles.value = (note.roles || [])[0] || "";
  if (memoryTools) memoryTools.value = (note.tools || []).join(",");
  memoryTitle.value = note.title || "";
  memoryBody.value = note.procedure || note.body || "";
  if (memoryReview) {
    memoryReview.checked = note.status === "validated";
    memoryReview.disabled = true;
  }
  memorySubmitLabel("update");
  memoryTitle.focus();
}

async function loadMemory() {
  try {
    renderMemory(await api(`/api/learning?${memoryFilterQuery()}`));
  } catch (_error) {
    memoryList.textContent = "";
    memoryList.append(el("li", "none", "learning board unavailable"));
  }
}

async function saveMemory() {
  const title = memoryTitle.value.trim();
  if (!title) {
    setTicker("a lesson needs a title");
    return;
  }
  const body = {
    kind: memoryKind.value || "technique",
    category: memoryCategory ? memoryCategory.value : "",
    roles: memoryRoles && memoryRoles.value ? memoryRoles.value : [],
    tools: memoryTools ? memoryTools.value.trim() : "",
    title,
    body: memoryBody.value.trim(),
    source: "operator",
    agent: "operator",
    review: Boolean(memoryReview && memoryReview.checked),
  };
  try {
    if (memoryEditing) {
      await api(`/api/learning/${memoryEditing}`, { method: "PUT", body: JSON.stringify(body) });
      addNote(`lesson #${memoryEditing} updated`, false);
    } else {
      const saved = await api("/api/learning", { method: "POST", body: JSON.stringify(body) });
      const note = (saved && saved.note) || {};
      addNote(
        note.duplicate_of
          ? `lesson merged into #${note.duplicate_of} (${note.status})`
          : `lesson #${note.id || "?"} saved as ${note.status || "proposed"}`,
        false
      );
    }
    memoryEditing = 0;
    memoryTitle.value = "";
    memoryBody.value = "";
    if (memoryReview) {
      memoryReview.checked = false;
      memoryReview.disabled = false;
    }
    memorySubmitLabel("save");
    await loadMemory();
  } catch (error) {
    addNote(`lesson save failed: ${(error && error.message) || error}`, true);
  }
}

async function reviewMemory(noteId, status) {
  try {
    await api(`/api/learning/${noteId}/review`, {
      method: "POST",
      body: JSON.stringify({ status, reviewer: "operator" }),
    });
    addNote(`lesson #${noteId} marked ${status}`, false);
    await loadMemory();
  } catch (error) {
    addNote(`review failed: ${(error && error.message) || error}`, true);
  }
}

async function showMemoryRelated(noteId) {
  try {
    const payload = await api(`/api/learning/${noteId}/related?limit=5`);
    const notes = (payload && payload.notes) || [];
    const text = notes.length
      ? notes.map((note) => `#${note.id} [${note.status}/${note.category}] ${note.title}`).join("\n")
      : "no related lessons";
    addNote(`related to lesson #${noteId}:\n${text}`, false);
  } catch (error) {
    addNote(`related lessons failed: ${(error && error.message) || error}`, true);
  }
}

async function deleteMemoryNote(noteId) {
  if (!window.confirm(`Delete lesson #${noteId}?`)) return;
  try {
    await api(`/api/learning/${noteId}`, { method: "DELETE" });
    await loadMemory();
  } catch (error) {
    addNote(`lesson delete failed: ${(error && error.message) || error}`, true);
  }
}

/* ---------------- agent skills ---------------- */
const skillList = $("#skillList");
const skillsRefresh = $("#skillsRefresh");

function renderSkills(payload) {
  const skills = (payload && payload.skills) || [];
  skillList.textContent = "";
  if (!skills.length) {
    skillList.append(el("li", "none", "no skills yet — agents create them with skill_create"));
    return;
  }
  for (const skill of skills) {
    const row = el("li");
    const head = el("div", "skill-head");
    head.append(el("span", "skill-name", skill.name || skill.slug));
    head.append(
      el(
        "span",
        "skill-meta",
        `${skill.language} · ${skill.runs || 0} runs · ${skill.last_ok ? "ok" : "untested"}`
      )
    );
    const actions = el("div", "skill-actions");
    const run = el("button", "ghost mini", "run");
    run.type = "button";
    run.addEventListener("click", () => runSkillFromPanel(skill.slug));
    const remove = el("button", "ghost mini danger", "delete");
    remove.type = "button";
    remove.addEventListener("click", () => deleteSkillFromPanel(skill.slug));
    actions.append(run, remove);
    row.append(
      head,
      el("p", "skill-desc", String(skill.description || "").slice(0, 180)),
      actions
    );
    skillList.append(row);
  }
}

async function loadSkills() {
  try {
    renderSkills(await api("/api/skills?limit=100"));
  } catch (_error) {
    skillList.textContent = "";
    skillList.append(el("li", "none", "skills unavailable"));
  }
}

async function runSkillFromPanel(name, presetArgs) {
  let args = presetArgs;
  if (args === undefined) {
    args = window.prompt(`Arguments for ${name} (optional):`, "");
    if (args === null) return;
  }
  setTicker(`running skill ${name}…`);
  try {
    const outcome = await api(`/api/skills/${encodeURIComponent(name)}/run`, {
      method: "POST",
      body: JSON.stringify({ args: args || "", timeout_s: 300 }),
    });
    systemTurn(
      `### skill \`${name}\` ${outcome.ok ? "ok" : "failed"}\n\n\`\`\`\n${String(
        outcome.content || ""
      ).slice(0, 6_000)}\n\`\`\``
    );
    setTicker(`skill ${name} ${outcome.ok ? "ok" : "failed"}`);
    await loadSkills();
  } catch (error) {
    addNote(`skill run failed: ${(error && error.message) || error}`, true);
  }
}

async function deleteSkillFromPanel(name) {
  if (!window.confirm(`Delete skill "${name}" from the registry and the sandbox?`)) return;
  try {
    await api(`/api/skills/${encodeURIComponent(name)}`, { method: "DELETE" });
    await loadSkills();
  } catch (error) {
    addNote(`skill delete failed: ${(error && error.message) || error}`, true);
  }
}

/* ---------------- swarm fleet ---------------- */
const SWARM_CATEGORIES = ["recon", "web", "exploit", "coder", "reporter"];
const SWARM_LIVE = new Set(["queued", "running", "synthesising"]);
const SWARM_SETTLED = new Set(["done", "error", "cancelled", "skipped"]);
const swarmState = { runId: null, lastSeq: 0, controller: null, timer: 0, defaults: null, status: null };


const swarmForm = $("#swarmForm");
const swarmObjective = $("#swarmObjective");
const swarmFleet = $("#swarmFleet");
const swarmCategories = $("#swarmCategories");
const swarmLanes = $("#swarmLanes");
const swarmRounds = $("#swarmRounds");
const swarmUntilComplete = $("#swarmUntilComplete");
const swarmRuns = $("#swarmRuns");
const swarmRefresh = $("#swarmRefresh");
const swarmPanel = $("#swarmPanel");
const swarmTitle = $("#swarmTitle");
const swarmMeta = $("#swarmMeta");
const swarmObjectiveView = $("#swarmObjectiveView");
const swarmLaneGrid = $("#swarmLanesView");
const swarmLog = $("#swarmLog");
const swarmSynthesis = $("#swarmSynthesis");
const swarmStop = $("#swarmStop");
const swarmClose = $("#swarmClose");
const swarmNote = $("#swarmNote");
const swarmNoteInput = $("#swarmNoteInput");

/* ---------------- workspace: upload to / download from the VM ---------------- */
const uploadState = { batch: "", busy: false, root: "/work", dest: "/work/uploads", paths: [] };

const dropzone = $("#dropzone");
const fileInput = $("#fileInput");
const folderInput = $("#folderInput");
const uploadQueue = $("#uploadQueue");
const workspaceFiles = $("#workspaceFiles");
const workspaceHint = $("#workspaceHint");
const workspaceRefresh = $("#workspaceRefresh");
const workspaceDownloadAll = $("#workspaceDownloadAll");
const workspaceWipe = $("#workspaceWipe");

function humanSize(bytes) {
  let value = Number(bytes || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  for (let index = 0; index < units.length; index += 1) {
    if (value < 1024 || index === units.length - 1) {
      return index === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[index]}`;
    }
    value /= 1024;
  }
  return `${Math.round(value)} B`;
}

function setWorkspaceHint(text, isError, paths) {
  workspaceHint.textContent = text || "";
  workspaceHint.classList.toggle("error", Boolean(isError));
  if (paths && paths.length) {
    const button = el("button", "ghost mini", "reference");
    button.type = "button";
    button.title = "put these paths in the message box";
    button.style.marginLeft = "6px";
    button.addEventListener("click", () => {
      const block =
        `Files uploaded to ${uploadState.dest}:\n` +
        paths.slice(0, 60).map((path) => `- ${path}`).join("\n");
      const current = messageBox.value.trim();
      messageBox.value = current ? `${current}\n\n${block}` : block;
      messageBox.focus();
    });
    workspaceHint.append(" ", button);
  }
}

function renderWorkspace(payload) {
  workspaceFiles.textContent = "";
  const entries = ((payload && payload.entries) || []).filter((entry) => entry.path !== ".");
  if (payload && payload.root) {
    uploadState.root = payload.root;
  }
  if (!entries.length) {
    workspaceFiles.append(el("li", "none", "no files yet"));
    return;
  }
  for (const entry of entries) {
    const row = el("li", entry.dir ? "dir" : "");
    // Right-click (and the context menu) needs the sandbox-relative path.
    row.dataset.path = entry.path;
    row.dataset.dir = entry.dir ? "1" : "0";
    const link = el("a", null, entry.dir ? `${entry.path}/` : entry.path);
    link.href = `/api/files/download?path=${encodeURIComponent(`${uploadState.root}/${entry.path}`)}`;
    link.title = entry.dir ? `download ${entry.path} as a zip` : `download ${entry.path}`;
    if (entry.dir) {
      row.append(link, el("span", "size", "zip"));
    } else {
      link.download = entry.name;
      row.append(link, el("span", "size", entry.human_size || humanSize(entry.size)));
    }
    workspaceFiles.append(row);
  }
}

async function loadWorkspace() {
  try {
    const payload = await api("/api/files?limit=400");
    renderWorkspace(payload);
    if (payload && payload.truncated) {
      setWorkspaceHint(`showing the first ${payload.count} entries - the tree is larger`, false);
    }
  } catch (error) {
    workspaceFiles.textContent = "";
    workspaceFiles.append(el("li", "none", "workspace unavailable"));
    setTicker(String((error && error.message) || error));
  }
}

async function uploadOne(item, batch) {
  const file = item.file;
  const rel = item.rel || file.webkitRelativePath || file.name;
  const query = new URLSearchParams({ name: file.name, rel, batch });
  const response = await fetch(`/api/files/upload?${query.toString()}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ? JSON.stringify(body.detail) : detail;
    } catch (_error) {
      /* keep statusText */
    }
    throw new Error(`${response.status} ${detail}`);
  }
  return response.json();
}

async function uploadItems(items) {
  const list = (items || []).filter((item) => item && item.file);
  if (!list.length || uploadState.busy) return null;
  uploadState.busy = true;
  let finish = null;
  const stamp = new Date().toISOString().replace(/\D/g, "").slice(0, 14);
  const batch = `${stamp}-${Math.random().toString(36).slice(2, 8)}`;
  uploadState.batch = batch;
  uploadQueue.textContent = "";
  let done = 0;
  let failed = 0;
  setTicker(`uploading ${list.length} file${list.length === 1 ? "" : "s"}…`);
  for (const item of list) {
    const label = item.rel || item.file.webkitRelativePath || item.file.name;
    const row = el("li");
    row.dataset.state = "active";
    row.append(el("span", null, label));
    uploadQueue.append(row);
    try {
      await uploadOne(item, batch);
      row.dataset.state = "done";
      row.append(el("span", "size", humanSize(item.file.size)));
      done += 1;
      setTicker(`uploaded ${done}/${list.length}`);
    } catch (error) {
      row.dataset.state = "error";
      row.append(el("span", "size", "failed"));
      failed += 1;
      addNote(`upload ${label}: ${(error && error.message) || error}`, true);
    }
  }
  try {
    if (done > 0) {
      // The route is /api/files/finish; /api/files/upload/finish used to 404 and
      // silently left the upload in host staging instead of the sandbox.
      finish = await api(`/api/files/finish?batch=${encodeURIComponent(batch)}`, {
        method: "POST",
      });
      uploadState.dest = finish.dest || uploadState.dest;
      uploadState.paths = Array.isArray(finish.paths) ? finish.paths : [];
      setWorkspaceHint(
        `uploaded ${finish.files} file(s) · ${finish.human_bytes || ""} → ${finish.dest}`,
        false,
        uploadState.paths
      );
      setTicker(`upload ready at ${finish.dest}`);
    }
  } catch (error) {
    setWorkspaceHint(`upload publish failed: ${(error && error.message) || error}`, true);
    addNote(`upload publish failed: ${(error && error.message) || error}`, true);
  } finally {
    uploadState.busy = false;
    uploadState.batch = "";
    await loadWorkspace();
  }
  if (failed) setTicker(`${done} uploaded · ${failed} failed`);
  return finish;
}

function filesToItems(fileList) {
  return [...(fileList || [])].map((file) => ({
    file,
    rel: file.webkitRelativePath || file.name,
  }));
}

function readDroppedEntry(entry, prefix = "") {
  return new Promise((resolve) => {
    if (!entry) {
      resolve([]);
      return;
    }
    if (entry.isFile) {
      entry.file(
        (file) => resolve([{ file, rel: `${prefix}${file.name}` }]),
        () => resolve([])
      );
      return;
    }
    if (!entry.isDirectory) {
      resolve([]);
      return;
    }
    const reader = entry.createReader();
    const children = [];
    const readNext = () => {
      reader.readEntries(async (batch) => {
        if (!batch.length) {
          const nested = await Promise.all(
            children.map((child) => readDroppedEntry(child, `${prefix}${entry.name}/`))
          );
          resolve(nested.flat());
          return;
        }
        children.push(...batch);
        readNext();
      }, () => resolve([]));
    };
    readNext();
  });
}

async function handleWorkspaceDrop(event) {
  event.preventDefault();
  dropzone.classList.remove("drag");
  const transfer = event.dataTransfer;
  if (!transfer) return;
  const entries = [...(transfer.items || [])]
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (entries.length) {
    const nested = await Promise.all(entries.map((entry) => readDroppedEntry(entry)));
    const items = nested.flat();
    if (items.length) {
      await uploadItems(items);
      return;
    }
  }
  if (transfer.files && transfer.files.length) await uploadItems(filesToItems(transfer.files));
}

function downloadWorkspace() {
  window.location.href = `/api/files/download?path=${encodeURIComponent(uploadState.root)}`;
}

function workspaceAbsolute(rel) {
  const root = String(uploadState.root || "/work").replace(/\/+$/, "");
  const clean = String(rel || "").replace(/^\/+/, "");
  return `${root}/${clean}`;
}

async function deleteWorkspacePath(rel, isDir) {
  const what = isDir ? "folder" : "file";
  if (!window.confirm(`Delete ${what} "${rel}" from the sandbox? This cannot be undone.`)) return false;
  try {
    await api(`/api/files?path=${encodeURIComponent(workspaceAbsolute(rel))}`, { method: "DELETE" });
    setTicker(`deleted ${rel}`);
    addNote(`deleted ${what} ${rel}`, false);
    await loadWorkspace();
    return true;
  } catch (error) {
    addNote(`delete failed for ${rel}: ${(error && error.message) || error}`, true);
    return false;
  }
}

async function wipeWorkspace() {
  const ok = window.confirm(
    "Delete EVERY file and folder inside the sandbox workspace?\n\n" +
      "Uploads, agent output and lane terminals are removed. This cannot be undone."
  );
  if (!ok) return false;
  try {
    setTicker("wiping the workspace…");
    const out = await api("/api/files/clear", { method: "POST" });
    addNote(`workspace wiped: ${out.root || "/work"} is empty (${out.remaining || 0} entries left)`, false);
    setTicker("workspace wiped");
    await loadWorkspace();
    return true;
  } catch (error) {
    addNote(`workspace wipe failed: ${(error && error.message) || error}`, true);
    return false;
  }
}

/* ---------------- right-click menu on workspace entries ---------------- */
const ctxMenu = $("#ctxMenu");

function hideCtxMenu() {
  if (!ctxMenu) return;
  ctxMenu.hidden = true;
  delete ctxMenu.dataset.path;
  delete ctxMenu.dataset.dir;
}

function showCtxMenu(event, row) {
  if (!ctxMenu) return;
  ctxMenu.dataset.path = row.dataset.path || "";
  ctxMenu.dataset.dir = row.dataset.dir || "0";
  const label = ctxMenu.querySelector('button[data-action="download"]');
  if (label) label.textContent = row.dataset.dir === "1" ? "download (zip)" : "download";
  ctxMenu.hidden = false;
  const rect = ctxMenu.getBoundingClientRect();
  const left = Math.min(event.clientX, window.innerWidth - rect.width - 8);
  const top = Math.min(event.clientY, window.innerHeight - rect.height - 8);
  ctxMenu.style.left = `${Math.max(8, left)}px`;
  ctxMenu.style.top = `${Math.max(8, top)}px`;
}

async function onCtxAction(action) {
  const rel = ctxMenu.dataset.path || "";
  const isDir = ctxMenu.dataset.dir === "1";
  hideCtxMenu();
  if (!rel) return;
  if (action === "download") {
    window.location.href = `/api/files/download?path=${encodeURIComponent(workspaceAbsolute(rel))}`;
    return;
  }
  if (action === "reference") {
    const block = `Workspace path: ${workspaceAbsolute(rel)}`;
    const current = messageBox.value.trim();
    messageBox.value = current ? `${current}\n\n${block}` : block;
    messageBox.focus();
    return;
  }
  if (action === "delete") await deleteWorkspacePath(rel, isDir);
}

/* ---------------- chat helpers ---------------- */
function systemTurn(text) {
  const turn = el("div", "turn");
  const card = el("div", "system-card");
  card.innerHTML = markdown(String(text || ""));
  turn.append(card);
  transcript.append(turn);
  showEmpty(false);
  scrollDown();
  return card;
}

async function attachForChat(items) {
  const list = (items || []).filter((item) => item && item.file);
  if (!list.length) return;
  setTicker(`uploading ${list.length} file(s) for the chat…`);
  const finish = await uploadItems(list);
  if (!finish || !finish.dest) return;
  const paths = (finish.paths || []).slice(0, 60);
  const block =
    `Files uploaded to ${finish.dest} (${finish.files} file(s), ${finish.human_bytes || ""}):\n` +
    paths.map((path) => `- ${path}`).join("\n");
  const current = messageBox.value.trim();
  messageBox.value = current ? `${current}\n\n${block}` : block;
  messageBox.focus();
  addNote(`uploaded ${finish.files} file(s) → ${finish.dest}`, false);
}

async function appendArtifacts(node) {
  if (!node || !node.isConnected) return;
  try {
    const payload = await api("/api/files?limit=80");
    const files = ((payload && payload.entries) || []).filter((entry) => !entry.dir).slice(0, 8);
    if (!files.length) return;
    const box = el("div", "artifacts");
    box.append(el("span", "artifacts-label", "download from the agents: "));
    for (const file of files) {
      const link = el("a", "artifact", file.path);
      link.href = `/api/files/download?path=${encodeURIComponent(workspaceAbsolute(file.path))}`;
      link.download = file.name;
      link.title = `${file.human_size || humanSize(file.size)} · click to download`;
      box.append(link);
    }
    node.append(box);
    scrollDown();
  } catch (_error) {
    /* the workspace panel already reports sandbox problems */
  }
}

async function handleChatDrop(event) {
  const transfer = event.dataTransfer;
  if (!transfer) return;
  event.preventDefault();
  const entries = [...(transfer.items || [])]
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (entries.length) {
    const nested = await Promise.all(entries.map((entry) => readDroppedEntry(entry)));
    const items = nested.flat();
    if (items.length) {
      await attachForChat(items);
      return;
    }
  }
  if (transfer.files && transfer.files.length) await attachForChat(filesToItems(transfer.files));
}

function chosenCategories() {
  const chips = [...swarmCategories.querySelectorAll("button[data-category]")];
  const picked = chips
    .filter((chip) => chip.dataset.on === "1")
    .map((chip) => chip.dataset.category);
  return picked.length ? picked : ["recon"];
}

function renderSwarmChips() {
  // Every role is always on the board.  This row used to be filtered by the
  // server's `defaults.categories`, so an AV2_SWARM_CATEGORIES=recon,web
  // environment silently deleted the exploit (and coder/reporter) chips from
  // the dashboard.  The server defaults only decide what a categories-less
  // API call runs; the operator always gets the full roster to choose from.
  swarmCategories.textContent = "";
  for (const name of SWARM_CATEGORIES) {
    const chip = el("button", "chip on", name);
    chip.type = "button";
    chip.dataset.category = name;
    chip.dataset.on = "1";
    chip.addEventListener("click", () => {
      chip.dataset.on = chip.dataset.on === "1" ? "0" : "1";
      chip.classList.toggle("on", chip.dataset.on === "1");
    });
    swarmCategories.append(chip);
  }
}

function fillSelect(select, values, chosen) {
  select.textContent = "";
  for (const value of values) {
    const option = el("option", null, String(value));
    option.value = String(value);
    select.append(option);
  }
  if (chosen !== undefined && chosen !== null) select.value = String(chosen);
}

function renderSwarmRuns(items) {
  swarmRuns.textContent = "";
  if (!items || !items.length) {
    swarmRuns.append(el("li", "none", "no fleets yet"));
    return;
  }
  for (const run of items) {
    const row = el("li");
    const open = el("button", "load");
    open.type = "button";
    open.classList.toggle("active", run.run_id === swarmState.runId);
    const line = el("div", "line");
    const state = el("span", "state", run.status);
    state.dataset.status = run.status;
    line.append(el("span", "sid", run.run_id), state);
    const ledger = run.ledger || {};
    const facts = [
      (run.categories || []).join("+"),
      `${run.lanes} lanes`,
      `round ${run.round}/${run.rounds}${run.until_complete ? " complete-mode" : ""}`,
      `${ledger.duplicates_suppressed || 0} dup`,
    ].join(" · ");
    open.append(line, el("span", "prev", run.objective || ""), el("span", "facts", facts));
    open.title = `${run.objective || ""}\n${run.session_id || ""}`;
    open.addEventListener("click", () => openSwarm(run.run_id));
    row.append(open);
    swarmRuns.append(row);
  }
}

async function loadSwarm() {
  // The roster is static: build it before the network call so a swarm API that
  // is slow or down can never leave the operator without category chips.
  if (!swarmCategories.children.length) renderSwarmChips();
  try {
    const payload = await api("/api/swarm?limit=12");
    swarmState.defaults = payload.defaults || swarmState.defaults;
    const defaults = swarmState.defaults || {};
    if (!swarmLanes.options.length) {
      fillSelect(swarmLanes, [1, 2, 3, 4, 5], Math.min(5, Number(defaults.fleet_groups || 1)));
      swarmRounds.value = String(
        Math.min(50, Math.max(1, Number(defaults.fleet_rounds || defaults.rounds || 2)))
      );
    }
    renderSwarmRuns(payload.runs || []);
  } catch (error) {
    swarmRuns.textContent = "";
    swarmRuns.append(el("li", "none", "swarm unavailable"));
    setTicker(String(error.message || error));
  }
}

async function startSwarm() {
  const objective = swarmObjective.value.trim();
  if (objective.length < 4) {
    setTicker("objective needs at least 4 characters");
    return;
  }
  setTicker("launching fleet…");
  try {
    const payload = await api("/api/swarm", {
      method: "POST",
      body: JSON.stringify({
        objective,
        session_id: state.sessionId || null,
        fleet: swarmFleet ? swarmFleet.checked : false,
        categories: chosenCategories(),
        lanes: Number(swarmLanes.value || 1),
        groups: Number(swarmLanes.value || 0) || undefined,
        rounds: Number(swarmRounds.value || 1),
        until_complete: Boolean(swarmUntilComplete && swarmUntilComplete.checked),
        unlimited: Boolean(swarmUntilComplete && swarmUntilComplete.checked),
      }),
    });
    swarmObjective.value = "";
    setTicker(`fleet ${payload.run_id} queued`);
    await loadSwarm();
    await openSwarm(payload.run_id);
  } catch (error) {
    setTicker(String(error.message || error));
  }
}

function laneChip(lane) {
  const chip = el("div", "lane");
  chip.dataset.state = lane.state || "idle";
  const bits = [lane.state || "idle"];
  if (lane.tool_calls !== undefined) bits.push(`${lane.tool_calls} tools`);
  if (lane.duplicates) bits.push(`${lane.duplicates} dup`);
  if (lane.findings) bits.push(`${lane.findings} find`);
  if (lane.enforced) bits.push(`${lane.enforced} pushed`);
  if (lane.terminated) bits.push("terminated");
  if (lane.tokens) bits.push(`${lane.tokens} tok`);
  const model = String(lane.model || "").split("/").pop();
  if (model) bits.push(model);
  chip.append(el("b", null, lane.label || lane.lane_id), el("small", null, bits.join(" · ")));
  chip.title = [
    lane.workdir,
    lane.summary,
    lane.error,
    lane.reason ? `completion: ${lane.reason}` : "",
    Array.isArray(lane.models_used) && lane.models_used.length > 1
      ? `models: ${lane.models_used.join(" -> ")}`
      : "",
  ]
    .filter(Boolean)
    .join("\n\n");
  return chip;
}

function renderSwarmStatus(status) {
  swarmState.status = status;
  if (!status) return;
  swarmTitle.textContent = `fleet ${status.run_id}`;
  const ledger = status.ledger || {};
  const budget = status.budget || {};
  const board = Object.values(status.memory || {}).reduce(
    (sum, value) => sum + Number(value || 0),
    0
  );
  const lanes = status.lanes || [];
  const settled = lanes.filter((lane) => SWARM_SETTLED.has(lane.state)).length;
  const findings = Number((status.memory && status.memory.finding) || 0);
  const pushes = Number((status.enforcement && status.enforcement.pushes) || 0);
  const learned = Number((status.knowledge && status.knowledge.learned) || 0);
  const killed = ((status.enforcement && status.enforcement.terminated) || []).length;
  const remaining = Number(
    status.rounds_remaining === undefined ? 0 : status.rounds_remaining
  );
  const roundsLabel = status.until_complete
    ? `round ${status.round}/${status.rounds} (until complete)`
    : `round ${status.round}/${status.rounds}`;
  const facts = [
    status.status,
    roundsLabel,
    status.mission_complete ? "mission complete" : "",
    status.status === "running" && remaining ? `${remaining} round(s) left` : "",
    `${settled}/${lanes.length} lanes settled`,
    `${ledger.executed || 0} executed`,
    `${ledger.duplicates_suppressed || 0} dup suppressed`,
    `${budget.tokens_used || 0}/${budget.tokens_limit || 0} tok`,
    `${findings} finding(s)`,
    pushes ? `${pushes} enforced` : "",
    killed ? `${killed} terminated` : "",
    learned ? `${learned} learned` : "",
    status.completion && status.completion.approved === false && status.completion.reason
      ? `not proven: ${oneLine(status.completion.reason, 60)}`
      : "",
    status.stopped_reason || "",
    board ? `${board} board` : "",
  ].filter(Boolean);
  swarmMeta.textContent = facts.join(" · ");
  swarmMeta.dataset.status = status.status;
  swarmObjectiveView.textContent = status.objective || "";
  swarmLaneGrid.textContent = "";
  for (const lane of lanes) swarmLaneGrid.append(laneChip(lane));
  if (status.synthesis) {
    swarmSynthesis.innerHTML = markdown(status.synthesis);
  } else if (status.error) {
    swarmSynthesis.textContent = status.error;
  } else {
    swarmSynthesis.textContent = SWARM_LIVE.has(status.status) ? "lanes still working…" : "no synthesis yet";
  }
  swarmMeta.title = status.model
    ? `${status.model} · ${status.lanes_per_category} lanes/category`
    : "";
  swarmStop.disabled = !SWARM_LIVE.has(status.status);
}

function addSwarmLog(kind, text) {
  swarmLog.append(el("div", `line ${kind}`, text));
  while (swarmLog.childElementCount > 400) swarmLog.firstElementChild.remove();
  swarmLog.scrollTop = swarmLog.scrollHeight;
}

function stopSwarmStream() {
  if (swarmState.controller) {
    swarmState.controller.abort();
    swarmState.controller = null;
  }
}

async function refreshSwarm() {
  if (!swarmState.runId) return;
  try {
    const status = await api(`/api/swarm/${encodeURIComponent(swarmState.runId)}`);
    renderSwarmStatus(status);
    if (!SWARM_LIVE.has(status.status)) {
      stopSwarmStream();
      if (swarmState.timer) {
        window.clearInterval(swarmState.timer);
        swarmState.timer = 0;
      }
    }
  } catch (error) {
    setTicker(String(error.message || error));
  }
}

function onSwarmEvent(event) {
  if (typeof event.seq === "number" && event.seq > swarmState.lastSeq) {
    swarmState.lastSeq = event.seq;
  }
  const lane = (event.data && (event.data.label || event.data.agent || event.lane)) ? `${event.data.label || event.data.agent || event.lane} ` : "";
  const data = event.data || {};
  switch (event.type) {
    case "tool_call":
      addSwarmLog("tool", `${lane}→ ${data.name || "tool"} · ${toolCaption(data.name, data.args)}`);
      break;
    case "tool_result": {
      const failed = data.ok === false;
      const body = oneLine(data.content || data.text || "", 140);
      addSwarmLog(
        failed ? "error" : "tool",
        `${lane}${failed ? "×" : "✓"} ${data.name || "tool"}${body ? ` · ${body}` : ""}`
      );
      break;
    }
    case "agent":
      addSwarmLog("agent", `${lane}${event.text || data.agent || "agent"}`);
      break;
    case "notice":
      addSwarmLog("notice", `${lane}${event.text || ""}`);
      break;
    case "error":
      addSwarmLog("error", `${lane}${event.text || "error"}`);
      break;
    case "done":
      addSwarmLog("notice", `${lane}lane finished${data.error ? ` (${data.error})` : ""}`);
      refreshSwarm();
      break;
    default:
      break;
  }
}

async function streamSwarm(runId) {
  stopSwarmStream();
  const controller = new AbortController();
  swarmState.controller = controller;
  try {
    const response = await fetch(
      `/api/swarm/${encodeURIComponent(runId)}/events?after=0`,
      { headers: { Accept: "text/event-stream" }, signal: controller.signal }
    );
    if (!response.ok || !response.body) throw new Error(`events ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        for (const line of frame.split(/\r?\n/)) {
          if (!line.startsWith("data:")) continue;
          const raw = line.slice(5).trim();
          if (!raw) continue;
          let event;
          try {
            event = JSON.parse(raw);
          } catch (_error) {
            continue;
          }
          if (event && typeof event.type === "string") onSwarmEvent(event);
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch (error) {
    if (!error || error.name !== "AbortError") {
      addSwarmLog("error", `stream: ${(error && error.message) || error}`);
    }
  } finally {
    if (swarmState.controller === controller) swarmState.controller = null;
  }
}

async function openSwarm(runId) {
  stopSwarmStream();
  swarmState.runId = runId;
  swarmState.lastSeq = 0;
  swarmLog.textContent = "";
  swarmSynthesis.textContent = "";
  swarmPanel.hidden = false;
  addSwarmLog("notice", `attached to ${runId}`);
  await refreshSwarm();
  await loadSwarm();
  streamSwarm(runId);
  if (swarmState.timer) window.clearInterval(swarmState.timer);
  swarmState.timer = window.setInterval(refreshSwarm, 5_000);
}

async function stopSwarm() {
  if (!swarmState.runId) return;
  swarmStop.disabled = true;
  try {
    await api(`/api/swarm/${encodeURIComponent(swarmState.runId)}/stop?cancel=true`, {
      method: "POST",
    });
    addSwarmLog("notice", "stop requested — lanes cancelled");
    await refreshSwarm();
  } catch (error) {
    swarmStop.disabled = false;
    setTicker(String(error.message || error));
  }
}

async function postSwarmNote() {
  const text = swarmNoteInput.value.trim();
  if (!text || !swarmState.runId) return;
  try {
    await api(`/api/swarm/${encodeURIComponent(swarmState.runId)}/message`, {
      method: "POST",
      body: JSON.stringify({ message: text }),
    });
    swarmNoteInput.value = "";
    addSwarmLog("notice", `operator → board: ${text}`);
  } catch (error) {
    setTicker(String(error.message || error));
  }
}

function closeSwarm() {
  stopSwarmStream();
  if (swarmState.timer) {
    window.clearInterval(swarmState.timer);
    swarmState.timer = 0;
  }
  swarmState.runId = null;
  swarmPanel.hidden = true;
  loadSwarm();
}

/* ---------------- sidebar layout (drag to resize / collapse) ---------------- */
function clampWidth(value) {
  const ceiling = Math.max(320, Math.min(760, window.innerWidth - 460));
  return Math.max(240, Math.min(Math.round(value), ceiling));
}

function applySidebarWidth(width) {
  const value = clampWidth(width);
  document.documentElement.style.setProperty("--sidebar-w", `${value}px`);
  localStorage.setItem("av2.sidebarWidth", String(value));
  return value;
}

function initSidebar() {
  const stored = Number(localStorage.getItem("av2.sidebarWidth") || 0);
  if (stored > 0) applySidebarWidth(stored);
  else if (window.innerWidth >= 1500) applySidebarWidth(360);

  let dragging = false;
  sidebarResizer.addEventListener("pointerdown", (event) => {
    dragging = true;
    document.body.classList.add("resizing");
    sidebarResizer.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  sidebarResizer.addEventListener("pointermove", (event) => {
    if (dragging) applySidebarWidth(event.clientX);
  });
  const stopDrag = (event) => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("resizing");
    try {
      sidebarResizer.releasePointerCapture(event.pointerId);
    } catch (_error) {
      /* pointer already released */
    }
  };
  sidebarResizer.addEventListener("pointerup", stopDrag);
  sidebarResizer.addEventListener("pointercancel", stopDrag);
  sidebarResizer.addEventListener("dblclick", () => applySidebarWidth(320));
  sidebarResizer.addEventListener("keydown", (event) => {
    const current = Number(
      getComputedStyle(document.documentElement).getPropertyValue("--sidebar-w").replace("px", "")
    ) || 320;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      applySidebarWidth(current - 16);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      applySidebarWidth(current + 16);
    }
  });

  const collapsed = localStorage.getItem("av2.sidebarCollapsed") === "1";
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  sidebarToggle.addEventListener("click", () => {
    const next = !document.body.classList.contains("sidebar-collapsed");
    document.body.classList.toggle("sidebar-collapsed", next);
    localStorage.setItem("av2.sidebarCollapsed", next ? "1" : "0");
  });
}

/* ---------------- wiring ---------------- */
function wire() {
  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage();
  });

  messageBox.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });

  agentSelect.addEventListener("change", () => chooseAgent(agentSelect.value));

  // "until done" removes the round cap entirely; the field shows the server
  // ceiling so a complex task is never cut off at the usual 2 rounds.
  untilComplete.addEventListener("change", () => {
    const current = Number(roundsInput.value || 0);
    if (untilComplete.checked && current < 50) roundsInput.value = "50";
    else if (!untilComplete.checked && current === 50) roundsInput.value = "2";
  });
  if (swarmUntilComplete) {
    swarmUntilComplete.addEventListener("change", () => {
      const current = Number(swarmRounds.value || 0);
      if (swarmUntilComplete.checked && current < 50) swarmRounds.value = "50";
      else if (!swarmUntilComplete.checked && current === 50) swarmRounds.value = "2";
    });
  }

  stopButton.addEventListener("click", () => {
    if (state.controller) state.controller.abort();
  });

  targetForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = targetInput.value.trim();
    if (!value) return;
    try {
      await api("/api/scope", {
        method: "POST",
        body: JSON.stringify({ value, mode: modeSelect.value || "exploit" }),
      });
      targetInput.value = "";
      await loadScope();
    } catch (error) {
      setTicker(String(error.message || error));
    }
  });

  clearTargets.addEventListener("click", async () => {
    try {
      await api("/api/scope", { method: "DELETE" });
      await loadScope();
    } catch (error) {
      setTicker(String(error.message || error));
    }
  });

  newSessionButton.addEventListener("click", () => startNewSession());

  fileInput.addEventListener("change", () => {
    uploadItems(filesToItems(fileInput.files));
    fileInput.value = "";
  });
  folderInput.addEventListener("change", () => {
    uploadItems(filesToItems(folderInput.files));
    folderInput.value = "";
  });
  for (const type of ["dragenter", "dragover"]) {
    dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      dropzone.classList.add("drag");
    });
  }
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
  dropzone.addEventListener("drop", handleWorkspaceDrop);
  workspaceRefresh.addEventListener("click", () => loadWorkspace());
  workspaceDownloadAll.addEventListener("click", () => downloadWorkspace());
  if (workspaceWipe) workspaceWipe.addEventListener("click", () => wipeWorkspace());

  // Right-click any workspace entry for a single delete / download / reference.
  workspaceFiles.addEventListener("contextmenu", (event) => {
    const row = event.target.closest("li[data-path]");
    if (!row) return;
    event.preventDefault();
    showCtxMenu(event, row);
  });
  if (ctxMenu) {
    ctxMenu.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      if (button) onCtxAction(button.dataset.action);
    });
    document.addEventListener("click", (event) => {
      if (!ctxMenu.hidden && !ctxMenu.contains(event.target)) hideCtxMenu();
    });
    window.addEventListener("blur", hideCtxMenu);
  }

  // Upload straight from the chat box: files or whole folders land in the
  // sandbox and their paths are written into the message.
  if (chatFileInput) {
    chatFileInput.addEventListener("change", () => {
      attachForChat(filesToItems(chatFileInput.files));
      chatFileInput.value = "";
    });
  }
  if (chatFolderInput) {
    chatFolderInput.addEventListener("change", () => {
      attachForChat(filesToItems(chatFolderInput.files));
      chatFolderInput.value = "";
    });
  }
  for (const zone of [composer, transcript]) {
    zone.addEventListener("dragover", (event) => event.preventDefault());
    zone.addEventListener("drop", handleChatDrop);
  }

  if (memoryForm) {
    memoryForm.addEventListener("submit", (event) => {
      event.preventDefault();
      saveMemory();
    });
  }
  if (memoryRefresh) memoryRefresh.addEventListener("click", () => loadMemory());
  if (memoryQuery) {
    memoryQuery.addEventListener("input", () => {
      window.clearTimeout(memoryQuery._timer);
      memoryQuery._timer = window.setTimeout(loadMemory, 250);
    });
  }
  for (const select of [
    memoryFilterCategory,
    memoryFilterRole,
    memoryFilterStatus,
    memoryFilterResult,
  ]) {
    if (select) select.addEventListener("change", () => loadMemory());
  }
  if (skillsRefresh) skillsRefresh.addEventListener("click", () => loadSkills());

  if (providerForm) {
    providerForm.addEventListener("submit", (event) => {
      event.preventDefault();
      saveProvider();
    });
  }
  if (providerRefresh) providerRefresh.addEventListener("click", () => loadProvider());

  swarmForm.addEventListener("submit", (event) => {
    event.preventDefault();
    startSwarm();
  });

  swarmObjective.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      startSwarm();
    }
  });

  swarmRefresh.addEventListener("click", () => loadSwarm());
  swarmStop.addEventListener("click", () => stopSwarm());
  swarmClose.addEventListener("click", () => closeSwarm());
  swarmNote.addEventListener("submit", (event) => {
    event.preventDefault();
    postSwarmNote();
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      hideCtxMenu();
      if (!swarmPanel.hidden) closeSwarm();
    }
  });
}

async function init() {
  initSidebar();
  wire();
  resetTranscript();
  // How many squads of five work one task is the operator's choice.
  fillSelect(agentsSelect, [1, 2, 3, 4, 5], 1);
  const stored = localStorage.getItem(SESSION_KEY);
  await Promise.all([
    refreshHealth(),
    loadAgents(),
    loadScope(),
    loadSessions(),
    loadSwarm(),
    loadProvider(),
    loadWorkspace(),
    loadMemory(),
    loadSkills(),
  ]);
  if (stored) await openSession(stored);
  window.setInterval(refreshHealth, 25_000);
  window.setInterval(loadWorkspace, 30_000);
  window.setInterval(loadMemory, 45_000);
  messageBox.focus();
}

init();
