import "@testing-library/jest-dom/vitest";
import { webcrypto } from "node:crypto";

// jsdom lacks SubtleCrypto; the viewer needs it for revision checks.
if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto, configurable: true });
}

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest globals are off, so Testing Library's automatic cleanup is wired here.
afterEach(() => cleanup());
