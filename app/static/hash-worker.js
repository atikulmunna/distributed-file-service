// Hashes one File with SHA-256 incrementally (hash-wasm, vendored), reading it in slices, so the
// whole file is never in memory. Messages out: progress {done, total}, done {sha256}, error {message}.
importScripts("/static/vendor/hash-wasm/sha256.umd.min.js");

const SLICE_BYTES = 8 * 1024 * 1024;

self.onmessage = async (event) => {
  const file = event.data.file;
  try {
    const hasher = await self.hashwasm.createSHA256();
    hasher.init();
    for (let offset = 0; offset < file.size; offset += SLICE_BYTES) {
      const slice = await file.slice(offset, offset + SLICE_BYTES).arrayBuffer();
      hasher.update(new Uint8Array(slice));
      self.postMessage({ type: "progress", done: Math.min(file.size, offset + SLICE_BYTES), total: file.size });
    }
    self.postMessage({ type: "done", sha256: hasher.digest("hex") });
  } catch (error) {
    self.postMessage({ type: "error", message: String((error && error.message) || error) });
  }
};
