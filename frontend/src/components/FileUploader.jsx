/**
 * FileUploader — Drag-and-drop & click-to-select .xlsx upload component.
 *
 * Features:
 *   • Animated drop zone with glassmorphism
 *   • File type validation (.xlsx only)
 *   • Drag-over visual feedback
 *   • File size display
 *   • Upload progress states
 */
import React, { useState, useRef, useCallback } from "react";

const MAX_FILE_SIZE_MB = 50;

export default function FileUploader({ onFileSelected, disabled = false }) {
  const [isDragOver, setIsDragOver] = useState(false);
  const [selectedFile, setSelectedFile] = useState(null);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  const validateFile = useCallback((file) => {
    if (!file) return "No file selected.";
    if (!file.name.toLowerCase().endsWith(".xlsx")) {
      return "Only .xlsx files are supported.";
    }
    if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      return `File exceeds ${MAX_FILE_SIZE_MB}MB limit.`;
    }
    return null;
  }, []);

  const handleFile = useCallback(
    (file) => {
      const validationError = validateFile(file);
      if (validationError) {
        setError(validationError);
        setSelectedFile(null);
        return;
      }
      setError(null);
      setSelectedFile(file);
      onFileSelected?.(file);
    },
    [onFileSelected, validateFile]
  );

  const handleDrop = useCallback(
    (e) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragOver(false);
      const file = e.dataTransfer.files[0];
      handleFile(file);
    },
    [handleFile]
  );

  const handleDragOver = useCallback((e) => {
    e.preventDefault();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e) => {
    e.preventDefault();
    setIsDragOver(false);
  }, []);

  const handleClick = () => {
    if (!disabled) inputRef.current?.click();
  };

  const handleInputChange = (e) => {
    const file = e.target.files[0];
    if (file) handleFile(file);
  };

  const formatSize = (bytes) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  };

  return (
    <div style={{ width: "100%" }}>
      {/* Hidden file input */}
      <input
        ref={inputRef}
        type="file"
        accept=".xlsx"
        onChange={handleInputChange}
        style={{ display: "none" }}
        id="file-upload-input"
      />

      {/* Drop zone */}
      <div
        onClick={handleClick}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        id="file-drop-zone"
        style={{
          position: "relative",
          border: `2px dashed ${
            isDragOver
              ? "#6366f1"
              : error
              ? "#ef4444"
              : selectedFile
              ? "#10b981"
              : "#334155"
          }`,
          borderRadius: "16px",
          padding: "48px 32px",
          textAlign: "center",
          cursor: disabled ? "not-allowed" : "pointer",
          opacity: disabled ? 0.5 : 1,
          background: isDragOver
            ? "rgba(99, 102, 241, 0.08)"
            : "rgba(15, 23, 42, 0.6)",
          backdropFilter: "blur(12px)",
          transition: "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)",
          transform: isDragOver ? "scale(1.02)" : "scale(1)",
        }}
      >
        {/* Animated background gradient */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            borderRadius: "14px",
            background: isDragOver
              ? "radial-gradient(ellipse at center, rgba(99,102,241,0.15) 0%, transparent 70%)"
              : "none",
            pointerEvents: "none",
            transition: "all 0.3s ease",
          }}
        />

        {/* Icon */}
        <div
          style={{
            fontSize: "48px",
            marginBottom: "16px",
            filter: isDragOver ? "drop-shadow(0 0 12px #6366f1)" : "none",
            transition: "filter 0.3s ease",
          }}
        >
          {selectedFile ? "📊" : "📁"}
        </div>

        {/* Label */}
        {selectedFile ? (
          <div>
            <div
              style={{
                fontSize: "16px",
                fontWeight: 600,
                color: "#e2e8f0",
                marginBottom: "6px",
              }}
            >
              {selectedFile.name}
            </div>
            <div
              style={{
                fontSize: "13px",
                color: "#94a3b8",
              }}
            >
              {formatSize(selectedFile.size)} — Ready to upload
            </div>
          </div>
        ) : (
          <div>
            <div
              style={{
                fontSize: "16px",
                fontWeight: 600,
                color: "#e2e8f0",
                marginBottom: "6px",
              }}
            >
              Drop your <span style={{ color: "#6366f1" }}>.xlsx</span> file
              here
            </div>
            <div style={{ fontSize: "13px", color: "#94a3b8" }}>
              or click to browse • Max {MAX_FILE_SIZE_MB}MB
            </div>
          </div>
        )}
      </div>

      {/* Error message */}
      {error && (
        <div
          style={{
            marginTop: "10px",
            padding: "10px 14px",
            borderRadius: "8px",
            background: "rgba(239, 68, 68, 0.1)",
            border: "1px solid rgba(239, 68, 68, 0.3)",
            color: "#f87171",
            fontSize: "13px",
            fontWeight: 500,
          }}
          role="alert"
          id="file-upload-error"
        >
          ⚠ {error}
        </div>
      )}
    </div>
  );
}
