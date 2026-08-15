import { useCallback, useMemo, useRef, useState } from "react";
import type { ThreeListPayload } from "../api/client";
import { useAsyncAction } from "./useAsyncAction";

export type RowEditor<Row> = {
  editingId: string;
  rowId: (row: Row) => string;
  isEditing: (row: Row) => boolean;
  isSaving: (row: Row) => boolean;
  draft: (row: Row) => ThreeListPayload;
  begin: (row: Row) => void;
  cancel: () => void;
  update: (row: Row, patch: ThreeListPayload) => void;
  save: (row: Row) => void;
};

// 行内编辑的公共状态机：三清单三张表共用同一套 editing / draft / save 逻辑。
export function useRowEditor<Row>(
  idKey: keyof Row,
  updater: (id: string, payload: ThreeListPayload) => Promise<void>,
  errorText = "保存失败",
): RowEditor<Row> {
  const [editingId, setEditingId] = useState("");
  const [drafts, setDrafts] = useState<Record<string, ThreeListPayload>>({});
  const draftsRef = useRef(drafts);
  draftsRef.current = drafts;

  const action = useAsyncAction(
    async (id: string, payload: ThreeListPayload) => {
      await updater(id, payload);
    },
    { errorText, successText: "已保存", key: (id) => id },
  );

  const rowId = useCallback((row: Row) => String(row[idKey]), [idKey]);

  const dropDraft = useCallback((id: string) => {
    setDrafts((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

  const save = useCallback(
    async (row: Row) => {
      const id = rowId(row);
      const ok = await action.run(id, draftsRef.current[id] || {});
      if (!ok) return;
      setEditingId("");
      dropDraft(id);
    },
    [action, dropDraft, rowId],
  );

  return useMemo<RowEditor<Row>>(
    () => ({
      editingId,
      rowId,
      isEditing: (row) => editingId === rowId(row),
      isSaving: (row) => action.isPending(rowId(row)),
      draft: (row) => drafts[rowId(row)] || {},
      begin: (row) => setEditingId(rowId(row)),
      cancel: () => {
        if (editingId) dropDraft(editingId);
        setEditingId("");
      },
      update: (row, patch) => {
        const id = rowId(row);
        setDrafts((current) => ({ ...current, [id]: { ...current[id], ...patch } }));
      },
      save: (row) => {
        void save(row);
      },
    }),
    [action, dropDraft, drafts, editingId, rowId, save],
  );
}
