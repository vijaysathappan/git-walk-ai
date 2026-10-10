/**
 * taskpane.jsx — Entry point for the Office.js embedded side panel.
 *
 * Initializes inside Office.onReady() and renders the TaskpaneUI
 * component only when the host is confirmed as Excel.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import TaskpaneUI from "./components/TaskpaneUI";

// ── Error fallback for non-Office environments ──────────────────────────
function OfficeNotAvailable() {
  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: "32px",
        background: "linear-gradient(180deg, #0f1219 0%, #131927 100%)",
        textAlign: "center",
      }}
    >
      <div
        style={{
          width: "64px",
          height: "64px",
          borderRadius: "16px",
          background: "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: "28px",
          marginBottom: "20px",
          boxShadow: "0 4px 20px rgba(245, 158, 11, 0.3)",
        }}
      >
        ⚠
      </div>
      <h2
        style={{
          fontSize: "18px",
          fontWeight: 700,
          color: "#e2e8f0",
          marginBottom: "10px",
        }}
      >
        Office.js Not Detected
      </h2>
      <p
        style={{
          fontSize: "13px",
          color: "#94a3b8",
          maxWidth: "320px",
          lineHeight: 1.7,
        }}
      >
        This panel is designed to run inside Microsoft Excel as an Office
        Web Add-in. Please open the{" "}
        <strong style={{ color: "#8b5cf6" }}>configured .xlsx file</strong>{" "}
        in Excel Desktop to use this sync panel.
      </p>
    </div>
  );
}

// ── Office.js Bootstrap ─────────────────────────────────────────────────
const rootEl = document.getElementById("taskpane-root");
const root = createRoot(rootEl);

if (typeof Office !== "undefined" && Office.onReady) {
  Office.onReady((info) => {
    if (info.host === Office.HostType.Excel) {
      root.render(<TaskpaneUI />);
    } else {
      root.render(<OfficeNotAvailable />);
    }
  });
} else {
  // Fallback: not inside Office context (e.g. opened in browser for dev)
  root.render(<OfficeNotAvailable />);
}
