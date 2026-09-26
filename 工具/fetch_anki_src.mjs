// 抓取 Anki 26.09.3 的相关源码片段到临时目录，供开发时核对接口。
// 用法：node 工具/fetch_anki_src.mjs <输出目录>

import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

for (const key of ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]) {
  delete process.env[key];
}

const TAG = "26.09.3";
const BASE = `https://raw.githubusercontent.com/ankitects/anki/${TAG}/`;
const outDir = process.argv[2];
if (!outDir) {
  console.error("用法：node fetch_anki_src.mjs <输出目录>");
  process.exit(2);
}

const FILES = [
  "qt/aqt/gui_hooks.py",
  "qt/aqt/deckbrowser.py",
  "qt/aqt/decks.py",
  "qt/aqt/overview.py",
  "qt/aqt/main.py",
  "qt/aqt/browser/sidebar/tag_tree.py",
  "qt/aqt/browser/sidebar/tag_item.py",
  "qt/aqt/browser/sidebar/tree.py",
  "qt/aqt/browser/sidebar/item.py",
  "qt/aqt/operations/scheduling.py",
  "qt/aqt/operations/deck.py",
  "pylib/anki/scheduler/base.py",
  "pylib/anki/decks.py",
  "pylib/anki/collection.py",
  "rslib/src/scheduler/filtered/mod.rs",
  "rslib/src/decks/mod.rs",
];

await mkdir(outDir, { recursive: true });
for (const rel of FILES) {
  const target = join(outDir, rel.replaceAll("/", "__"));
  try {
    const response = await fetch(BASE + rel, { headers: { "User-Agent": "Mozilla/5.0" } });
    if (!response.ok) {
      console.log(`跳过 ${rel}（HTTP ${response.status}）`);
      continue;
    }
    const text = await response.text();
    await writeFile(target, text);
    console.log(`已保存 ${rel}（${text.length} 字符）`);
  } catch (error) {
    console.log(`失败 ${rel}：${error.message}`);
  }
}
