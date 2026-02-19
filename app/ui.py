def ui_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Distributed File Service Console</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --card: #ffffff;
      --ink: #1f2937;
      --muted: #6b7280;
      --accent: #0f766e;
      --accent-2: #115e59;
      --warn: #b45309;
      --border: #d1d5db;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background: radial-gradient(circle at top left, #d8f3ee 0%, var(--bg) 45%);
      color: var(--ink);
    }
    .wrap {
      max-width: 920px;
      margin: 32px auto;
      padding: 0 16px;
    }
    .hero {
      background: linear-gradient(135deg, #0f766e, #115e59);
      color: #fff;
      border-radius: 16px;
      padding: 20px;
      margin-bottom: 16px;
    }
    .hero h1 {
      margin: 0 0 6px 0;
      font-size: 1.4rem;
      letter-spacing: 0.3px;
    }
    .hero p {
      margin: 0;
      opacity: 0.9;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 12px;
    }
    .row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
    }
    label {
      display: block;
      font-size: 0.82rem;
      color: var(--muted);
      margin-bottom: 4px;
    }
    input, button, textarea {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
      font: inherit;
    }
    button {
      background: var(--accent);
      color: #fff;
      border: none;
      font-weight: 600;
      cursor: pointer;
    }
    button:hover { background: var(--accent-2); }
    .ghost {
      background: #fff;
      color: var(--accent-2);
      border: 1px solid var(--accent-2);
    }
    .progress {
      height: 10px;
      border-radius: 8px;
      background: #e5e7eb;
      overflow: hidden;
      margin-top: 10px;
    }
    .bar {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #14b8a6, #0f766e);
      transition: width 0.2s;
    }
    .mono {
      font-family: Consolas, Menlo, monospace;
      font-size: 0.9rem;
    }
    .warn { color: var(--warn); }
    .log {
      min-height: 160px;
      background: #0b1220;
      color: #d1e9ff;
      border-radius: 10px;
      padding: 12px;
      white-space: pre-wrap;
      font-family: Consolas, Menlo, monospace;
      font-size: 0.85rem;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Distributed File Service Console</h1>
      <p>Production-style demo UI for chunked uploads and downloads.</p>
    </section>

    <section class="card">
      <div class="row">
        <div>
          <label>Base URL</label>
          <input id="baseUrl" value="" />
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
    </section>

    <section class="card">
      <div class="row">
        <div>
          <label>Select File</label>
          <input id="fileInput" type="file" />
        </div>
        <div>
          <label>Upload ID (for resume/download)</label>
          <input id="uploadId" class="mono" />
        </div>
      </div>
      <div class="row" style="margin-top:10px;">
        <div><button id="startBtn">Start Upload</button></div>
        <div><button id="resumeBtn" class="ghost">Resume Missing Chunks</button></div>
        <div><button id="completeBtn" class="ghost">Complete Upload</button></div>
        <div><button id="downloadBtn" class="ghost">Download File</button></div>
      </div>
      <div class="progress"><div class="bar" id="bar"></div></div>
      <p id="status" class="mono warn"></p>
    </section>

    <section class="card">
      <label>Activity Log</label>
      <div id="log" class="log"></div>
    </section>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    const logEl = $("log");
    const statusEl = $("status");
    const barEl = $("bar");

    const baseUrl = () => $("baseUrl").value.trim() || window.location.origin;
    const apiKey = () => $("apiKey").value.trim();
    const chunkSize = () => Math.max(1, Number($("chunkSize").value || "1048576"));
    const fileInput = () => $("fileInput").files[0];

    $("baseUrl").value = window.location.origin;

    function log(msg, obj) {
      const line = `[${new Date().toISOString()}] ${msg}` + (obj ? ` ${JSON.stringify(obj)}` : "");
      logEl.textContent += line + "\\n";
      logEl.scrollTop = logEl.scrollHeight;
    }

    function setProgress(done, total) {
      const pct = total > 0 ? Math.floor((done / total) * 100) : 0;
      barEl.style.width = `${pct}%`;
      statusEl.textContent = `Progress: ${done}/${total} chunks (${pct}%)`;
    }

    async function api(path, opts = {}) {
      const headers = Object.assign({}, opts.headers || {}, { "X-API-Key": apiKey() });
      const res = await fetch(baseUrl() + path, Object.assign({}, opts, { headers }));
      let body;
      try { body = await res.json(); } catch { body = null; }
      if (!res.ok) {
        throw new Error((body && body.detail) || `HTTP ${res.status}`);
      }
      return body;
    }

    async function initUpload(file) {
      const payload = { file_name: file.name, file_size: file.size, chunk_size: chunkSize() };
      return await api("/v1/uploads/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
    }

    async function uploadChunk(uploadId, index, blob) {
      const arr = await blob.arrayBuffer();
      const hashBuf = await crypto.subtle.digest("SHA-256", arr);
      const hashHex = [...new Uint8Array(hashBuf)].map((x) => x.toString(16).padStart(2, "0")).join("");
      await api(`/v1/uploads/${uploadId}/chunks/${index}`, {
        method: "PUT",
        headers: {
          "Content-Length": String(blob.size),
          "X-Chunk-SHA256": hashHex
        },
        body: blob
      });
    }

    function makeChunks(file, size) {
      const chunks = [];
      for (let i = 0; i < file.size; i += size) {
        chunks.push(file.slice(i, Math.min(i + size, file.size)));
      }
      return chunks;
    }

    async function uploadAll(uploadId, file, onlyIndexes = null) {
      const chunks = makeChunks(file, chunkSize());
      const indexes = onlyIndexes || chunks.map((_, i) => i);
      let done = 0;
      setProgress(0, indexes.length);
      for (const idx of indexes) {
        await uploadChunk(uploadId, idx, chunks[idx]);
        done += 1;
        setProgress(done, indexes.length);
        log("uploaded chunk", { upload_id: uploadId, chunk_index: idx });
      }
    }

    $("startBtn").onclick = async () => {
      try {
        const file = fileInput();
        if (!file) throw new Error("Select a file first");
        const init = await initUpload(file);
        $("uploadId").value = init.upload_id;
        log("init complete", init);
        await uploadAll(init.upload_id, file);
      } catch (e) {
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
        await uploadAll(uploadId, file, missing.missing_chunk_indexes);
      } catch (e) {
        log("resume failed", { error: String(e.message || e) });
      }
    };

    $("completeBtn").onclick = async () => {
      try {
        const uploadId = $("uploadId").value.trim();
        if (!uploadId) throw new Error("Set upload ID");
        const result = await api(`/v1/uploads/${uploadId}/complete`, { method: "POST" });
        log("complete response", result);
      } catch (e) {
        log("complete failed", { error: String(e.message || e) });
      }
    };

    $("downloadBtn").onclick = async () => {
      try {
        const uploadId = $("uploadId").value.trim();
        if (!uploadId) throw new Error("Set upload ID");
        const res = await fetch(baseUrl() + `/v1/uploads/${uploadId}/download`, {
          headers: { "X-API-Key": apiKey() }
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
        log("download complete", { upload_id: uploadId, bytes: blob.size, file_name: fileName });
      } catch (e) {
        log("download failed", { error: String(e.message || e) });
      }
    };
  </script>
</body>
</html>
"""
