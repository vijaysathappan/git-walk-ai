import React from "react";

const STATUS_CONFIG = {
  idle: { label: "Idle", color: "#70858f", background: "rgba(112,133,143,.1)", pulse: false },
  connected: { label: "Connected", color: "#75ead0", background: "rgba(117,234,208,.09)", pulse: true },
  syncing: { label: "Syncing", color: "#f4ba62", background: "rgba(244,186,98,.09)", pulse: true },
  synced: { label: "Committed to SQLite", color: "#75ead0", background: "rgba(117,234,208,.09)", pulse: false },
  error: { label: "Sync error", color: "#ff7f75", background: "rgba(255,127,117,.09)", pulse: true },
};

export default function StatusBanner({ status = "idle", message }) {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.idle;
  return (
    <>
      <style>{`
        @keyframes livePulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: .45; transform: scale(.72); }
        }
      `}</style>
      <div
        role="status"
        aria-live="polite"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "10px 13px",
          border: `1px solid ${config.color}45`,
          borderRadius: 10,
          color: config.color,
          background: config.background,
          fontSize: 12,
          fontWeight: 700,
        }}
      >
        <span
          style={{
            width: 8,
            height: 8,
            flexShrink: 0,
            borderRadius: "50%",
            background: config.color,
            boxShadow: `0 0 12px ${config.color}80`,
            animation: config.pulse ? "livePulse 1.4s ease-in-out infinite" : "none",
          }}
        />
        {message || config.label}
      </div>
    </>
  );
}
