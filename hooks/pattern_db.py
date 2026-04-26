"""
review-patterns.db 管理モジュール。

学習済みパターン（レビューで検出→修正された実装問題）の記録・取得・注入を担当する。
既存の review-feedback.db とは独立したDB。実装バグのみを格納し、
スタイル・ドキュメント等のノイズを含まない。

13カテゴリ（enum強制）:
  logic, security, robustness, data-integrity, concurrency, type-safety,
  performance, api-contract, test-quality, consistency, documentation,
  ux, maintainability
"""

import sqlite3
import sys
from pathlib import Path
from datetime import datetime

# DB パス: ~/.claude/review-patterns.db
PATTERNS_DB_PATH = Path.home() / ".claude" / "review-patterns.db"

# 13カテゴリ（enum 強制）— LLM分類時にこのリストから1つを選ばせる
VALID_CATEGORIES = frozenset({
    "logic",
    "security",
    "robustness",
    "data-integrity",
    "concurrency",
    "type-safety",
    "performance",
    "api-contract",
    "test-quality",
    "consistency",
    "documentation",
    "ux",
    "maintainability",
})


def _is_absolute_path(path: str) -> bool:
    """Windows/UNC/Unix 風の絶対パスかを判定する。"""
    return path.startswith("//") or path.startswith("/") or (len(path) >= 3 and path[1] == ":" and path[2] == "/")


def _build_file_path_candidates(file_path: str, repo_root: str | None) -> list[str]:
    """absolute/relative 両方の候補を返す。"""
    normalized = file_path.replace("\\", "/")
    candidates = {normalized}
    if not repo_root:
        return sorted(candidates)

    normalized_root = repo_root.replace("\\", "/").rstrip("/")
    prefix = normalized_root.lower() + "/"
    if normalized.lower().startswith(prefix):
        candidates.add(normalized[len(normalized_root) + 1:])
    elif not _is_absolute_path(normalized):
        candidates.add(f"{normalized_root}/{normalized}")
    return sorted(candidates)


def _normalize_file_path_for_storage(file_path: str | None, repo_root: str | None) -> str | None:
    """repo_root 配下は relative path に正規化して保存する。"""
    if not file_path:
        return None
    normalized = file_path.replace("\\", "/")
    if not repo_root:
        return normalized

    normalized_root = repo_root.replace("\\", "/").rstrip("/")
    prefix = normalized_root.lower() + "/"
    if normalized.lower().startswith(prefix):
        return normalized[len(normalized_root) + 1:]
    return normalized


def _ensure_db(conn: sqlite3.Connection) -> None:
    """テーブルが存在しなければ作成する。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            -- 分類
            category        TEXT NOT NULL,           -- 13カテゴリ enum
            pattern_text    TEXT NOT NULL,            -- 抽象化されたパターン記述（50文字以内）
            severity        TEXT NOT NULL DEFAULT 'warning',  -- critical / warning
            -- コンテキスト
            file_path       TEXT,                     -- 検出ファイル（正規化済み）
            repo_root       TEXT,                     -- リポジトリルート（正規化済み）
            -- 信頼度
            confidence      TEXT NOT NULL DEFAULT 'high',  -- high（RFL明示）/ medium（git diff推定）
            detection_count INTEGER NOT NULL DEFAULT 1,    -- 同一パターンの検出回数
            -- タイムスタンプ
            first_detected  TEXT NOT NULL,             -- 初回検出日時（ISO 8601）
            last_detected   TEXT NOT NULL,             -- 最終検出日時（ISO 8601）
            -- ソース
            source_finding_id INTEGER,                -- 元の findings テーブル ID（参照用）
            source_reviewer TEXT,                      -- 検出したレビュアー（review-fix-loop 等）
            -- 将来拡張用（Phase 2: embedding重複排除）
            pattern_embedding TEXT                     -- JSON配列（sentence-transformer等）
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_patterns_file
        ON patterns (repo_root, file_path)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_patterns_category
        ON patterns (category)
    """)
    conn.commit()


def get_connection() -> sqlite3.Connection:
    """DB接続を取得する。テーブルが存在しなければ自動作成。"""
    conn = sqlite3.connect(str(PATTERNS_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    _ensure_db(conn)
    return conn


def validate_category(category: str) -> str:
    """カテゴリを検証し、正規化して返す。無効な場合は最も近いカテゴリを推定。"""
    normalized = category.lower().strip().replace("_", "-")
    if normalized in VALID_CATEGORIES:
        return normalized

    # よくある表記揺れの吸収
    aliases = {
        "bug": "logic",
        "code-bug": "logic",
        "logic-bug": "logic",
        "logic-error": "logic",
        "correctness": "logic",
        "edge-case": "logic",
        "error-handling": "robustness",
        "resource-management": "robustness",
        "file-io": "robustness",
        "encoding": "robustness",
        "null-safety": "security",
        "validation": "security",
        "data-loss": "security",
        "data-consistency": "data-integrity",
        "data-quality": "data-integrity",
        "query-correctness": "data-integrity",
        "state-management": "concurrency",
        "design": "api-contract",
        "architecture": "api-contract",
        "integration": "api-contract",
        "config": "api-contract",
        "missing-dependency": "api-contract",
        "ci": "test-quality",
        "workflow": "test-quality",
        "build": "test-quality",
        "pipeline": "test-quality",
        "test-coverage": "test-quality",
        "testability": "test-quality",
        "naming": "consistency",
        "style": "consistency",
        "code-duplication": "consistency",
        "dead-code": "consistency",
        "cleanup": "consistency",
        "dry": "consistency",
        "docs": "documentation",
        "doc-sync": "documentation",
        "onboarding": "documentation",
        "quickstart": "documentation",
        "readme": "documentation",
        "setup-guide": "documentation",
        "readability": "documentation",
        "clarity": "documentation",
        "a11y": "ux",
        "accessibility": "ux",
        "i18n": "ux",
        "seo": "ux",
        "navigation": "ux",
        "visual-fidelity": "ux",
        "code-quality": "maintainability",
        "complexity": "maintainability",
        "maintenance": "maintainability",
    }
    if normalized in aliases:
        return aliases[normalized]

    # フォールバック: 不明なカテゴリは maintainability に分類（最も汎用的）
    print(f"[pattern_db] 未知のカテゴリ '{category}' → maintainability にフォールバック", file=sys.stderr)
    return "maintainability"


def record_pattern(
    category: str,
    pattern_text: str,
    severity: str = "warning",
    file_path: str | None = None,
    repo_root: str | None = None,
    confidence: str = "high",
    source_finding_id: int | None = None,
    source_reviewer: str | None = None,
) -> int:
    """
    パターンをDBに記録する。同一パターンが存在すれば detection_count をインクリメント。

    Returns: パターンID
    """
    category = validate_category(category)
    # severity / confidence のバリデーション（不正値はデフォルトにフォールバック）
    if severity not in ("critical", "warning"):
        severity = "warning"
    if confidence not in ("high", "medium"):
        confidence = "high"
    # パターンテキストの正規化（80文字制限、改行除去）
    # 50→80文字: 衝突率 1-3% → 0.1-0.5% に改善、embedding移行時の影響もゼロ
    pattern_text = pattern_text.replace("\r\n", " ").replace("\n", " ").strip()[:80]
    # パス正規化（バックスラッシュ→フォワードスラッシュ、重複登録防止）
    if file_path:
        file_path = _normalize_file_path_for_storage(file_path, repo_root)
    if repo_root:
        repo_root = repo_root.replace("\\", "/")

    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    conn = get_connection()
    try:
        # 重複チェック: 同一 repo_root + file_path + category + パターンテキスト完全一致
        existing = conn.execute("""
            SELECT id, detection_count FROM patterns
            WHERE category = :category
              AND pattern_text = :pattern_text
              AND COALESCE(repo_root, '') = COALESCE(:repo_root, '')
              AND COALESCE(file_path, '') = COALESCE(:file_path, '')
        """, {
            "category": category,
            "pattern_text": pattern_text,
            "repo_root": repo_root,
            "file_path": file_path,
        }).fetchone()

        if existing:
            # 既存パターン: カウント増加 + 最終検出日更新
            conn.execute("""
                UPDATE patterns
                SET detection_count = detection_count + 1,
                    last_detected = :now,
                    confidence = CASE
                        WHEN :confidence = 'high' THEN 'high'
                        ELSE confidence
                    END
                WHERE id = :id
            """, {"now": now, "confidence": confidence, "id": existing["id"]})
            conn.commit()
            return existing["id"]
        else:
            # 新規パターン
            cursor = conn.execute("""
                INSERT INTO patterns (
                    category, pattern_text, severity, file_path, repo_root,
                    confidence, detection_count, first_detected, last_detected,
                    source_finding_id, source_reviewer
                ) VALUES (
                    :category, :pattern_text, :severity, :file_path, :repo_root,
                    :confidence, 1, :now, :now,
                    :source_finding_id, :source_reviewer
                )
            """, {
                "category": category,
                "pattern_text": pattern_text,
                "severity": severity,
                "file_path": file_path,
                "repo_root": repo_root,
                "confidence": confidence,
                "now": now,
                "source_finding_id": source_finding_id,
                "source_reviewer": source_reviewer,
            })
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


def get_patterns_for_file(
    file_path: str,
    repo_root: str | None = None,
    max_patterns: int = 5,
) -> list[dict]:
    """
    ファイルに関連する学習済みパターンを取得する（PreToolUse注入用）。

    カテゴリ別に最頻パターン1つを選出し、最大 max_patterns 件返す。
    Cool-off: detection_count >= 2 のパターンのみ（初回検出は学習しない）。

    Returns: [{"category": str, "pattern_text": str, "severity": str, "count": int}, ...]
    """
    if not PATTERNS_DB_PATH.exists():
        return []

    # file_path 正規化（バックスラッシュ→フォワードスラッシュ）
    path_candidates = _build_file_path_candidates(file_path, repo_root)
    # repo_root も正規化
    if repo_root:
        repo_root = repo_root.replace("\\", "/")

    conn = get_connection()
    try:
        # カテゴリ別に detection_count 最大のパターンを1つずつ取得
        # Cool-off: detection_count >= 2（新規パターンは学習対象外）
        repo_clause = (
            "AND (replace(COALESCE(repo_root, ''), '\\', '/') = ? OR repo_root IS NULL)"
            if repo_root else ""
        )
        path_placeholders = ",".join("?" * len(path_candidates))
        params: tuple = (*path_candidates, repo_root) if repo_root else tuple(path_candidates)

        rows = conn.execute(f"""
            SELECT category, pattern_text, severity, detection_count
            FROM patterns
            WHERE replace(COALESCE(file_path, ''), '\\', '/') IN ({path_placeholders})
              {repo_clause}
              AND detection_count >= 2
            ORDER BY
                CASE severity WHEN 'critical' THEN 0 ELSE 1 END,
                detection_count DESC
        """, params).fetchall()

        # カテゴリ別に最頻1件を選出
        seen_categories: set[str] = set()
        result: list[dict] = []
        for row in rows:
            cat = row["category"]
            if cat in seen_categories:
                continue
            seen_categories.add(cat)
            result.append({
                "category": cat,
                "pattern_text": row["pattern_text"],
                "severity": row["severity"],
                "count": row["detection_count"],
            })
            if len(result) >= max_patterns:
                break

        return result
    finally:
        conn.close()


def format_injection_text(patterns: list[dict]) -> str:
    """
    PreToolUse 注入用のテキストを生成する。

    Returns: 注入テキスト（空文字列 = 注入なし）
    """
    if not patterns:
        return ""

    lines = ["[LEARNED PATTERNS] このファイルで過去に検出・修正された実装パターン:"]
    for p in patterns:
        lines.append(f"  - [{p['category']}] {p['pattern_text']} ({p['count']}回検出)")
    lines.append("上記パターンに該当する問題がないか注意してコードを書いてください。")
    return "\n".join(lines)
