import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "antd/dist/reset.css";
import "./styles.css";
import "./fixed-viewport.css";

// Build-time opt-in: ordinary development and production never show sample branding.
if (import.meta.env.VITE_SAMPLE_MODE === "true") {
  document.title = "合成样例 · POY/DTY";
  const notice = document.createElement("div");
  notice.setAttribute("role", "status");
  notice.textContent = "合成样例 · 非真实行情 · 无模型调用 / Synthetic sample";
  Object.assign(notice.style, { position: "fixed", bottom: "4px", left: "280px", zIndex: "99999", padding: "6px 12px", background: "#fff7ed", color: "#9a3412", border: "1px solid #fdba74", borderRadius: "6px", fontSize: "13px" });
  document.body.appendChild(notice);
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
