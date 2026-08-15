import { useCallback, useMemo, useState } from "react";

import type { ConversationModelCatalog } from "../types";


const STORAGE_KEY = "project-agent-conversation-model";


export function useConversationModelSelection() {
  const [catalog, setCatalog] = useState<ConversationModelCatalog | null>(null);
  const [selectedModel, setSelectedModel] = useState(
    () => window.localStorage.getItem(STORAGE_KEY) || "",
  );

  const applyCatalog = useCallback((nextCatalog: ConversationModelCatalog) => {
    setCatalog(nextCatalog);
    setSelectedModel((current) => {
      const allowed = new Set(nextCatalog.models.map((model) => model.id));
      const nextModel = allowed.has(current) ? current : nextCatalog.default_model;
      window.localStorage.setItem(STORAGE_KEY, nextModel);
      return nextModel;
    });
  }, []);

  const selectModel = useCallback((model: string) => {
    setSelectedModel(model);
    window.localStorage.setItem(STORAGE_KEY, model);
  }, []);

  const modelOptions = useMemo(
    () => (catalog?.models || []).map((model) => ({
      label: model.label,
      value: model.id,
    })),
    [catalog],
  );

  return {
    applyCatalog,
    modelOptions,
    selectedModel,
    selectModel,
  };
}
