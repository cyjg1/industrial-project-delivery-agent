export type ParsedSseEvent = {
  id: number | null;
  event: string;
  data: unknown;
};

export class SseReplayCursor {
  runId = "";
  lastEventId = 0;
  replaying = false;

  startAttempt(): void {
    this.replaying = Boolean(this.runId);
  }

  accept(event: ParsedSseEvent): boolean {
    if (event.id === null) {
      if (this.replaying) return false;
    } else {
      if (event.id <= this.lastEventId) return false;
      this.lastEventId = event.id;
    }
    if (event.event === "run_started") {
      this.runId = String((event.data as { run_id?: string }).run_id || this.runId);
    }
    return true;
  }
}

export function parseSseEvent(text: string): ParsedSseEvent | null {
  let event = "";
  let id: number | null = null;
  const dataLines: string[] = [];
  for (const rawLine of text.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("id:")) {
      const parsedId = Number(line.slice("id:".length).trim());
      id = Number.isFinite(parsedId) ? parsedId : null;
    } else if (line.startsWith("data:")) {
      dataLines.push(stripFieldSpace(line.slice("data:".length)));
    }
  }
  if (!event || !dataLines.length) return null;
  try {
    return { id, event, data: JSON.parse(dataLines.join("\n")) as unknown };
  } catch {
    return null;
  }
}

function stripFieldSpace(value: string) {
  return value.startsWith(" ") ? value.slice(1) : value;
}
