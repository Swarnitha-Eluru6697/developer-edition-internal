import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles/global.css";
import "./components/pralai-dark.css";
import "./styles/dark.css";
// Eager: sets <html data-theme> from the shell before first paint.
import "./components/DeveloperUserMenu";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
