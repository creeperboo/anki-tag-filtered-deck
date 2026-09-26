// 用法：node 工具/fetch.mjs <url> <输出文件>
// 本机 curl / Invoke-WebRequest 不可用，统一走 Node 的 fetch。

import { writeFile } from "node:fs/promises";

for (const key of ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]) {
  delete process.env[key];
}

const [url, out] = process.argv.slice(2);
if (!url || !out) {
  console.error("用法：node fetch.mjs <url> <输出文件>");
  process.exit(2);
}

const response = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
if (!response.ok) {
  console.error(`HTTP ${response.status} ${response.statusText} <- ${url}`);
  process.exit(1);
}
const body = Buffer.from(await response.arrayBuffer());
await writeFile(out, body);
console.log(`已保存 ${out}（${body.length} 字节）`);
