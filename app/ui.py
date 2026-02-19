def ui_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Distributed File Service Console</title>
  <style>
    :root {
      --bg: #f3f6f8;
      --card: #ffffff;
      --ink: #111827;
      --muted: #4b5563;
      --accent: #0f766e;
      --accent-2: #115e59;
      --border: #d1d5db;
      --good: #166534;
      --bad: #b91c1c;
      --warn: #92400e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at top left, #c8efe8 0%, var(--bg) 45%);
    }
    .wrap {
      max-width: 1200px;
      margin: 26px auto 42px;
      padding: 0 14px;
    }
    .hero {
      background: linear-gradient(135deg, #134e4a, #0f766e);
      color: #fff;
      border-radius: 16px;
      padding: 18px 20px;
      margin-bottom: 12px;
    }
    .hero h1 {
      margin: 0 0 6px 0;
      font-size: 1.35rem;
      letter-spacing: 0.2px;
    }
    .hero p {
      margin: 0;
      opacity: 0.92;
      font-size: 0.93rem;
    }
    .grid-2 {
      display: grid;
      gap: 12px;
      grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
      margin-bottom: 12px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 12px;
      box-shadow: 0 2px 0 rgba(0, 0, 0, 0.03);
    }
    .title {
      margin: 0 0 10px 0;
      font-size: 0.95rem;
      color: var(--muted);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 8px;
    }
    label {
      display: block;
      font-size: 0.77rem;
      color: var(--muted);
      margin-bottom: 4px;
      font-weight: 600;
    }
    input, button {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px 10px;
      font: inherit;
      background: #fff;
    }
    button {
      border: none;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      cursor: pointer;
    }
    button:hover { background: var(--accent-2); }
    button.alt {
      background: #fff;
      border: 1px solid var(--accent-2);
      color: var(--accent-2);
    }
    .progress {
      height: 9px;
      border-radius: 8px;
      background: #e5e7eb;
      overflow: hidden;
      margin-top: 10px;
    }
    .bar {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #14b8a6, #0f766e);
      transition: width 0.15s ease;
    }
    .mono { font-family: Consolas, Menlo, monospace; font-size: 0.88rem; }
    .badge {
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.72rem;
      font-weight: 700;
      margin-right: 5px;
    }
    .ok { background: #dcfce7; color: var(--good); }
    .warn { background: #fef3c7; color: var(--warn); }
    .bad { background: #fee2e2; color: var(--bad); }
    .kv {
      display: grid;
      grid-template-columns: 1fr 2fr;
      gap: 6px 10px;
      font-size: 0.86rem;
    }
    .kv div:nth-child(odd) { color: var(--muted); font-weight: 700; }
    .table-wrap {
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 10px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }
    th, td {
      border-bottom: 1px solid #e5e7eb;
      padding: 7px 8px;
      text-align: left;
      white-space: nowrap;
    }
    th { background: #f8fafc; color: var(--muted); }
    .log {
      min-height: 160px;
      background: #0b1220;
      color: #d1e9ff;
      border-radius: 10px;
      padding: 10px;
      white-space: pre-wrap;
      font-family: Consolas, Menlo, monospace;
      font-size: 0.82rem;
    }
    @media (max-width: 960px) {
      .grid-2 { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Distributed File Service Console</h1>
      <p>Chunked upload operator view with request timeline, queue/runtime details, and chunk-level execution tracking.</p>
    </section>

    <section class="grid-2">
      <div class="card">
        <h2 class="title">Control Plane</h2>
        <div class="row">
          <div>
            <label>Base URL</label>
            <input id="baseUrl" />
          </div>
          <div>
            <label>API Key</label>
            <input id="apiKey" value="dev-key" />
          </div>
          <div>
            <label>Chunk Size (bytes)</label>
            <input id="chunkSize" type="number" value="1048576" />
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Select File</label>
            <input id="fileInput" type="file" />
          </div>
          <div>
            <label>Upload ID (for resume/download)</label>
            <input id="uploadId" class="mono" />
          </div>
          <div>
            <label>Actions</label>
            <button id="runtimeBtn" class="alt">Refresh Runtime</button>
          </div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div><button id="startBtn">Start Upload</button></div>
          <div><button id="resumeBtn" class="alt">Resume Missing Chunks</button></div>
          <div><button id="completeBtn" class="alt">Complete Upload</button></div>
          <div><button id="downloadBtn" class="alt">Download File</button></div>
        </div>
        <div class="row" style="margin-top:8px;">
          <div>
            <label>Chunk Upload Parallelism</label>
            <input id="parallelism" type="number" value="3" min="1" max="16" />
          </div>
          <div>
            <label>Download Range (start-end, optional)</label>
            <input id="downloadRange" class="mono" placeholder="e.g. 0-1048575" />
          </div>
          <div>
            <label>Behavior Flags</label>
            <div class="mono" style="display:grid; gap:2px;">
              <label style="display:flex; align-items:center; gap:6px; margin:0;"><input id="autoComplete" type="checkbox" checked style="width:auto;">auto-complete after upload</label>
              <label style="display:flex; align-items:center; gap:6px; margin:0;"><input id="useIdempotency" type="checkbox" checked style="width:auto;">add idempotency keys</label>
              <label style="display:flex; align-items:center; gap:6px; margin:0;"><input id="sendFileChecksum" type="checkbox" style="width:auto;">send file sha256 on init</label>
            </div>
          </div>
        </div>
        <div class="progress"><div class="bar" id="bar"></div></div>
        <p id="status" class="mono"></p>
      </div>

      <div class="card">
        <h2 class="title">Runtime / Background</h2>
        <div class="kv mono" id="runtimeInfo">
          <div>app_name</div><div>loading...</div>
          <div>app_version</div><div>loading...</div>
          <div>queue_backend</div><div>loading...</div>
          <div>storage_backend</div><div>loading...</div>
        </div>
        <hr style="border:none; border-top:1px solid #e5e7eb; margin:10px 0;">
        <div class="kv mono" id="transferInfo">
          <div>file_name</div><div>-</div>
          <div>file_size</div><div>-</div>
          <div>total_chunks</div><div>-</div>
          <div>uploaded_chunks</div><div>-</div>
          <div>bytes_uploaded</div><div>-</div>
          <div>elapsed_ms</div><div>-</div>
          <div>avg_throughput</div><div>-</div>
        </div>
      </div>
    </section>

    <section class="card">
      <h2 class="title">Chunk Execution</h2>
      <div class="table-wrap">
        <table id="chunkTable">
          <thead>
            <tr>
              <th>chunk</th>
              <th>size_bytes</th>
              <th>sha256</th>
              <th>status</th>
              <th>duration_ms</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </section>

    <section class="grid-2">
      <div class="card">
        <h2 class="title">Request Timeline</h2>
        <div class="table-wrap">
          <table id="reqTable">
            <thead>
              <tr>
                <th>time</th>
                <th>method</th>
                <th>path</th>
                <th>status</th>
                <th>duration_ms</th>
                <th>request_id</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
      <div class="card">
        <h2 class="title">Activity Log</h2>
        <div id="log" class="log"></div>
      </div>
    </section>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    const logEl = $("log");
    const statusEl = $("status");
    const barEl = $("bar");
    const runtimeInfoEl = $("runtimeInfo");
    const transferInfoEl = $("transferInfo");
    const chunkTableBody = $("chunkTable").querySelector("tbody");
    const reqTableBody = $("reqTable").querySelector("tbody");

    const state = {
      requestTimeline: [],
      chunkRows: new Map(),
      transfer: {
        file_name: "-",
        file_size: 0,
        total_chunks: 0,
        uploaded_chunks: 0,
        bytes_uploaded: 0,
        started_at: 0
      }
    };

    const baseUrl = () => $("baseUrl").value.trim() || window.location.origin;
    const apiKey = () => $("apiKey").value.trim();
    const chunkSize = () => Math.max(1, Number($("chunkSize").value || "1048576"));
    const parallelism = () => Math.max(1, Number($("parallelism").value || "1"));
    const fileInput = () => $("fileInput").files[0];
    $("baseUrl").value = window.location.origin;

    function nowIso() { return new Date().toISOString(); }
    function shortHex(s) { return s ? `${s.slice(0, 10)}...` : "-"; }
    function bytesHuman(n) {
      if (!n || n < 0) return "0 B";
      const u = ["B", "KB", "MB", "GB"];
      let idx = 0;
      let val = n;
      while (val >= 1024 && idx < u.length - 1) { val /= 1024; idx += 1; }
      return `${val.toFixed(idx === 0 ? 0 : 2)} ${u[idx]}`;
    }

    function log(msg, obj) {
      const line = `[${nowIso()}] ${msg}` + (obj ? ` ${JSON.stringify(obj)}` : "");
      logEl.textContent += line + "\\n";
      logEl.scrollTop = logEl.scrollHeight;
    }

    function setStatus(text, level = "warn") {
      statusEl.textContent = text;
      statusEl.className = `mono ${level}`;
    }

    function setProgress(done, total) {
      const pct = total > 0 ? Math.floor((done / total) * 100) : 0;
      barEl.style.width = `${pct}%`;
      setStatus(`Progress: ${done}/${total} chunks (${pct}%)`, done === total ? "ok" : "warn");
    }

    function renderRuntimeInfo(payload) {
      runtimeInfoEl.innerHTML = `
        <div>app_name</div><div>${payload.app_name || "-"}</div>
        <div>app_version</div><div>${payload.app_version || "-"}</div>
        <div>queue_backend</div><div><span class="badge warn">${payload.queue_backend || "-"}</span></div>
        <div>storage_backend</div><div><span class="badge ok">${payload.storage_backend || "-"}</span></div>
      `;
    }

    function renderTransferInfo() {
      const t = state.transfer;
      const elapsed = t.started_at ? Date.now() - t.started_at : 0;
      const throughput = elapsed > 0 ? (t.bytes_uploaded / (elapsed / 1000)) : 0;
      transferInfoEl.innerHTML = `
        <div>file_name</div><div>${t.file_name || "-"}</div>
        <div>file_size</div><div>${bytesHuman(t.file_size)}</div>
        <div>total_chunks</div><div>${t.total_chunks || "-"}</div>
        <div>uploaded_chunks</div><div>${t.uploaded_chunks || 0}</div>
        <div>bytes_uploaded</div><div>${bytesHuman(t.bytes_uploaded)}</div>
        <div>elapsed_ms</div><div>${elapsed || "-"}</div>
        <div>avg_throughput</div><div>${throughput > 0 ? bytesHuman(throughput) + "/s" : "-"}</div>
      `;
    }

    function renderChunkTable() {
      const rows = [...state.chunkRows.values()].sort((a, b) => a.index - b.index);
      chunkTableBody.innerHTML = rows.map((r) => {
        const cls = r.status === "uploaded" ? "ok" : (r.status === "failed" ? "bad" : "warn");
        return `<tr>
          <td class="mono">${r.index}</td>
          <td class="mono">${r.size}</td>
          <td class="mono">${shortHex(r.sha256)}</td>
          <td><span class="badge ${cls}">${r.status}</span></td>
          <td class="mono">${r.duration_ms || "-"}</td>
        </tr>`;
      }).join("");
    }

    function renderReqTable() {
      reqTableBody.innerHTML = state.requestTimeline.map((r) => {
        const cls = r.status >= 200 && r.status < 300 ? "ok" : "bad";
        return `<tr>
          <td class="mono">${r.time}</td>
          <td class="mono">${r.method}</td>
          <td class="mono">${r.path}</td>
          <td><span class="badge ${cls}">${r.status}</span></td>
          <td class="mono">${r.duration_ms}</td>
          <td class="mono">${r.request_id || "-"}</td>
        </tr>`;
      }).join("");
    }

    function recordRequest(meta) {
      state.requestTimeline.unshift(meta);
      state.requestTimeline = state.requestTimeline.slice(0, 60);
      renderReqTable();
    }

    async function fetchRuntime() {
      const res = await fetch(baseUrl() + "/version");
      if (!res.ok) throw new Error(`runtime check failed: HTTP ${res.status}`);
      const payload = await res.json();
      renderRuntimeInfo(payload);
      log("runtime loaded", payload);
    }

    async function api(path, opts = {}) {
      const method = opts.method || "GET";
      const headers = Object.assign({}, opts.headers || {}, { "X-API-Key": apiKey() });
      const started = performance.now();
      const res = await fetch(baseUrl() + path, Object.assign({}, opts, { headers }));
      const durationMs = Math.round(performance.now() - started);
      const reqId = res.headers.get("X-Request-ID");
      const appVer = res.headers.get("X-DFS-App-Version");
      let body = null;
      try { body = await res.json(); } catch {}
      recordRequest({
        time: nowIso(),
        method,
        path,
        status: res.status,
        duration_ms: durationMs,
        request_id: reqId
      });
      if (appVer) {
        const current = runtimeInfoEl.querySelector("div:nth-child(4)");
        if (current && current.textContent !== appVer) {
          runtimeInfoEl.querySelector("div:nth-child(4)").textContent = appVer;
        }
      }
      if (!res.ok) {
        throw new Error((body && body.detail) || `HTTP ${res.status}`);
      }
      return body;
    }

    function makeChunks(file, size) {
      const chunks = [];
      for (let i = 0; i < file.size; i += size) {
        chunks.push(file.slice(i, Math.min(i + size, file.size)));
      }
      return chunks;
    }

    async function sha256Hex(blob) {
      const arr = await blob.arrayBuffer();
      const hashBuf = await crypto.subtle.digest("SHA-256", arr);
      return [...new Uint8Array(hashBuf)].map((x) => x.toString(16).padStart(2, "0")).join("");
    }

    async function initUpload(file) {
      const payload = { file_name: file.name, file_size: file.size, chunk_size: chunkSize() };
      if ($("sendFileChecksum").checked) {
        payload.file_checksum_sha256 = await sha256Hex(file);
      }
      const headers = { "Content-Type": "application/json" };
      if ($("useIdempotency").checked) headers["Idempotency-Key"] = `init-${Date.now()}-${Math.random()}`;
      return await api("/v1/uploads/init", {
        method: "POST",
        headers,
        body: JSON.stringify(payload)
      });
    }

    async function uploadAll(uploadId, file, onlyIndexes = null) {
      const chunks = makeChunks(file, chunkSize());
      const indexes = onlyIndexes || chunks.map((_, i) => i);
      if (state.transfer.started_at === 0) state.transfer.started_at = Date.now();
      setProgress(0, indexes.length);
      let cursor = 0;
      let done = 0;
      const workers = new Array(parallelism()).fill(0).map(async (_, workerId) => {
        while (true) {
          const pointer = cursor;
          cursor += 1;
          if (pointer >= indexes.length) break;
          const idx = indexes[pointer];
          const blob = chunks[idx];
          const sha = await sha256Hex(blob);
          state.chunkRows.set(idx, { index: idx, size: blob.size, sha256: sha, status: "uploading", duration_ms: "-" });
          renderChunkTable();
          const started = performance.now();
          try {
            const headers = {
              "Content-Length": String(blob.size),
              "X-Chunk-SHA256": sha
            };
            if ($("useIdempotency").checked) headers["Idempotency-Key"] = `chunk-${uploadId}-${idx}`;
            await api(`/v1/uploads/${uploadId}/chunks/${idx}`, {
              method: "PUT",
              headers,
              body: blob
            });
            const elapsed = Math.round(performance.now() - started);
            state.chunkRows.set(idx, { index: idx, size: blob.size, sha256: sha, status: "uploaded", duration_ms: elapsed });
            state.transfer.uploaded_chunks += 1;
            state.transfer.bytes_uploaded += blob.size;
            done += 1;
            setProgress(done, indexes.length);
            renderChunkTable();
            renderTransferInfo();
            log("uploaded chunk", { upload_id: uploadId, chunk_index: idx, duration_ms: elapsed, worker_id: workerId });
          } catch (e) {
            state.chunkRows.set(idx, { index: idx, size: blob.size, sha256: sha, status: "failed", duration_ms: "-" });
            renderChunkTable();
            throw e;
          }
        }
      });
      await Promise.all(workers);
    }

    async function completeUpload(uploadId) {
      const headers = {};
      if ($("useIdempotency").checked) headers["Idempotency-Key"] = `complete-${uploadId}`;
      const result = await api(`/v1/uploads/${uploadId}/complete`, { method: "POST", headers });
      setStatus(`Upload completed: ${uploadId}`, "ok");
      log("complete response", result);
    }

    async function downloadUpload(uploadId) {
      const started = performance.now();
      const headers = { "X-API-Key": apiKey() };
      const rangeValue = $("downloadRange").value.trim();
      if (rangeValue) headers["Range"] = `bytes=${rangeValue}`;
      const res = await fetch(baseUrl() + `/v1/uploads/${uploadId}/download`, { headers });
      const duration = Math.round(performance.now() - started);
      recordRequest({
        time: nowIso(),
        method: "GET",
        path: `/v1/uploads/${uploadId}/download`,
        status: res.status,
        duration_ms: duration,
        request_id: res.headers.get("X-Request-ID")
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const contentDisposition = res.headers.get("Content-Disposition") || "";
      let fileName = `download-${uploadId}.bin`;
      const match = contentDisposition.match(/filename="([^"]+)"/i);
      if (match && match[1]) fileName = match[1];
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = fileName;
      a.click();
      URL.revokeObjectURL(a.href);
      log("download complete", { upload_id: uploadId, bytes: blob.size, file_name: fileName, duration_ms: duration, range: rangeValue || null });
      setStatus(`Downloaded ${fileName} (${bytesHuman(blob.size)})`, "ok");
    }

    function resetTransferForFile(file) {
      const chunks = makeChunks(file, chunkSize());
      state.transfer = {
        file_name: file.name,
        file_size: file.size,
        total_chunks: chunks.length,
        uploaded_chunks: 0,
        bytes_uploaded: 0,
        started_at: Date.now()
      };
      state.chunkRows.clear();
      renderChunkTable();
      renderTransferInfo();
      setProgress(0, chunks.length);
    }

    $("runtimeBtn").onclick = async () => {
      try {
        await fetchRuntime();
      } catch (e) {
        log("runtime refresh failed", { error: String(e.message || e) });
      }
    };

    $("startBtn").onclick = async () => {
      try {
        const file = fileInput();
        if (!file) throw new Error("Select a file first");
        resetTransferForFile(file);
        const init = await initUpload(file);
        $("uploadId").value = init.upload_id;
        log("init complete", init);
        await uploadAll(init.upload_id, file);
        if ($("autoComplete").checked) {
          await completeUpload(init.upload_id);
        }
      } catch (e) {
        setStatus(`Upload failed: ${String(e.message || e)}`, "bad");
        log("start upload failed", { error: String(e.message || e) });
      }
    };

    $("resumeBtn").onclick = async () => {
      try {
        const uploadId = $("uploadId").value.trim();
        const file = fileInput();
        if (!uploadId) throw new Error("Set upload ID");
        if (!file) throw new Error("Select the original file");
        const missing = await api(`/v1/uploads/${uploadId}/missing-chunks`);
        log("missing chunks", missing);
        if (!missing.missing_chunk_indexes || missing.missing_chunk_indexes.length === 0) {
          setStatus("No missing chunks. Upload can be completed.", "ok");
          return;
        }
        if (state.transfer.file_name !== file.name) resetTransferForFile(file);
        await uploadAll(uploadId, file, missing.missing_chunk_indexes);
      } catch (e) {
        setStatus(`Resume failed: ${String(e.message || e)}`, "bad");
        log("resume failed", { error: String(e.message || e) });
      }
    };

    $("completeBtn").onclick = async () => {
      try {
        const uploadId = $("uploadId").value.trim();
        if (!uploadId) throw new Error("Set upload ID");
        await completeUpload(uploadId);
      } catch (e) {
        setStatus(`Complete failed: ${String(e.message || e)}`, "bad");
        log("complete failed", { error: String(e.message || e) });
      }
    };

    $("downloadBtn").onclick = async () => {
      try {
        const uploadId = $("uploadId").value.trim();
        if (!uploadId) throw new Error("Set upload ID");
        await downloadUpload(uploadId);
      } catch (e) {
        setStatus(`Download failed: ${String(e.message || e)}`, "bad");
        log("download failed", { error: String(e.message || e) });
      }
    };

    fetchRuntime().catch((e) => {
      log("runtime load failed", { error: String(e.message || e) });
      setStatus("Unable to load runtime metadata.", "bad");
    });
    renderTransferInfo();
  </script>
</body>
</html>
"""
