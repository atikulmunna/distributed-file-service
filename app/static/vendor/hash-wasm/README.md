# hash-wasm (vendored)

SHA-256 in WebAssembly, used by `app/static/hash-worker.js` to hash a whole file incrementally,
so a large file is never loaded into memory.

- Package: [hash-wasm](https://www.npmjs.com/package/hash-wasm) 4.12.0 (published 2024-11-19), MIT licensed (see `LICENSE`).
- File: `dist/sha256.umd.min.js` from the npm tarball, unmodified.
- Tarball integrity (npm, sha512): `+/2B2rYLb48I/evdOIhP+K/DD2ca2fgBjp6O+GBEnCDk2e4rpeXIK8GvIyRPjTezgmWn9gmKwkQjjx6BtqDHVQ==`
- `sha256.umd.min.js` SHA-256: `ed7f5896b41aba98909ab143264c5b2ae5c81abf2e47f08dd2d1c0d02ce623de`

To update: download the new tarball from the npm registry, check it against the registry's
integrity value, copy `dist/sha256.umd.min.js` and `LICENSE` here, and update this file.
`tests/test_web_ui.py` checks the file against the SHA-256 above.
