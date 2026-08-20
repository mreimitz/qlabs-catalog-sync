import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import "./styles.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider defaultTheme="light">
      <App />
    </ThemeProvider>
  </StrictMode>,
);
