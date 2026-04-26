#!/usr/bin/env node

/**
 * UserPromptSubmit Hook - Implementation Session Detector
 *
 * 実装系コマンド / スキル起動を検出して、learned patterns 注入のゲート情報を保存する。
 * file-specific findings は常時有効のまま、review-patterns.db 由来の learned patterns のみ
 * implementation 文脈で段階的に解放するためのメタデータを作る。
 */

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const STATE_PATH = process.env.IMPLEMENTATION_SESSION_PATH || path.join(
  process.env.USERPROFILE || process.env.HOME,
  ".claude",
  "hooks",
  "implementation-session.json"
);

const IMPLEMENTATION_MARKERS = [
  "/review-fix-loop",
  "/rfl",
  "/iterative-fix",
  "/ui-fix",
  "sc-rfl",
  "sc-review-fix-loop",
  "sc-ui",
  "sc-frontend-implementation",
  "sc-tdd",
  "sc-e2e",
  "sc-bt",
  "sc-at",
];

function detectImplementationPrompt(text) {
  if (!text) return [];
  return IMPLEMENTATION_MARKERS.filter((marker) => text.includes(marker));
}

function normalizePath(value) {
  return String(value || "").replace(/\\/g, "/").replace(/\/+$/, "");
}

function detectRepoRootByTraversal(cwd) {
  let current = path.resolve(cwd);
  while (true) {
    if (fs.existsSync(path.join(current, ".git"))) {
      return normalizePath(current);
    }
    const parent = path.dirname(current);
    if (parent === current) {
      return "";
    }
    current = parent;
  }
}

function detectRepoRoot(cwd) {
  if (!cwd) return "";
  try {
    return normalizePath(
      execFileSync("git", ["-C", cwd, "rev-parse", "--show-toplevel"], {
        encoding: "utf8",
        timeout: 3000,
      }).trim()
    );
  } catch {
    try {
      return normalizePath(
        execFileSync("git", ["rev-parse", "--show-toplevel"], {
          cwd,
          encoding: "utf8",
          timeout: 3000,
        }).trim()
      );
    } catch {
      return detectRepoRootByTraversal(cwd);
    }
  }
}

function main() {
  let inputData = "";
  try {
    inputData = fs.readFileSync(0, "utf8");
  } catch {
    process.exit(0);
  }

  let parsed;
  try {
    parsed = JSON.parse(inputData);
  } catch {
    process.exit(0);
  }

  const userPrompt = String(parsed.user_prompt || parsed.prompt || parsed.input || "");
  const matchedMarkers = detectImplementationPrompt(userPrompt);
  if (matchedMarkers.length === 0) {
    process.exit(0);
  }

  const cwd = normalizePath(parsed.cwd || process.cwd());
  const metadata = {
    detected_at: new Date().toISOString(),
    session_id: parsed.session_id || parsed.sessionId || "",
    cwd,
    repo_root: detectRepoRoot(cwd),
    matched_markers: matchedMarkers,
  };

  try {
    fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
    fs.writeFileSync(STATE_PATH, JSON.stringify(metadata, null, 2), "utf8");
  } catch {
    // 状態保存失敗でも通常動作を妨げない
  }

  process.exit(0);
}

main();
