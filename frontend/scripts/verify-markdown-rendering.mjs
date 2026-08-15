import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const markdown = [
  "## Project status",
  "",
  "1. First **risk**",
  "2. Second task",
  "",
  "| Item | Owner |",
  "|---|---|",
  "| Standard layer | PM |",
  "",
  "> Evidence comes from the project store.",
  "",
  "```bash",
  "npm run build",
  "```",
  "",
  "[Source](https://example.com)",
  "",
  "<script>alert('xss')</script>",
].join("\n");

const server = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});

try {
  const { renderAgentMessage } = await server.ssrLoadModule("/src/chat/AgentBubble.tsx");
  const html = renderToStaticMarkup(renderAgentMessage(markdown));

  assert.match(html, /class="markdown-body"/);
  assert.match(html, /<h2>Project status<\/h2>/);
  assert.match(html, /<ol>/);
  assert.match(html, /<strong>risk<\/strong>/);
  assert.match(html, /<table>/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /<pre><code class="language-bash">npm run build/);
  assert.match(html, /<a href="https:\/\/example\.com" target="_blank" rel="noopener noreferrer">Source<\/a>/);
  assert.doesNotMatch(html, /<script>/);
} finally {
  await server.close();
}

console.log("Markdown renderer verification passed.");
