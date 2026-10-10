/**
 * index.js — Main entry point for the browser upload application.
 * Renders the App component into the #root element.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

const rootEl = document.getElementById("root");
const root = createRoot(rootEl);
root.render(<App />);
