import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const frontendUrl = process.env.FRONTEND_URL || "http://127.0.0.1:5174/";
const chromeBin = process.env.CHROME_BIN || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const cdpPort = Number(process.env.CDP_PORT || 9225);
const outputDir = process.env.UI_SCREENSHOT_DIR || join(tmpdir(), "project-agent-ui-verification");
const profileDir = await mkdtemp(join(tmpdir(), "project-agent-ui-chrome-"));

await mkdir(outputDir, { recursive: true });
const chrome = spawn(
  chromeBin,
  [
    "--headless=new",
    "--disable-extensions",
    "--disable-gpu",
    "--no-sandbox",
    `--remote-debugging-port=${cdpPort}`,
    `--user-data-dir=${profileDir}`,
    frontendUrl,
  ],
  { stdio: "ignore" },
);
const chromeExited = new Promise((resolve) => chrome.once("exit", resolve));

let client;
try {
  const page = await waitForPage(cdpPort, frontendUrl);
  client = await connectCdp(page.webSocketDebuggerUrl);
  await client.send("Page.enable");
  await client.send("Runtime.enable");

  const desktop = await verifyViewport(client, {
    label: "desktop",
    width: 1440,
    height: 900,
    mobile: false,
  });
  const mobile = await verifyViewport(client, {
    label: "mobile",
    width: 390,
    height: 844,
    mobile: true,
  });

  process.stdout.write(`${JSON.stringify({ frontendUrl, outputDir, desktop, mobile }, null, 2)}\n`);
} finally {
  client?.close();
  chrome.kill("SIGTERM");
  await Promise.race([chromeExited, delay(3_000)]);
  await rm(profileDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}

async function verifyViewport(cdp, viewport) {
  const previousTimeOrigin = await evaluate(cdp, "performance.timeOrigin");
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: viewport.width,
    height: viewport.height,
    deviceScaleFactor: 1,
    mobile: viewport.mobile,
  });
  await cdp.send("Page.reload", { ignoreCache: true });
  await waitForExpression(
    cdp,
    `document.readyState === 'complete' && performance.timeOrigin !== ${previousTimeOrigin}`,
  );
  await waitForExpression(cdp, "Boolean(document.querySelector('.agent-tabs'))");

  const clicked = await evaluate(
    cdp,
    `(() => {
      const tab = [...document.querySelectorAll('.ant-tabs-tab')]
        .find((node) => node.textContent?.includes('待确认'));
      if (!tab) return false;
      tab.click();
      return true;
    })()`,
  );
  if (!clicked) throw new Error(`${viewport.label}: 待确认页签不存在或不可点击`);
  await waitForExpression(cdp, "Boolean(document.querySelector('.ingestion-status-band'))");

  const metrics = await evaluate(
    cdp,
    `(() => {
      const root = document.documentElement;
      const band = document.querySelector('.ingestion-status-band');
      const row = document.querySelector('.ingestion-job-row');
      const progress = document.querySelector('.ingestion-job-row .ant-progress');
      const jobTitle = document.querySelector('.ingestion-job-title strong')?.textContent?.trim() || '';
      const jobStatus = document.querySelector('.ingestion-job-title .ant-tag')?.textContent?.trim() || '';
      const rect = band?.getBoundingClientRect();
      return {
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
        documentWidth: root.scrollWidth,
        horizontalOverflow: root.scrollWidth > window.innerWidth + 1,
        bandVisible: Boolean(rect && rect.width > 0 && rect.height > 0),
        bandLeft: rect ? Math.round(rect.left) : null,
        bandRight: rect ? Math.round(rect.right) : null,
        rowVisible: Boolean(row && row.getBoundingClientRect().height > 0),
        progressVisible: Boolean(progress && progress.getBoundingClientRect().height > 0),
        jobTitle,
        jobStatus,
        showsPersistedTitle: Boolean(jobTitle),
        showsPersistedStatus: ['排队中', '处理中', '已完成', '失败', '已中断'].includes(jobStatus),
      };
    })()`,
  );

  if (metrics.horizontalOverflow) {
    throw new Error(`${viewport.label}: 页面横向溢出 ${metrics.documentWidth}px > ${metrics.viewportWidth}px`);
  }
  if (!metrics.bandVisible || !metrics.rowVisible || !metrics.progressVisible) {
    throw new Error(`${viewport.label}: 入库状态、作业行或真实进度条不可见`);
  }
  if (metrics.bandLeft < 0 || metrics.bandRight > viewport.width + 1) {
    throw new Error(`${viewport.label}: 入库状态条超出视口 ${metrics.bandLeft}..${metrics.bandRight}`);
  }
  if (!metrics.showsPersistedTitle || !metrics.showsPersistedStatus) {
    throw new Error(`${viewport.label}: 未渲染后端持久化作业标题或状态`);
  }

  const screenshot = await cdp.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  const screenshotPath = join(outputDir, `${viewport.label}.png`);
  await writeFile(screenshotPath, Buffer.from(screenshot.data, "base64"));
  return { ...metrics, screenshotPath };
}

async function waitForPage(port, targetUrl) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    try {
      const pages = await fetch(`http://127.0.0.1:${port}/json`).then((response) => response.json());
      const page = pages.find((entry) => entry.type === "page" && entry.url.startsWith(targetUrl));
      if (page) return page;
    } catch {
      // Chrome may need a few hundred milliseconds before exposing the CDP endpoint.
    }
    await delay(200);
  }
  throw new Error(`Chrome CDP did not expose ${targetUrl}`);
}

async function waitForExpression(cdp, expression) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (await evaluate(cdp, expression)) return;
    await delay(100);
  }
  throw new Error(`Timed out waiting for browser expression: ${expression}`);
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "Browser evaluation failed");
  }
  return result.result.value;
}

async function connectCdp(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result || {});
  });
  return {
    send(method, params = {}) {
      const id = ++sequence;
      socket.send(JSON.stringify({ id, method, params }));
      return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
    },
    close() {
      socket.close();
    },
  };
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
