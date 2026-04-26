#!/usr/bin/env node

/**
 * Review Feedback Session Check (SessionStart Hook)
 *
 * セッション開始時に pending findings / learned-pattern signal を additionalContext として返す。
 * repo 内の hook として完結させるため、summary script は __dirname 基準で解決する。
 */

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const REPO_ROOT = path.resolve(__dirname, "..");
const SCRIPT_PATH = process.env.REVIEW_FEEDBACK_SCRIPT || path.join(
  process.env.USERPROFILE || process.env.HOME,
  ".claude",
  "scripts",
  "review-feedback.py"
);
const GLM_SUMMARY_SCRIPT_PATH = path.join(
  REPO_ROOT,
  "scripts",
  "summarize-glm-fallbacks.py"
);
const LEARNED_PATTERN_SUMMARY_SCRIPT_PATH = path.join(
  REPO_ROOT,
  "scripts",
  "summarize-learned-pattern-injections.py"
);

function normalizePath(value) {
  return String(value || "").replace(/\\/g, "/").replace(/\/+$/, "");
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
    return "";
  }
}

function parseJsonOrNull(raw, label) {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (e) {
    process.stderr.write(`[review-feedback-session-check] ${label} parse error: ${e.message} | raw: ${String(raw).slice(0, 200)}\n`);
    return null;
  }
}

function runPython(scriptPath, args = []) {
  return execFileSync("python", [scriptPath, ...args], {
    encoding: "utf8",
    timeout: 5000,
  }).trim();
}

function main() {
  let inputData = "";
  try {
    inputData = fs.readFileSync(0, "utf8");
  } catch {
    inputData = "";
  }

  const hookData = parseJsonOrNull(inputData, "stdin") || {};
  const cwd = hookData.cwd || process.cwd();
  const repoRoot = detectRepoRoot(cwd);
  const contextParts = [];

  try {
    const queryArgs = ["query", "--resolution", "pending", "--limit", "5"];
    if (repoRoot) {
      queryArgs.splice(3, 0, "--repo-root", repoRoot);
    }
    const result = runPython(SCRIPT_PATH, queryArgs);

    if (result && result !== "[]") {
      const findings = parseJsonOrNull(result, "pending findings") || [];
      if (Array.isArray(findings) && findings.length > 0) {
        const critical = findings.filter(f => f.severity === "critical").length;
        const high = findings.filter(f => f.severity === "high").length;

        const lines = [
          `=== PENDING REVIEW FINDINGS: ${findings.length}件 ===`,
          "前回のレビューで未解決のfindingsがあります:",
          "",
        ];
        if (critical > 0) lines.push(`🚨 CRITICAL findings: ${critical} 件`);
        if (high > 0) lines.push(`⚠️ HIGH findings: ${high} 件`);
        if (critical > 0 || high > 0) lines.push("");

        findings.forEach((f, i) => {
          lines.push(`${i + 1}. [${f.reviewer}] ${f.finding_summary} (${f.severity})`);
        });

        if (findings.length >= 5) {
          const queryHint = repoRoot
            ? `全件: python review-feedback.py query --resolution pending --repo-root "${repoRoot}"`
            : "全件: python review-feedback.py query --resolution pending";
          lines.push("", `(上位5件のみ表示。${queryHint})`);
        }

        lines.push("=================================");
        contextParts.push(lines.join("\n"));
      }
    }
  } catch {
    // review-feedback.py 未配置等はサイレントスキップ
  }

  try {
    const glmArgs = ["--json", "--limit", "5"];
    if (repoRoot) {
      glmArgs.push("--repo-root", repoRoot);
    }
    const glmResult = runPython(GLM_SUMMARY_SCRIPT_PATH, glmArgs);
    const summary = parseJsonOrNull(glmResult, "glm summary");

    if (summary && summary.total > 0) {
      const reasonCounts = summary.by_reason || {};
      const recentByReviewer = summary.recent_by_reviewer || {};
      const recent = Array.isArray(summary.recent) ? summary.recent : [];
      const recent429 = recent.filter(e => e.reason === "http_429").length;
      const recentNoToken = recent.filter(e => e.reason === "no_token").length;
      const recentSuppressed = recent.filter(e => e.reason === "suppressed_http_429").length;
      const shouldAlert = recent429 >= 2 || recentNoToken >= 2 || recentSuppressed >= 1;

      if (shouldAlert) {
        const lines = [
          "=== GLM CLASSIFIER SOFT ALERT ===",
          repoRoot
            ? "この repo の直近 GLM 分類 fallback に注意が必要です:"
            : "直近の GLM 分類 fallback に注意が必要です:",
        ];
        if (recent429 >= 2) lines.push(`- recent http_429: ${recent429} 件`);
        if (recentNoToken >= 2) lines.push(`- recent no_token: ${recentNoToken} 件`);
        if (recentSuppressed >= 1) lines.push(`- recent suppressed_http_429: ${recentSuppressed} 件`);

        const topReasons = Object.entries(reasonCounts)
          .sort((a, b) => b[1] - a[1])
          .slice(0, 3)
          .map(([reason, count]) => `${reason}=${count}`)
          .join(", ");
        if (topReasons) lines.push(`- total breakdown: ${topReasons}`);

        const topReviewer = Object.entries(recentByReviewer)
          .map(([reviewer, counts]) => ({
            reviewer,
            total: Object.values(counts).reduce((sum, count) => sum + count, 0),
            breakdown: Object.entries(counts)
              .sort((a, b) => b[1] - a[1])
              .map(([reason, count]) => `${reason}=${count}`)
              .join(", "),
          }))
          .sort((a, b) => b.total - a.total)[0];
        if (topReviewer && topReviewer.breakdown) {
          lines.push(`- top reviewer: ${topReviewer.reviewer} (${topReviewer.breakdown})`);
        }
        lines.push("================================");
        contextParts.push(lines.join("\n"));
      }
    }
  } catch {
    // summary script 未配置・ログ未作成等は無視
  }

  try {
    const learnedArgs = ["--json", "--limit", "5"];
    if (repoRoot) {
      learnedArgs.push("--repo-root", repoRoot);
    }
    const learnedResult = runPython(LEARNED_PATTERN_SUMMARY_SCRIPT_PATH, learnedArgs);
    const summary = parseJsonOrNull(learnedResult, "learned pattern summary");

    if (summary && summary.total > 0 && (summary.recent_count || 0) > 0) {
      const lines = [
        "=== LEARNED PATTERN SIGNAL ===",
        repoRoot
          ? "この repo では learned patterns 注入が最近使われています:"
          : "learned patterns 注入が最近使われています:",
        `- recent injections: ${summary.recent_count} 件`,
      ];
      if (summary.top_file) lines.push(`- top file: ${summary.top_file.file_path} (${summary.top_file.count})`);
      if (summary.top_tool) lines.push(`- top tool: ${summary.top_tool.tool_name} (${summary.top_tool.count})`);
      if (summary.top_reviewer) lines.push(`- top reviewer: ${summary.top_reviewer.reviewer} (${summary.top_reviewer.count})`);
      if (summary.top_reviewer_effectiveness) {
        lines.push(
          `- top reviewer effectiveness: pending=${summary.top_reviewer_effectiveness.pending_count}, `
          + `fixed=${summary.top_reviewer_effectiveness.fixed_count}, accepted=${summary.top_reviewer_effectiveness.accepted_count}`
        );
      }
      lines.push("================================");
      contextParts.push(lines.join("\n"));
    }
  } catch {
    // summary script 未配置・ログ未作成等は無視
  }

  if (contextParts.length === 0) {
    process.exit(0);
  }

  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: contextParts.join("\n\n"),
    },
  }));
  process.exit(0);
}

main();
