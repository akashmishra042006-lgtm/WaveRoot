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

// Browser geometry/painting are verified by visual_check.py in real Chrome.
// jsdom supplies neither ResizeObserver nor a Canvas rendering context.
globalThis.ResizeObserver = class ResizeObserver {
  observe() {} unobserve() {} disconnect() {}
};
HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
