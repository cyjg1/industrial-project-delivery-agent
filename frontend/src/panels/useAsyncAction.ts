import { App } from "antd";
import { useCallback, useMemo, useRef, useState } from "react";

export type AsyncActionOptions<Args extends unknown[]> = {
  errorText: string;
  successText?: string;
  key?: (...args: Args) => string;
};

export type AsyncAction<Args extends unknown[]> = {
  run: (...args: Args) => Promise<boolean>;
  pending: boolean;
  pendingKey: string;
  isPending: (key: string) => boolean;
};

export function panelErrorText(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

// 统一保存路径：loading 状态 + 失败提示，避免接口拒绝时界面毫无反馈。
export function useAsyncAction<Args extends unknown[]>(
  action: (...args: Args) => Promise<void>,
  options: AsyncActionOptions<Args>,
): AsyncAction<Args> {
  const { message } = App.useApp();
  const actionRef = useRef(action);
  const optionsRef = useRef(options);
  actionRef.current = action;
  optionsRef.current = options;
  const [pending, setPending] = useState(false);
  const [pendingKey, setPendingKey] = useState("");

  const run = useCallback(
    async (...args: Args) => {
      const config = optionsRef.current;
      setPending(true);
      setPendingKey(config.key ? config.key(...args) : "");
      try {
        await actionRef.current(...args);
        if (config.successText) message.success(config.successText);
        return true;
      } catch (error) {
        message.error(panelErrorText(error, config.errorText));
        return false;
      } finally {
        setPending(false);
        setPendingKey("");
      }
    },
    [message],
  );

  const isPending = useCallback(
    (key: string) => pending && pendingKey === key,
    [pending, pendingKey],
  );

  return useMemo(
    () => ({ run, pending, pendingKey, isPending }),
    [isPending, pending, pendingKey, run],
  );
}
