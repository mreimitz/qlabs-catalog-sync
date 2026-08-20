import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ThemeProvider } from "@elabs-ai/components-tokens";
import "./styles.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* defaultDensity="compact" per app-spec.md's taste dial: "a dense, operator-facing
        internal tool ... keeps more of that visible without scrolling". */}
    <ThemeProvider defaultTheme="light" defaultDensity="compact">
      <App />
    </ThemeProvider>
  </StrictMode>,
);
