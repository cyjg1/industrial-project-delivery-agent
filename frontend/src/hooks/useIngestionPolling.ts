import { useEffect } from "react";
import type { Workspace } from "../types";

type IngestionPollingOptions = {
  workspace: Workspace | null;
  actorId: string;
  refresh: () => Promise<void>;
  onError: (message: string) => void;
};

export function useIngestionPolling({ workspace, actorId, refresh, onError }: IngestionPollingOptions) {
  const activeIngestionJobKey = (workspace?.inputs.ingestion_jobs || [])
    .filter((job) => job.status === "queued" || job.status === "running")
    .map((job) => job.id)
    .sort()
    .join("|");

  useEffect(() => {
    if (!activeIngestionJobKey) return undefined;
    let requestRunning = false;
    const timer = window.setInterval(async () => {
      if (requestRunning) return;
      requestRunning = true;
      try {
        await refresh();
      } catch (reason) {
        onError(reason instanceof Error ? reason.message : "入库进度刷新失败");
      } finally {
        requestRunning = false;
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeIngestionJobKey, actorId]);
}
