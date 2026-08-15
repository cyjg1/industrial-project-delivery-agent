import React from "react";
import ReactDOM from "react-dom/client";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import "antd/dist/reset.css";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { antdTheme } from "./theme";
import "./styles.css";
import "./ingestion.css";

dayjs.locale("zh-cn");

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ConfigProvider theme={antdTheme} locale={zhCN}>
      <AntdApp>
        <ErrorBoundary scope="应用根节点" title="应用启动失败" onReset={() => window.location.reload()}>
          <App />
        </ErrorBoundary>
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>
);
