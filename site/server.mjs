// Serves the MeetingScribe homepage on localhost. Cloudflare Tunnel (applyai)
// routes meetingscribe.preetbohra.com here. Byte ranges are supported so the
// demo video streams and seeks instead of downloading in one go.
import { createServer } from "node:http";
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const dir = dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PORT ?? 3070);

// Only these files are public; everything else in the folder (this script, the
// PowerShell helpers) stays private.
const files = {
  "/": ["index.html", "text/html; charset=utf-8", "public, max-age=300"],
  "/index.html": ["index.html", "text/html; charset=utf-8", "public, max-age=300"],
  "/icon.svg": ["icon.svg", "image/svg+xml", "public, max-age=86400"],
  "/poster.jpg": ["poster.jpg", "image/jpeg", "public, max-age=86400"],
  "/demo.mp4": ["demo.mp4", "video/mp4", "public, max-age=86400"],
};

createServer(async (req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { Allow: "GET, HEAD" }).end();
    return;
  }
  const entry = files[new URL(req.url, "http://x").pathname];
  if (!entry) {
    res.writeHead(404, { "Content-Type": "text/plain" }).end("Not found");
    return;
  }
  const [name, type, cache] = entry;
  const path = join(dir, name);
  let size;
  try {
    size = (await stat(path)).size;
  } catch {
    res.writeHead(500, { "Content-Type": "text/plain" }).end("File missing");
    return;
  }

  const headers = { "Content-Type": type, "Cache-Control": cache, "Accept-Ranges": "bytes" };
  const range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? "");
  let start = 0;
  let end = size - 1;
  if (range && (range[1] || range[2])) {
    if (range[1]) {
      start = Number(range[1]);
      if (range[2]) end = Math.min(Number(range[2]), size - 1);
    } else {
      start = Math.max(0, size - Number(range[2])); // suffix range: the last N bytes
    }
    if (start > end || start >= size) {
      res.writeHead(416, { "Content-Range": `bytes */${size}` }).end();
      return;
    }
    res.writeHead(206, { ...headers, "Content-Range": `bytes ${start}-${end}/${size}`, "Content-Length": end - start + 1 });
  } else {
    res.writeHead(200, { ...headers, "Content-Length": size });
  }
  if (req.method === "HEAD") {
    res.end();
    return;
  }
  createReadStream(path, { start, end }).on("error", () => res.destroy()).pipe(res);
}).listen(port, "127.0.0.1", () => {
  console.log(`[meetingscribe] homepage on http://127.0.0.1:${port}`);
});
