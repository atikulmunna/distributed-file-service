// File Service UI. Plain JavaScript, no build step (D4).
// Rules: the server is the source of truth (chunk colors, statuses and history come from the API);
// every number says where it comes from; dynamic text is inserted with textContent or DOM nodes only.

const $ = (id) => document.getElementById(id);

const MAX_CELLS = 1000; // above this, one cell stands for a group of chunks
const WHOLE_FILE_HASH_LIMIT = 256 * 1024 * 1024; // hashed in memory by this page
const THROUGHPUT_WINDOW_MS = 10_000;

const state = {
  apiKey: "",
  limits: null,
  file: null,
  plan: null,
  job: null,
  uploads: new Map(),
  uploadsCursor: null,
  selectedUploadId: null,
  statsAt: null,
};

// ---------------------------------------------------------------- formatting

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  const text = Number.isInteger(value) ? String(value) : value.toFixed(1);
  return `${text} ${units[unit]}`;
}

function formatCount(value, singular, plural = `${singular}s`) {
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}

function clockTime(date) {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function dateTime(iso) {
  return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" });
}

function ago(iso) {
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

function uptime(seconds) {
  if (seconds < 120) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 120) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function shortId(id) {
  return id.slice(0, 8);
}

function hex(buffer) {
  return Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function randomId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16)); // available outside secure contexts too
  return hex(bytes.buffer);
}

// ---------------------------------------------------------------- DOM helpers

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "className") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function show(node, visible) {
  node.hidden = !visible;
}

// ---------------------------------------------------------------- events and the request log

function addEvent(text, tone = "") {
  const list = $("events");
  const now = new Date();
  list.prepend(el("li", { dataset: { tone } }, el("time", { datetime: now.toISOString(), text: clockTime(now) }), el("span", { text })));
  while (list.children.length > 60) list.lastElementChild.remove();
}

function describeFailure(error) {
  // What failed, the server's reason, and the request ID to look up in the server logs.
  const parts = [error.detail];
  if (error.status) parts.push(`HTTP ${error.status}`);
  if (error.requestId) parts.push(`request ${error.requestId}`);
  return parts.join(", ");
}

function logRequest(method, path, status, ms, requestId) {
  const body = $("request-log");
  const loggedPath = path.startsWith("/v1/downloads/") ? "/v1/downloads/{token}" : path;
  body.prepend(
    el(
      "tr",
      {},
      el("td", { text: clockTime(new Date()) }),
      el("td", { text: method }),
      el("td", { className: "ident", text: loggedPath }),
      el("td", { className: "num", text: String(status) }),
      el("td", { className: "num", text: String(Math.round(ms)) }),
      el("td", { className: "ident", text: requestId || "none" }),
    ),
  );
  while (body.children.length > 100) body.lastElementChild.remove();
}

// ---------------------------------------------------------------- API client

class ApiError extends Error {
  constructor({ status, detail, errorCode, requestId, retryAfter }) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.errorCode = errorCode || "";
    this.requestId = requestId || "";
    this.retryAfter = retryAfter;
  }
}

async function api(method, path, { body, headers = {}, auth = true, allowError = false } = {}) {
  const init = { method, headers: { ...headers } };
  if (auth && state.apiKey) init.headers["X-API-Key"] = state.apiKey;
  if (body instanceof ArrayBuffer) {
    init.body = body;
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const started = performance.now();
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    logRequest(method, path, "network error", performance.now() - started, "");
    throw new ApiError({ status: 0, detail: "the server could not be reached" });
  }
  const requestId = response.headers.get("X-Request-ID") || "";
  logRequest(method, path, response.status, performance.now() - started, requestId);
  let payload = null;
  if ((response.headers.get("content-type") || "").includes("application/json")) {
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
  }
  if (!response.ok && !allowError) {
    throw new ApiError({
      status: response.status,
      detail: (payload && payload.detail) || response.statusText || "request failed",
      errorCode: payload && payload.error_code,
      requestId,
      retryAfter: response.headers.get("Retry-After"),
    });
  }
  return { status: response.status, body: payload, instance: response.headers.get("X-DFS-Instance") || "" };
}

// ---------------------------------------------------------------- header: which server, is it ready

async function refreshServerFacts() {
  try {
    const ready = await api("GET", "/ready", { auth: false, allowError: true });
    const facts = ready.body || {};
    $("fact-instance").textContent = facts.instance_id || ready.instance || "unknown";
    const readiness = $("fact-ready");
    if (ready.status === 200) {
      readiness.textContent = "ready";
      readiness.dataset.state = "ready";
    } else {
      const problems = Object.entries(facts.checks || {})
        .filter(([, value]) => value !== "ok")
        .map(([name, value]) => `${name}: ${value}`);
      readiness.textContent = problems.length ? `not ready (${problems.join("; ")})` : "not ready";
      readiness.dataset.state = "down";
    }
  } catch (error) {
    $("fact-ready").textContent = `unreachable (${error.detail})`;
    $("fact-ready").dataset.state = "down";
  }
}

async function loadVersion() {
  try {
    const version = await api("GET", "/version", { auth: false });
    $("fact-version").textContent = version.body.app_version;
  } catch {
    $("fact-version").textContent = "unknown";
  }
}

// ---------------------------------------------------------------- theme

const THEMES = ["system", "light", "dark"];

function readTheme() {
  try {
    const saved = localStorage.getItem("dfs-theme");
    return THEMES.includes(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

function applyTheme(theme) {
  state.theme = theme;
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  $("theme-toggle").textContent = `Theme: ${theme}`;
  try {
    localStorage.setItem("dfs-theme", theme);
  } catch {
    // private windows may refuse storage; the choice then lasts until reload
  }
}

// ---------------------------------------------------------------- API key

async function useKey(event) {
  event.preventDefault();
  const key = $("api-key").value.trim();
  const status = $("key-status");
  if (!key) {
    status.textContent = "Enter an API key first.";
    status.dataset.state = "bad";
    return;
  }
  state.apiKey = key;
  status.textContent = "Checking the key with the server.";
  delete status.dataset.state;
  try {
    const limits = await api("GET", "/v1/limits");
    state.limits = limits.body;
    status.textContent = "Key accepted. Upload limits loaded from the server.";
    status.dataset.state = "ok";
    updatePlan();
    await loadUploads(true);
    await loadStats();
  } catch (error) {
    state.apiKey = "";
    state.limits = null;
    status.textContent = `The server refused the key: ${describeFailure(error)}.`;
    status.dataset.state = "bad";
    updatePlan();
  }
}

// ---------------------------------------------------------------- upload plan

function perChunkChecksums() {
  return Boolean(globalThis.crypto && crypto.subtle);
}

function choosePlan(file, limits, parallelism) {
  if (file.size > limits.max_file_size_bytes) {
    return { problem: `This file is ${formatBytes(file.size)}; the server accepts files up to ${formatBytes(limits.max_file_size_bytes)}.` };
  }
  const mib = 1024 * 1024;
  let chunkSize = limits.default_chunk_size_bytes;
  const smallestThatFits = Math.ceil(file.size / limits.max_total_chunks);
  if (smallestThatFits > chunkSize) chunkSize = Math.ceil(smallestThatFits / mib) * mib;
  if (limits.storage_backend_requires_min_part && file.size > chunkSize && chunkSize < limits.min_part_size_bytes) {
    chunkSize = limits.min_part_size_bytes;
  }
  chunkSize = Math.min(Math.max(chunkSize, limits.min_chunk_size_bytes), limits.max_chunk_size_bytes);
  const totalChunks = file.size === 0 ? 0 : Math.ceil(file.size / chunkSize);
  if (totalChunks > limits.max_total_chunks) {
    return { problem: `This file needs ${totalChunks.toLocaleString()} chunks; the server allows ${limits.max_total_chunks.toLocaleString()}.` };
  }
  const allowed = limits.max_inflight_chunks_per_upload;
  return { chunkSize, totalChunks, parallelism: Math.max(1, Math.min(parallelism, allowed, totalChunks || 1)) };
}

function matchingUnfinishedUpload(file) {
  // The newest listed upload of this file (same name and size) that can still receive chunks.
  for (const upload of state.uploads.values()) {
    if (upload.file_name === file.name && upload.file_size === file.size && ["INITIATED", "IN_PROGRESS"].includes(upload.status)) {
      return upload;
    }
  }
  return null;
}

function updatePlan() {
  const planNode = $("plan");
  const problemNode = $("plan-problem");
  const start = $("start-upload");
  const resume = $("resume-upload");
  const file = state.file;
  const limits = state.limits;
  state.plan = null;
  show(problemNode, false);
  show(resume, false);

  if (limits) {
    $("parallelism").max = String(limits.max_inflight_chunks_per_upload);
    $("parallelism-hint").textContent = `The server accepts up to ${limits.max_inflight_chunks_per_upload} chunks of one upload at a time.`;
  }
  const wholeBox = $("whole-checksum");
  if (!perChunkChecksums()) {
    wholeBox.disabled = true;
    wholeBox.checked = false;
    $("whole-checksum-hint").textContent = "Checksums need a secure page (HTTPS or localhost); this page is not one.";
  } else if (file && file.size > WHOLE_FILE_HASH_LIMIT) {
    wholeBox.disabled = true;
    wholeBox.checked = false;
    $("whole-checksum-hint").textContent = `This page hashes the whole file in memory, so the option is limited to files up to ${formatBytes(WHOLE_FILE_HASH_LIMIT)}.`;
  } else {
    wholeBox.disabled = false;
  }

  if (!file) {
    show(planNode, false);
    start.disabled = true;
    return;
  }
  if (!limits) {
    planNode.textContent = "Enter an API key to see how this file will be sent; the plan uses the server's limits.";
    show(planNode, true);
    start.disabled = true;
    return;
  }
  const plan = choosePlan(file, limits, Number($("parallelism").value) || 1);
  if (plan.problem) {
    problemNode.textContent = plan.problem;
    show(problemNode, true);
    show(planNode, false);
    start.disabled = true;
    return;
  }
  state.plan = plan;
  const checks = perChunkChecksums() ? "SHA-256 checked per chunk" : "no per-chunk checksums (this page is not secure)";
  const whole = wholeBox.checked ? ", whole file verified after upload" : "";
  planNode.textContent =
    plan.totalChunks === 0
      ? "Plan: an empty file has no chunks; the server records it when you start."
      : `Plan: ${formatCount(plan.totalChunks, "chunk")} of ${formatBytes(plan.chunkSize)}, ${plan.parallelism} in parallel, ${checks}${whole}.`;
  show(planNode, true);
  start.disabled = Boolean(state.job && state.job.running);

  const existing = matchingUnfinishedUpload(file);
  if (existing && !(state.job && state.job.running)) {
    resume.textContent = `Resume upload (${existing.uploaded_chunks} of ${existing.total_chunks} chunks already on server)`;
    resume.dataset.upload = existing.upload_id;
    show(resume, true);
  }
}

function onFileChosen() {
  const file = $("file-input").files[0] || null;
  state.file = file;
  const summary = $("file-summary");
  summary.replaceChildren();
  if (file) summary.append(el("strong", { text: file.name }), `, ${formatBytes(file.size)}`);
  else summary.textContent = "No file chosen.";
  updatePlan();
  if (state.selectedUploadId) renderDetailActions();
}

// ---------------------------------------------------------------- throughput, measured in this browser

const throughput = {
  samples: [],
  record(bytes) {
    this.samples.push({ at: performance.now(), bytes });
  },
  perSecond() {
    const now = performance.now();
    this.samples = this.samples.filter((sample) => now - sample.at <= THROUGHPUT_WINDOW_MS);
    if (!this.samples.length) return null;
    const span = Math.max(1000, Math.min(THROUGHPUT_WINDOW_MS, now - this.samples[0].at));
    return this.samples.reduce((sum, sample) => sum + sample.bytes, 0) / (span / 1000);
  },
};

function renderThroughput() {
  const rate = throughput.perSecond();
  const line = $("throughput-line");
  if (rate === null) line.textContent = "Nothing sent from this browser in the last 10 s.";
  else
    line.replaceChildren(
      el("span", { className: "browser-figure", text: `${formatBytes(Math.round(rate))}/s` }),
      ", measured in this browser over the last 10 s",
    );
}

// ---------------------------------------------------------------- the chunk map (server truth)

function newJob(upload, file, parallelism) {
  return {
    uploadId: upload.upload_id,
    file,
    chunkSize: upload.chunk_size,
    totalChunks: upload.total_chunks,
    parallelism,
    status: upload.status,
    server: new Map(), // index -> chunk info, from GET /v1/uploads/{id}/chunks
    sending: new Set(), // sent by this browser, not yet listed by the server
    roundTrip: new Map(), // index -> ms, measured in this browser
    group: Math.max(1, Math.ceil(upload.total_chunks / MAX_CELLS)),
    cells: [],
    selected: 0,
    running: false,
    error: null,
    refreshing: false,
  };
}

function buildChunkMap(job) {
  const map = $("chunk-map");
  map.replaceChildren();
  const cellCount = Math.ceil(job.totalChunks / job.group);
  map.style.setProperty("--cell", cellCount > 400 ? "12px" : "18px");
  job.cells = [];
  for (let cell = 0; cell < cellCount; cell += 1) {
    const node = el("span", { className: "cell", dataset: { cell: String(cell) } });
    job.cells.push(node);
    map.append(node);
  }
  show($("chunk-area"), job.totalChunks > 0);
}

function cellRange(job, cell) {
  const first = cell * job.group;
  return [first, Math.min(job.totalChunks, first + job.group) - 1];
}

function paintCells(job) {
  job.cells.forEach((node, cell) => {
    const [first, last] = cellRange(job, cell);
    let confirmed = 0;
    let failed = 0;
    let sending = 0;
    for (let index = first; index <= last; index += 1) {
      const info = job.server.get(index);
      if (info && info.status === "UPLOADED") confirmed += 1;
      else if (info && info.status === "FAILED") failed += 1;
      else if (job.sending.has(index)) sending += 1;
    }
    const size = last - first + 1;
    node.className = "cell";
    node.style.removeProperty("--filled");
    if (confirmed === size) node.classList.add("confirmed");
    else if (failed) node.classList.add("failed");
    else if (confirmed) {
      node.classList.add("partial");
      node.style.setProperty("--filled", `${Math.round((confirmed / size) * 100)}%`);
    } else if (sending) node.classList.add("sending");
    if (cell === job.selected) node.classList.add("selected");
  });
}

function describeCell(job, cell) {
  const [first, last] = cellRange(job, cell);
  if (first !== last) {
    let confirmed = 0;
    for (let index = first; index <= last; index += 1) if (job.server.get(index)?.status === "UPLOADED") confirmed += 1;
    return `Chunks ${first} to ${last}: ${confirmed} of ${last - first + 1} confirmed by the server.`;
  }
  const info = job.server.get(first);
  const roundTrip = job.roundTrip.get(first);
  const measured = roundTrip ? ` Round trip ${Math.round(roundTrip)} ms, measured in this browser.` : "";
  if (!info) {
    return job.sending.has(first) ? `Chunk ${first}: being sent by this browser.${measured}` : `Chunk ${first}: not received by the server yet.`;
  }
  const attempts = info.retry_count + 1;
  const checksum = info.sha256 ? `SHA-256 ${info.sha256.slice(0, 12)}, ` : "";
  return `Chunk ${first}: ${formatBytes(info.size_bytes)}, ${checksum}${formatCount(attempts, "storage attempt")}, ${info.status.toLowerCase()} at ${clockTime(new Date(info.updated_at))} (server).${measured}`;
}

function selectCell(cell) {
  const job = state.job;
  if (!job || !job.cells.length) return;
  job.selected = Math.max(0, Math.min(job.cells.length - 1, cell));
  paintCells(job);
  $("chunk-detail").textContent = describeCell(job, job.selected);
}

async function refreshFromServer(job) {
  if (job.refreshing) return;
  job.refreshing = true;
  try {
    const detail = await api("GET", `/v1/uploads/${job.uploadId}`);
    job.status = detail.body.status;
    const server = new Map();
    for (let offset = 0; offset < job.totalChunks; offset += 1000) {
      const page = await api("GET", `/v1/uploads/${job.uploadId}/chunks?offset=${offset}&limit=1000`);
      for (const chunk of page.body.items) server.set(chunk.index, chunk);
      if (page.body.items.length < 1000) break;
    }
    job.server = server;
    for (const index of [...job.sending]) if (server.get(index)?.status === "UPLOADED") job.sending.delete(index);
    renderTransferStatus(job, detail.body);
    paintCells(job);
    if (job.cells.length) $("chunk-detail").textContent = describeCell(job, job.selected);
  } catch (error) {
    addEvent(`Could not refresh the chunk list: ${describeFailure(error)}.`, "bad");
  } finally {
    job.refreshing = false;
  }
}

function renderTransferStatus(job, detail) {
  const confirmed = [...job.server.values()].filter((chunk) => chunk.status === "UPLOADED").length;
  const label = job.totalChunks === 0 ? "an empty file" : `${confirmed} of ${formatCount(job.totalChunks, "chunk")} confirmed by server`;
  $("transfer-status").textContent = `${detail.file_name}: ${detail.status}, ${label}.`;
}

// ---------------------------------------------------------------- sending

async function sendChunk(job, index) {
  const start = index * job.chunkSize;
  const end = Math.min(start + job.chunkSize, job.file.size);
  const data = await job.file.slice(start, end).arrayBuffer();
  const headers = {};
  let sha = "";
  if (perChunkChecksums()) {
    sha = hex(await crypto.subtle.digest("SHA-256", data));
    headers["X-Chunk-SHA256"] = sha;
  }
  // The same chunk always carries the same key, so a repeated request is recognised as a repeat.
  headers["Idempotency-Key"] = `chunk-${job.uploadId}-${index}-${sha ? sha.slice(0, 16) : "nosha"}`;
  job.sending.add(index);
  paintCells(job);
  const started = performance.now();
  try {
    await api("PUT", `/v1/uploads/${job.uploadId}/chunks/${index}`, { body: data, headers });
  } catch (error) {
    job.sending.delete(index);
    throw error;
  }
  job.roundTrip.set(index, performance.now() - started);
  throughput.record(end - start);
  renderThroughput();
}

async function transfer(job, indexes) {
  state.job = job;
  job.running = true;
  job.error = null;
  updatePlan();
  buildChunkMap(job);
  await refreshFromServer(job);
  selectCell(0);
  const queue = indexes.filter((index) => job.server.get(index)?.status !== "UPLOADED");
  if (queue.length) addEvent(`Sending ${formatCount(queue.length, "chunk")}, ${job.parallelism} at a time.`);
  const ticker = setInterval(() => {
    refreshFromServer(job);
    renderThroughput();
  }, 1500);
  const worker = async () => {
    while (queue.length && !job.error) {
      const index = queue.shift();
      try {
        await sendChunk(job, index);
      } catch (error) {
        if (!job.error) job.error = { index, error };
      }
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(job.parallelism, queue.length) }, worker));
  } finally {
    clearInterval(ticker);
  }
  await refreshFromServer(job);
  renderThroughput();
  job.running = false;
  if (job.error) {
    const { index, error } = job.error;
    addEvent(`Chunk ${index} failed: ${describeFailure(error)}. Needs your action: choose Resume to send the chunks the server is missing.`, "bad");
    await afterChange(job.uploadId);
    return;
  }
  await complete(job);
}

async function complete(job) {
  addEvent("All chunks sent. Asking the server to complete the upload.");
  try {
    const result = await api("POST", `/v1/uploads/${job.uploadId}/complete`, { headers: { "Idempotency-Key": `complete-${job.uploadId}` } });
    if (result.body.status === "VERIFYING") {
      addEvent("The server is comparing the stored file with the declared SHA-256.");
      await waitWhileVerifying(job.uploadId);
    }
    await refreshFromServer(job);
    const final = (await api("GET", `/v1/uploads/${job.uploadId}`)).body;
    if (final.status === "COMPLETED") addEvent("Upload completed. Every chunk is confirmed by the server.", "good");
    else if (final.status === "FAILED") addEvent(`Upload failed: ${final.failure_reason || "the server gave no reason"}.`, "bad");
    else addEvent(`The server reports the upload as ${final.status}.`);
  } catch (error) {
    addEvent(`Completing failed: ${describeFailure(error)}. Needs your action: choose Complete again in Your uploads.`, "bad");
  }
  await afterChange(job.uploadId);
}

async function waitWhileVerifying(uploadId) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const detail = await api("GET", `/v1/uploads/${uploadId}`);
    if (detail.body.status !== "VERIFYING") return detail.body.status;
  }
  return "VERIFYING";
}

async function sha256OfFile(file) {
  return hex(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()));
}

async function startUpload() {
  const file = state.file;
  const plan = state.plan;
  if (!file || !plan) return;
  $("start-upload").disabled = true;
  const body = { file_name: file.name, file_size: file.size, chunk_size: plan.chunkSize };
  try {
    if ($("whole-checksum").checked) {
      addEvent("Computing the file's SHA-256 in this browser.");
      body.file_checksum_sha256 = await sha256OfFile(file);
    }
    const init = await api("POST", "/v1/uploads/init", { body, headers: { "Idempotency-Key": `init-${randomId()}` } });
    const upload = { ...init.body, file_name: file.name, file_size: file.size };
    addEvent(`Upload ${shortId(upload.upload_id)} created on the server: ${formatCount(upload.total_chunks, "chunk")} of ${formatBytes(upload.chunk_size)}.`);
    const job = newJob(upload, file, plan.parallelism);
    await transfer(job, [...Array(upload.total_chunks).keys()]);
  } catch (error) {
    addEvent(`Starting the upload failed: ${describeFailure(error)}.`, "bad");
  } finally {
    updatePlan();
  }
}

async function resumeUpload(uploadId) {
  const file = state.file;
  try {
    const detail = (await api("GET", `/v1/uploads/${uploadId}`)).body;
    if (!file || file.name !== detail.file_name || file.size !== detail.file_size) {
      addEvent(`To resume ${detail.file_name}, choose that same file (${formatBytes(detail.file_size)}) above first.`, "bad");
      return;
    }
    const parallelism = Math.max(1, Math.min(Number($("parallelism").value) || 1, state.limits.max_inflight_chunks_per_upload));
    addEvent(`Resuming upload ${shortId(uploadId)}: ${detail.uploaded_chunks} of ${detail.total_chunks} chunks are already on the server.`);
    const job = newJob(detail, file, parallelism);
    await transfer(job, [...Array(detail.total_chunks).keys()]);
  } catch (error) {
    addEvent(`Resuming failed: ${describeFailure(error)}.`, "bad");
  } finally {
    updatePlan();
  }
}

// ---------------------------------------------------------------- your uploads

async function loadUploads(reset) {
  if (!state.apiKey) return;
  const cursor = reset ? "" : state.uploadsCursor;
  try {
    const page = await api("GET", `/v1/uploads?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
    if (reset) {
      state.uploads.clear();
      $("uploads-body").replaceChildren();
    }
    for (const upload of page.body.items) {
      state.uploads.set(upload.upload_id, upload);
      $("uploads-body").append(uploadRow(upload));
    }
    state.uploadsCursor = page.body.next_cursor;
    show($("uploads-more"), Boolean(page.body.next_cursor));
    const any = state.uploads.size > 0;
    show($("uploads-table"), any);
    $("uploads-empty").textContent = "No uploads yet. Choose a file above to start one.";
    show($("uploads-empty"), !any);
    updatePlan();
  } catch (error) {
    $("uploads-empty").textContent = `Could not load your uploads: ${describeFailure(error)}.`;
    show($("uploads-empty"), true);
  }
}

function uploadRow(upload) {
  return el(
    "tr",
    {
      dataset: { upload: upload.upload_id },
      tabindex: "0",
      "aria-selected": upload.upload_id === state.selectedUploadId ? "true" : "false",
      onclick: () => selectUpload(upload.upload_id),
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectUpload(upload.upload_id);
        }
      },
    },
    el("td", { className: "file-cell", title: upload.file_name, text: upload.file_name }),
    el("td", { className: "num col-size", text: formatBytes(upload.file_size) }),
    el("td", {}, el("span", { className: "status", dataset: { status: upload.status }, text: upload.status })),
    el("td", { className: "num", text: `${upload.uploaded_chunks} of ${upload.total_chunks}` }),
    el("td", { className: "col-activity", title: dateTime(upload.last_activity_at), text: ago(upload.last_activity_at) }),
  );
}

async function selectUpload(uploadId) {
  state.selectedUploadId = uploadId;
  for (const row of $("uploads-body").children) row.setAttribute("aria-selected", row.dataset.upload === uploadId ? "true" : "false");
  await renderDetail();
}

function checksumText(detail) {
  if (!detail.file_checksum_sha256) return "None declared; each chunk was still checked on arrival.";
  if (detail.checksum_verified === true) return "Matched the declared SHA-256.";
  if (detail.checksum_verified === false) return "Did not match the declared SHA-256.";
  return "Declared; verification has not finished.";
}

async function renderDetail() {
  const panel = $("upload-detail");
  const uploadId = state.selectedUploadId;
  if (!uploadId) {
    show(panel, false);
    return;
  }
  let detail;
  try {
    detail = (await api("GET", `/v1/uploads/${uploadId}`)).body;
  } catch (error) {
    panel.replaceChildren(el("p", { className: "problem", text: `Could not load this upload: ${describeFailure(error)}.` }));
    show(panel, true);
    return;
  }
  state.detail = detail;
  const facts = el("dl", { className: "facts" });
  const fact = (label, value, className) => facts.append(el("dt", { text: label }), el("dd", { className, text: value }));
  fact("Upload ID", detail.upload_id, "ident");
  fact("Size", `${formatBytes(detail.file_size)} in chunks of ${formatBytes(detail.chunk_size)}`);
  fact("Received", `${detail.uploaded_chunks} of ${formatCount(detail.total_chunks, "chunk")} (${formatBytes(detail.uploaded_bytes)})`);
  fact("Checksum", checksumText(detail));
  if (detail.failure_reason) fact("Failure reason", detail.failure_reason);

  const timeline = el(
    "ol",
    { className: "timeline", "aria-label": "Timeline from the server" },
    el("li", {}, el("time", { datetime: detail.created_at, text: dateTime(detail.created_at) }), "Created"),
    el("li", {}, el("time", { datetime: detail.last_activity_at, text: dateTime(detail.last_activity_at) }), "Last progress"),
    el("li", {}, el("time", { datetime: detail.updated_at, text: dateTime(detail.updated_at) }), `Status now ${detail.status}`),
  );

  panel.replaceChildren(
    el("h3", { text: detail.file_name }),
    el(
      "p",
      { className: "detail-status" },
      el("span", { className: "status", dataset: { status: detail.status }, text: detail.status }),
      el("span", { className: "panel-note", text: "reported by the server" }),
    ),
    facts,
    timeline,
    el("div", { className: "actions", id: "detail-actions" }),
    el("p", { className: "hint", id: "detail-note" }),
  );
  show(panel, true);
  renderDetailActions();
}

function renderDetailActions() {
  const detail = state.detail;
  const actions = $("detail-actions");
  const note = $("detail-note");
  if (!detail || !actions) return;
  actions.replaceChildren();
  note.textContent = "";
  const busy = Boolean(state.job && state.job.running);
  const status = detail.status;

  if (status === "INITIATED" || status === "IN_PROGRESS") {
    const fileMatches = state.file && state.file.name === detail.file_name && state.file.size === detail.file_size;
    actions.append(
      el("button", { type: "button", className: "button primary", disabled: busy || !fileMatches, text: "Resume", onclick: () => resumeUpload(detail.upload_id) }),
      el("button", {
        type: "button",
        className: "button",
        disabled: busy || detail.uploaded_chunks !== detail.total_chunks,
        text: "Complete",
        onclick: () => completeFromDetail(detail.upload_id),
      }),
      abortButton(detail.upload_id, busy),
    );
    if (!fileMatches) note.textContent = `To resume, choose ${detail.file_name} (${formatBytes(detail.file_size)}) in Upload above. Only the missing chunks are sent.`;
    else if (detail.uploaded_chunks !== detail.total_chunks) note.textContent = "Complete becomes available when every chunk is on the server.";
  } else if (status === "COMPLETED") {
    actions.append(
      el("button", { type: "button", className: "button primary", text: "Download", onclick: () => download(detail.upload_id) }),
      el("button", { type: "button", className: "button", text: "Create a share link", onclick: () => shareLink(detail.upload_id) }),
    );
  } else if (status === "VERIFYING" || status === "COMPLETING") {
    note.textContent = "The server is finishing this upload. This view refreshes on its own.";
    setTimeout(() => {
      if (state.selectedUploadId === detail.upload_id) renderDetail();
    }, 2000);
  } else {
    note.textContent = "Nothing more can be done with this upload.";
  }
}

function abortButton(uploadId, busy) {
  let timer = null;
  const button = el("button", { type: "button", className: "button danger", disabled: busy, text: "Abort" });
  button.addEventListener("click", async () => {
    if (button.dataset.armed !== "true") {
      button.dataset.armed = "true";
      button.textContent = "Confirm abort";
      timer = setTimeout(() => {
        button.dataset.armed = "false";
        button.textContent = "Abort";
      }, 4000);
      return;
    }
    clearTimeout(timer);
    button.disabled = true;
    try {
      await api("DELETE", `/v1/uploads/${uploadId}`);
      addEvent(`Upload ${shortId(uploadId)} aborted. The server discarded its stored chunks.`);
    } catch (error) {
      addEvent(`Aborting failed: ${describeFailure(error)}.`, "bad");
    }
    await afterChange(uploadId);
  });
  return button;
}

async function completeFromDetail(uploadId) {
  const detail = state.detail;
  const job = newJob(detail, state.file, 1);
  state.job = job;
  buildChunkMap(job);
  await complete(job);
}

async function download(uploadId) {
  // A signed link the browser follows itself, so the file streams to disk instead of into memory.
  try {
    const link = await api("POST", `/v1/uploads/${uploadId}/download-url`);
    addEvent(`Download started from a link that expires at ${clockTime(new Date(link.body.expires_at))}.`);
    window.location.assign(link.body.url);
  } catch (error) {
    addEvent(`Download failed: ${describeFailure(error)}.`, "bad");
  }
}

async function shareLink(uploadId) {
  try {
    const link = await api("POST", `/v1/uploads/${uploadId}/download-url`);
    const note = $("detail-note");
    const field = el("input", { type: "text", readonly: true, className: "ident", value: link.body.url, "aria-label": "Share link" });
    note.replaceChildren(`Anyone with this link can download the file until ${clockTime(new Date(link.body.expires_at))}.`, el("br"), field);
    field.select();
  } catch (error) {
    addEvent(`Creating a share link failed: ${describeFailure(error)}.`, "bad");
  }
}

async function afterChange(uploadId) {
  await loadUploads(true);
  if (state.selectedUploadId === uploadId || !state.selectedUploadId) {
    state.selectedUploadId = uploadId;
    await selectUpload(uploadId);
  }
}

// ---------------------------------------------------------------- service status (this instance)

async function loadStats() {
  if (!state.apiKey) {
    $("service-source").textContent = "Enter an API key to see this instance's numbers.";
    return;
  }
  try {
    const stats = (await api("GET", "/v1/stats")).body;
    state.statsAt = Date.now();
    state.statsInstance = stats.instance_id;
    const facts = $("service-facts");
    facts.replaceChildren();
    const fact = (label, value) => facts.append(el("dt", { text: label }), el("dd", { text: value }));
    fact("In flight", `${stats.inflight_chunks} of ${stats.inflight_limit} chunks`);
    fact("Queued", `${stats.queued_chunks} of ${stats.queue_limit} chunks`);
    fact("Storage workers", String(stats.worker_threads));
    fact("Chunks received", `${stats.chunks_last_5m.toLocaleString()} in the last 5 min`);
    fact("Data received", `${formatBytes(stats.bytes_last_5m)} in the last 5 min`);
    fact("Throttled requests", `${stats.throttled_last_5m.toLocaleString()} in the last 5 min`);
    fact(
      "Storage write time",
      stats.storage_write_ms_p50 === null ? "no writes in the last 5 min" : `${stats.storage_write_ms_p50} ms median, ${stats.storage_write_ms_p95} ms p95`,
    );
    fact("Uptime", uptime(stats.uptime_s));
    renderStatsAge();
  } catch (error) {
    $("service-source").textContent = `Could not load the numbers: ${describeFailure(error)}.`;
  }
}

function renderStatsAge() {
  if (!state.statsAt) return;
  const seconds = Math.round((Date.now() - state.statsAt) / 1000);
  $("service-source").textContent = `Reported by instance ${state.statsInstance}, updated ${seconds} s ago`;
}

// ---------------------------------------------------------------- wiring

function onChunkMapKey(event) {
  const job = state.job;
  if (!job || !job.cells.length) return;
  const columns = Math.max(1, Math.round($("chunk-map").clientWidth / (job.cells[0].offsetWidth + 3)));
  const moves = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: columns, ArrowUp: -columns, Home: -Infinity, End: Infinity };
  if (!(event.key in moves)) return;
  event.preventDefault();
  const move = moves[event.key];
  selectCell(move === -Infinity ? 0 : move === Infinity ? job.cells.length - 1 : job.selected + move);
}

function onChunkMapPointer(event) {
  const cell = event.target.closest(".cell");
  if (cell && cell.dataset.cell !== undefined) selectCell(Number(cell.dataset.cell));
}

function init() {
  applyTheme(readTheme());
  $("theme-toggle").addEventListener("click", () => {
    applyTheme(THEMES[(THEMES.indexOf(state.theme) + 1) % THEMES.length]);
  });
  $("key-form").addEventListener("submit", useKey);
  $("file-input").addEventListener("change", onFileChosen);
  $("parallelism").addEventListener("input", updatePlan);
  $("whole-checksum").addEventListener("change", updatePlan);
  $("start-upload").addEventListener("click", startUpload);
  $("resume-upload").addEventListener("click", () => resumeUpload($("resume-upload").dataset.upload));
  $("uploads-more").addEventListener("click", () => loadUploads(false));
  $("chunk-map").addEventListener("keydown", onChunkMapKey);
  $("chunk-map").addEventListener("pointerover", onChunkMapPointer);
  $("chunk-map").addEventListener("click", onChunkMapPointer);

  refreshServerFacts();
  loadVersion();
  updatePlan();
  setInterval(refreshServerFacts, 10_000);
  setInterval(loadStats, 5000);
  setInterval(renderStatsAge, 1000);
  setInterval(() => {
    if (state.apiKey && !(state.job && state.job.running)) loadUploads(true);
  }, 15_000);
}

init();
