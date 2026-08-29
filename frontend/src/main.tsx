import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { RouteApp } from "./RouteApp";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode><RouteApp /></StrictMode>,
);
