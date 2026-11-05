// Read selected fields from one pinned Parquet file into a PRIVATE local JSON.
// No network calls, identity claims, model training, or money authority.
// The caller must supply the pinned MIT-licensed hyparquet source tree.
import { createHash } from 'node:crypto';
import { readFile, open } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const MANIFEST_SHA256 = '3dbb74fe80df960cfd38527dbb9899b58c42439d2de661c37f7a81b3b8e05c3a';
const PARQUET_SHA256 = '6d744f1001a48b5109790cd1d0ab896eacd7b1621885afd22d55195a4a970044';
const COLUMNS = ['source_match_id', 'match_key', 'player_a_id', 'player_b_id',
  'winner_id', 'category', 'drop_reason', 'is_doubles'];

function digest(kind, bytes) { return createHash(kind).update(bytes).digest('hex'); }
function gitBlob(bytes) {
  return createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
}

async function main() {
  if (process.argv.length !== 5) throw new Error('usage: node decoder.mjs reader_dir input.parquet private_output.json');
  const [, , readerDir, parquetPath, outputPath] = process.argv;
  const manifestBytes = await readFile(new URL('./hyparquet_reader_manifest_20261105.json', import.meta.url));
  if (digest('sha256', manifestBytes) !== MANIFEST_SHA256) throw new Error('reader manifest hash mismatch');
  const manifest = JSON.parse(manifestBytes);
  for (const [relative, expected] of Object.entries(manifest.files)) {
    const bytes = await readFile(path.join(readerDir, relative));
    if (gitBlob(bytes) !== expected) throw new Error(`reader blob mismatch: ${relative}`);
  }
  const sourceBytes = await readFile(parquetPath);
  if (digest('sha256', sourceBytes) !== PARQUET_SHA256) throw new Error('Parquet SHA-256 mismatch');
  const reader = await import(pathToFileURL(path.join(path.resolve(readerDir), 'src/node.js')).href);
  // Decode the very same bytes whose digest was checked; no mutable re-read.
  const file = { byteLength: sourceBytes.byteLength, slice(start, end) {
    const value = sourceBytes.subarray(start, end);
    return Promise.resolve(value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength));
  } };
  const rows = await reader.parquetReadObjects({ file, columns: COLUMNS });
  const result = { schema: 'research.topspin_private_transcode.v1',
    source_parquet_sha256: PARQUET_SHA256, reader_commit: manifest.commit, columns: COLUMNS, rows };
  const body = JSON.stringify(result, (_, value) => typeof value === 'bigint' ? value.toString() : value) + '\n';
  const destination = await open(outputPath, 'wx', 0o600);
  try { await destination.writeFile(body, { encoding: 'utf8' }); } finally { await destination.close(); }
  console.log(JSON.stringify({rows: rows.length, columns: COLUMNS.length, source_parquet_sha256: PARQUET_SHA256}));
}

await main();
