// Revision-checked source text. Text is keyed by its own SHA-256, computed
// here, so a line is shown as "the analyzed code" only when the bytes match
// the hash in the report — never on path alone.

import type { SourceFile } from "./types";

export interface SourceText {
  name: string;
  sha256: string;
  text: string;
}

export type SourceState =
  | { kind: "verified"; text: SourceText }
  | { kind: "mismatch"; supplied: SourceText }
  | { kind: "unavailable" };

export async function sha256Hex(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new Error("SHA-256 is unavailable in this browser");
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function makeSourceText(name: string, text: string): Promise<SourceText> {
  return { name, text, sha256: await sha256Hex(text) };
}

function baseName(path: string): string {
  return path.split("/").pop() ?? path;
}

export function resolveSource(file: SourceFile, available: readonly SourceText[]): SourceState {
  const exact = available.find((s) => s.sha256 === file.sha256);
  if (exact) return { kind: "verified", text: exact };
  const sameName = available.find((s) => baseName(s.name) === baseName(file.path));
  if (sameName) return { kind: "mismatch", supplied: sameName };
  return { kind: "unavailable" };
}
