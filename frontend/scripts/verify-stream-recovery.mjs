import assert from "node:assert/strict";
import { createServer } from "vite";

const server = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});

try {
  const { parseSseEvent, SseReplayCursor } = await server.ssrLoadModule("/src/api/sse.ts");
  const cursor = new SseReplayCursor();
  const started = parseSseEvent(
    'id: 10\nevent: run_started\ndata: {"run_id":"run_1"}\n\n',
  );
  const reset = parseSseEvent(
    'id: 11\nevent: model_output_reset\ndata: {"reason":"partial_stream"}\n\n',
  );

  assert.equal(cursor.accept(started), true);
  assert.equal(cursor.runId, "run_1");
  assert.equal(cursor.lastEventId, 10);
  assert.equal(cursor.accept(started), false, "replayed event must be deduplicated");
  assert.equal(cursor.accept(reset), true);
  assert.equal(reset.event, "model_output_reset");
  assert.equal(cursor.lastEventId, 11);
  const multiline = parseSseEvent(
    'id: 12\r\nevent: delta\r\ndata: {"text":\r\ndata: "继续"}\r\n\r\n',
  );
  assert.deepEqual(multiline.data, { text: "继续" });
  cursor.startAttempt();
  assert.equal(
    cursor.accept(parseSseEvent('event: delta\ndata: {"text":"无编号旧事件"}\n\n')),
    false,
    "id-less events must be ignored during replay",
  );
  assert.equal(parseSseEvent("event: delta\ndata: not-json\n\n"), null);
  assert.equal(parseSseEvent(": heartbeat\n\n"), null);

  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const encoder = new TextEncoder();
  const fetchCalls = [];
  const deltas = [];
  const connectionStates = [];
  let fetchIndex = 0;
  globalThis.window = globalThis;
  globalThis.fetch = async (url) => {
    fetchCalls.push(String(url));
    fetchIndex += 1;
    if (fetchIndex === 1) {
      return eventResponse([
        'id: 1\nevent: run_started\ndata: {"run_id":"run_retry"}\n\n',
        'id: 2\nevent: delta\ndata: {"text":"首段"}\n\n',
      ], encoder);
    }
    return eventResponse([
      'id: 2\nevent: delta\ndata: {"text":"重复段"}\n\n',
      'id: 3\nevent: error\ndata: {"detail":"模型执行失败","error_id":"err_test"}\n\n',
    ], encoder);
  };
  try {
    const { streamWorkspaceMessage } = await server.ssrLoadModule("/src/api/client.ts");
    await assert.rejects(
      streamWorkspaceMessage({
        view: "overview",
        message: "测试断流恢复",
        model: "glm-5.2",
        onDelta: (text) => deltas.push(text),
        onConnectionChange: (state) => connectionStates.push(state.status),
      }),
      /模型执行失败/,
    );
    assert.deepEqual(deltas, ["首段"], "replayed deltas must not be appended twice");
    assert.equal(fetchCalls.length, 2);
    assert.match(fetchCalls[1], /\/api\/conversation\/runs\/run_retry\/events\?after=2$/);
    assert.deepEqual(
      connectionStates,
      ["connecting", "streaming", "reconnecting", "streaming"],
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
} finally {
  await server.close();
}

console.log("SSE replay and reset verification passed.");

function eventResponse(frames, encoder) {
  return new Response(
    new ReadableStream({
      start(controller) {
        frames.forEach((frame) => controller.enqueue(encoder.encode(frame)));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}
