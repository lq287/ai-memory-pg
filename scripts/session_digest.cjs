// 临时脚本：从 DSH 会话日志重建"这几天干了什么"。用完即删。
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const ROOT = path.join(process.env.USERPROFILE, '.dsh', 'sessions');
const SINCE = new Date(process.argv[2] || '2026-10-22T00:00:00');
const MAXLEN = Number(process.argv[3] || 200);

const MAGIC = 0xfd2fb528;

// DSH 把会话写成"多帧 zstd 拼接容器"，Node 的 zstdDecompress 只解第一帧，
// 所以按 RFC 8878 帧格式自己扫出每帧边界，再逐帧解压。
function scanFrames(buf) {
  const frames = [];
  let off = 0;
  while (off + 4 <= buf.length) {
    if (buf.readUInt32LE(off) !== MAGIC) throw new Error(`bad magic at ${off}`);
    const start = off;
    off += 4;
    const fhd = buf[off++];
    const fcsFlag = fhd >> 6;
    const singleSegment = (fhd >> 5) & 1;
    const checksum = (fhd >> 2) & 1;
    const dictFlag = fhd & 3;
    if (!singleSegment) off += 1;
    off += [0, 1, 2, 4][dictFlag];
    off += fcsFlag === 0 ? (singleSegment ? 1 : 0) : [0, 2, 4, 8][fcsFlag];
    for (;;) {
      const b0 = buf[off], b1 = buf[off + 1], b2 = buf[off + 2];
      off += 3;
      const last = b0 & 1;
      const type = (b0 >> 1) & 3;
      const size = (b0 >> 3) | (b1 << 5) | (b2 << 13);
      if (type === 0) off += size;
      else if (type === 1) off += 1;
      else if (type === 2) off += size;
      else throw new Error(`reserved block type at ${off}`);
      if (last) break;
    }
    if (checksum) off += 4;
    frames.push({ start, end: off });
  }
  return frames;
}

function readSession(p) {
  const buf = fs.readFileSync(p);
  return scanFrames(buf)
    .map(({ start, end }) => zlib.zstdDecompressSync(buf.subarray(start, end)).toString('utf8'))
    .join('');
}

function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (e.name.endsWith('.jsonl.zstd')) out.push(p);
  }
  return out;
}

const pad = (n) => String(n).padStart(2, '0');
function local(ms) {
  const d = new Date(ms);
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const files = walk(ROOT)
  .map((p) => ({ p, m: fs.statSync(p).mtime }))
  .filter((f) => f.m >= SINCE)
  .sort((a, b) => a.m - b.m);

for (const f of files) {
  let raw;
  try {
    raw = readSession(f.p);
  } catch (err) {
    console.log(`\n### ${f.p}\n  [解压失败] ${err.message}`);
    continue;
  }
  const recs = [];
  for (const line of raw.split('\n')) {
    if (!line) continue;
    try { recs.push(JSON.parse(line)); } catch { /* 跳过坏行 */ }
  }
  const proj = path.basename(path.dirname(path.dirname(f.p)))
    .replace(/~0020/g, ' ')
    .replace(/~([0-9A-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
  const titles = recs.filter((r) => r.type === 'session/title').map((r) => r.data?.title).filter(Boolean);
  const users = recs.filter((r) => r.type === 'user/message');
  const asst = recs.filter((r) => r.type === 'assistant/message').length;
  const tools = recs.filter((r) => r.type === 'tool/call').length;
  const first = users.find((r) => r.time)?.time;
  const last = [...users].reverse().find((r) => r.time)?.time;

  console.log(`\n${'='.repeat(78)}`);
  console.log(`### ${proj}   ${first ? local(first) : '?'} → ${last ? local(last) : '?'}`);
  if (titles.length) console.log(`    标题: ${[...new Set(titles)].join(' / ').slice(0, 160)}`);
  console.log(`    消息: 用户 ${users.length} / 助手 ${asst} / 工具调用 ${tools}`);
  console.log('='.repeat(78));
  for (const r of users) {
    const src = r.data?.source?.kind;
    const text = (r.data?.content || [])
      .filter((c) => c.type === 'text')
      .map((c) => c.text)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (!text) continue;
    if (src && src !== 'user') continue; // 系统注入的上下文/提醒不算用户提问
    if (/^<system-reminder|^Instructions from/.test(text)) continue;
    console.log(`  ${local(r.time)} · ${text.slice(0, MAXLEN)}`);
  }
}
