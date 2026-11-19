import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ApiClient, apiBaseFromLocation, tokenFromHash } from "./api/client";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");
// Read the access token once, then drop it from the address bar and history.
const token = tokenFromHash(window.location.hash);
if (window.location.hash) history.replaceState(null, "", window.location.pathname + window.location.search);
const client = new ApiClient(apiBaseFromLocation(window.location.search), undefined, token);
createRoot(root).render(
  <StrictMode>
    <App client={client} />
  </StrictMode>,
);
