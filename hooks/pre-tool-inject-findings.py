#!/usr/bin/env python3
"""
PreToolUse hook: Edit/Write 実行時に file_path 特化 findings を注入する
"""
import sys
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

# hook は任意の cwd から実行されるため、config.py がある hooks/ を sys.path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 環境で cp932 stdout に日本語を出力するための UTF-8 強制
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import DB_PATH, INJECT_STATE_DIR as STATE_DIR
INJECT_LIMIT = 8
FALLBACK_LIMIT = 5   # Phase B: プロジェクト横断 critical のみに絞るため小さめ
STALE_DAYS = 30
DEDUP_ROTATION_LIMIT = 2000  # inject-state dedup ファイルの最大行数（超えたら古い半分を削除）
RELEVANCE_DAYS = 14  # last_relevant_edit 用の短縮ウィンドウ（古い finding でも最近触られたファイルなら注入復活）
FP_PATTERN_LIMIT = 5  # 学習済み FP パターン表示上限
MIN_PROJECT_ROOT_DEPTH = 4  # Phase B depth check: git root がこれ未満のパス深さなら他プロジェクト巻き込み防止でスキップ


def _load_injected_ids(session_id: str) -> set[int]:
    """セッション内で既に注入済みの finding ID セットを読み込む（append-only .txt 形式）。
    DEDUP_ROTATION_LIMIT を超えた場合は古い半分を削除してローテーションする。"""
    state_file = STATE_DIR / f"{session_id}.txt"
    if state_file.exists():
        lines = [line.strip() for line in state_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        # ローテーション: 長時間セッションでの dedup ファイル肥大化を防止
        if len(lines) > DEDUP_ROTATION_LIMIT:
            # 新しい半分を残す（古い finding は再注入されても SNR 影響は軽微）
            sliced = lines[len(lines) // 2:]
            try:
                # アトミック書き込み: 並列プロセスの append と競合した場合の
                # ファイル破損を防ぐ（session-end-learn.py と同じパターン）
                with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8',
                    dir=STATE_DIR, suffix='.tmp', delete=False
                ) as tmp:
                    tmp.write("\n".join(sliced) + "\n")
                    tmp_path = tmp.name
                os.replace(tmp_path, str(state_file))
                lines = sliced  # 書き込み成功時のみ lines を更新（ファイルとメモリの整合性を保証）
            except OSError:
                # アトミック書き込み失敗時は temp を削除し、lines は元のまま維持
                # （ファイル内容と一致する状態を保つ）
                try:
                    os.unlink(tmp_path)
                except (OSError, NameError):
                    pass
        return {int(line) for line in lines if line.isdigit()}
    return set()


def _save_injected_ids(session_id: str, new_ids: set[int]) -> None:
    """注入済み finding ID を append-only でセッション状態ファイルに追記する（競合耐性）。

    並列 Edit 実行時に複数プロセスが同時に append する競合が発生し得る。
    POSIX の O_APPEND は原子的だが、Windows の append モードでは保証がない。
    最悪ケースでも同一 finding が 1 回余分に注入されるだけ（SNR への影響は軽微）。
    厳密な排他制御が必要な場合はファイルロックを追加する。
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{session_id}.txt"
    # append-only: read-modify-write より並列プロセス競合に強い
    with open(state_file, "a", encoding="utf-8") as f:
        for id_ in new_ids:
            f.write(f"{id_}\n")


def _update_injection_tracking(conn: sqlite3.Connection, findings: list[dict], now: str) -> None:
    """注入した finding の injected_count / last_injected を更新（カラム未追加時はスキップ）"""
    if not findings:  # 空リストの場合は何もしない（placeholders が空になるのを防ぐ）
        return
    ids = [f["id"] for f in findings]
    placeholders = ",".join("?" * len(ids))
    try:
        conn.execute(f"""
            UPDATE findings
            SET injected_count = injected_count + 1,
                last_injected  = ?
            WHERE id IN ({placeholders})
        """, (now,) + tuple(ids))
        conn.commit()
    except sqlite3.OperationalError as e:
        if "injected_count" not in str(e) and "last_injected" not in str(e):
            # 想定外の OperationalError（DB破損等）は stderr に出力してデバッグを可能にする
            print(f"[pre-tool-inject-findings] OperationalError: {e}", file=sys.stderr)
        # injected_count / last_injected カラム未追加時は無視


def _get_project_root(file_path: str, cwd: str | None = None) -> str | None:
    """編集対象ファイルの git リポジトリルートを取得する（Phase B フィルタ用）。
    file_path の親ディレクトリが存在しない場合（Write で新規ファイル作成時）は
    cwd をフォールバックとして使用する。"""
    # 新規ファイル作成時: 親ディレクトリがまだ存在しない可能性がある
    git_cwd = str(Path(file_path).parent)
    if not Path(git_cwd).exists():
        git_cwd = cwd if cwd else str(Path.cwd())
    try:
        result = subprocess.run(
            ["git", "-C", git_cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            # バックスラッシュをスラッシュに統一し、二重スラッシュを除去して
            # LIKE パターンとの整合性を保つ（UNCパス・git出力ゆれの両方に対応）
            normalized = result.stdout.strip().replace("\\", "/")
            # UNCパス（//server/share）の先頭 // は保持し、内部の // のみ除去
            unc_prefix = ""
            if normalized.startswith("//"):
                unc_prefix = "//"
                normalized = normalized[2:]
            while "//" in normalized:
                normalized = normalized.replace("//", "/")
            return unc_prefix + normalized
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # git 管理外ファイルはプロジェクト判定不能 → None を返して Phase B をスキップ
    return None


def get_fp_patterns(conn: sqlite3.Connection, repo_root: str | None) -> list[dict]:
    """学習済み FP パターン（ユーザーが2回以上 dismiss 承認したカテゴリ+理由）を取得する。
    注入テキスト末尾に追加して、Claude が同パターンの新規指摘を慎重に判断できるようにする。"""
    try:
        if repo_root:
            rows = conn.execute("""
                SELECT category, fp_reason, COUNT(*) AS cnt
                FROM findings
                WHERE dismissed = 1 AND dismissed_by = 'user'
                  AND fp_reason IS NOT NULL AND fp_reason != ''
                  AND severity != 'critical'
                  AND resolution = 'pending'
                  AND (replace(COALESCE(repo_root, ''), '\\', '/') = ? OR repo_root IS NULL)
                GROUP BY category, fp_reason
                HAVING cnt >= 2
                ORDER BY cnt DESC
                LIMIT ?
            """, (repo_root, FP_PATTERN_LIMIT)).fetchall()
        else:
            rows = conn.execute("""
                SELECT category, fp_reason, COUNT(*) AS cnt
                FROM findings
                WHERE dismissed = 1 AND dismissed_by = 'user'
                  AND fp_reason IS NOT NULL AND fp_reason != ''
                  AND severity != 'critical'
                  AND resolution = 'pending'
                GROUP BY category, fp_reason
                HAVING cnt >= 2
                ORDER BY cnt DESC
                LIMIT ?
            """, (FP_PATTERN_LIMIT,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def get_findings(
    file_path: str, session_id: str, conn: sqlite3.Connection,
    cwd: str | None = None,
) -> tuple[list[dict], bool, str | None]:
    """
    findings, is_fallback フラグ, repo_root を返す。
    is_fallback=False: Phase A（ファイル特化）
    is_fallback=True : Phase B（プロジェクト横断 critical のみ・新規ファイル用フォールバック）
    repo_root: FP パターン取得用に呼び出し元へ返す

    conn は呼び出し元で管理する（二重接続防止）。

    セッション内 dedup: 同一セッションで既に注入した finding は再注入しない。
    各ツール呼び出しは独立プロセスなのでファイルベースで状態を管理する。
    """
    # DB の created_at は strftime('%Y-%m-%dT%H:%M:%S','now','localtime') 形式
    # Python 側も T 区切りに統一することで文字列比較の正確性を保証する
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
    # last_relevant_edit 用の短縮カットオフ（古い finding でも最近触られたファイルなら注入復活）
    relevance_cutoff = (datetime.now() - timedelta(days=RELEVANCE_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

    # session_id が空の場合は注入をスキップする。
    # dedup なしで毎回全件を注入すると SNR が破壊されるため、
    # session_id を取得できない環境では注入しない方が安全。
    if not session_id:
        return [], False, None
    already_injected = _load_injected_ids(session_id)

    # --- Phase A: ファイル特化クエリ ---
    # repo_root でリポジトリスコープを分離（クロスコンタミネーション防止）
    # ⚠️ dismissed カラムは Phase 1 ALTER TABLE 完了後に有効（追加前は OperationalError をスキップして空を返す）
    normalized_path = file_path.replace("\\", "/")
    repo_root = _get_project_root(file_path, cwd=cwd)
    try:
        # repo_root が取得できた場合はスコープフィルタ追加、なければ従来通り
        # 鮮度条件: created_at >= 30日以内 OR last_relevant_edit >= 14日以内
        # last_relevant_edit カラムが未追加の場合は COALESCE で created_at 以前の値にフォールバック
        freshness_clause = "(created_at >= ? OR COALESCE(last_relevant_edit, '2000-01-01') >= ?)"
        if repo_root:
            rows = conn.execute(f"""
                SELECT id, severity, category, finding_summary
                FROM findings
                WHERE replace(file_path, '\\', '/') = ?
                  AND (replace(COALESCE(repo_root, ''), '\\', '/') = ? OR repo_root IS NULL)
                  AND dismissed = 0
                  AND resolution = 'pending'
                  AND severity IN ('critical', 'high', 'warning')
                  AND {freshness_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM findings f2
                      WHERE replace(f2.file_path, '\\', '/') = replace(findings.file_path, '\\', '/')
                        AND f2.category        = findings.category
                        AND f2.finding_summary = findings.finding_summary
                        AND f2.resolution      IN ('accepted', 'fixed')
                        AND (replace(COALESCE(f2.repo_root, ''), '\\', '/') = ?
                             OR (f2.repo_root IS NULL AND findings.repo_root IS NULL))
                  )
                ORDER BY
                  CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high'     THEN 1
                    WHEN 'warning'  THEN 2
                    ELSE 3
                  END,
                  id DESC
                LIMIT ?
            """, (normalized_path, repo_root, cutoff, relevance_cutoff, repo_root, INJECT_LIMIT)).fetchall()
        else:
            # repo_root なし（git 管理外）: NOT EXISTS に repo_root スコープなし
            # 制限: 他プロジェクトの同名 finding が accepted/fixed の場合に除外される可能性がある
            # git 管理外ファイルは稀なため、この制限は許容する
            rows = conn.execute(f"""
                SELECT id, severity, category, finding_summary
                FROM findings
                WHERE replace(file_path, '\\', '/') = ?
                  AND dismissed = 0
                  AND resolution = 'pending'
                  AND severity IN ('critical', 'high', 'warning')
                  AND {freshness_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM findings f2
                      WHERE replace(f2.file_path, '\\', '/') = replace(findings.file_path, '\\', '/')
                        AND f2.category        = findings.category
                        AND f2.finding_summary = findings.finding_summary
                        AND f2.resolution      IN ('accepted', 'fixed')
                  )
                ORDER BY
                  CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high'     THEN 1
                    WHEN 'warning'  THEN 2
                    ELSE 3
                  END,
                  id DESC
                LIMIT ?
            """, (normalized_path, cutoff, relevance_cutoff, INJECT_LIMIT)).fetchall()
    except sqlite3.OperationalError:
        return [], False, repo_root  # dismissed カラム未追加時はスキップ

    # セッション内 dedup: 既注入 ID を除外
    findings = [d for r in rows if (d := dict(r))["id"] not in already_injected]
    if findings:
        _update_injection_tracking(conn, findings, now)
        _save_injected_ids(session_id, {f["id"] for f in findings})
        return findings, False, repo_root

    # --- Phase B: プロジェクト横断 critical フォールバック（新規ファイル・findings なしファイル用）---
    # SNR 維持のため severity = 'critical' のみに絞り LIMIT も小さくする
    # project_root フィルタ: 他プロジェクトの critical を混入させない
    # Phase A で取得済みの repo_root を再利用（_get_project_root の二重サブプロセス呼び出しを回避）
    project_root = repo_root
    # rstrip('/') で末尾スラッシュを正規化してから '/%' を付与（境界不一致防止）
    project_filter = (project_root.rstrip("/") + "/%") if project_root else None

    if not project_filter:
        return [], False, repo_root  # プロジェクト判定不能時は Phase B をスキップ

    # depth check: git root が浅すぎる（ドライブルートや home 直下等）場合は
    # 他プロジェクトの findings を大量に巻き込む恐れがあるためスキップ
    if len(Path(project_root).parts) < MIN_PROJECT_ROOT_DEPTH:
        return [], False, repo_root

    try:
        # Phase A と同様に last_relevant_edit 鮮度条件を適用
        # critical は見逃すと致命的なため、古くても最近関連ファイルが編集されていれば注入する
        fallback_rows = conn.execute(f"""
            SELECT id, severity, category, finding_summary
            FROM findings
            WHERE dismissed = 0
              AND resolution = 'pending'
              AND severity = 'critical'
              AND {freshness_clause}
              AND LOWER(replace(file_path, '\\', '/')) LIKE LOWER(?)
              AND NOT EXISTS (
                  SELECT 1 FROM findings f2
                  WHERE replace(f2.file_path, '\\', '/') = replace(findings.file_path, '\\', '/')
                    AND f2.category        = findings.category
                    AND f2.finding_summary = findings.finding_summary
                    AND f2.resolution      IN ('accepted', 'fixed')
                    AND LOWER(replace(COALESCE(f2.file_path, ''), '\\', '/')) LIKE LOWER(?)
              )
            ORDER BY id DESC
            LIMIT ?
        """, (cutoff, relevance_cutoff, project_filter, project_filter, FALLBACK_LIMIT)).fetchall()
    except sqlite3.OperationalError:
        return [], False, repo_root

    # セッション内 dedup（Phase B も同様に適用）
    fallback = [d for r in fallback_rows if (d := dict(r))["id"] not in already_injected]
    if fallback:
        _update_injection_tracking(conn, fallback, now)
        _save_injected_ids(session_id, {f["id"] for f in fallback})
        return fallback, True, repo_root
    return [], False, repo_root


def format_injection(
    file_path: str,
    findings: list[dict],
    is_fallback: bool = False,
    fp_patterns: list[dict] | None = None,
) -> str:
    if is_fallback:
        # Phase B: 新規ファイル用（プロジェクト横断 critical のみ）
        header = f"=== PROJECT-WIDE CRITICAL PATTERNS (新規ファイル: {file_path}) ==="
    else:
        # Phase A: ファイル特化
        header = f"=== PAST FINDINGS: {file_path} ==="
    lines = [header]
    for f in findings:
        # #1: dismiss ディスカバラビリティ — 各 finding に ID を表示
        lines.append(f"【{f['severity']}】#{f['id']} {f['category'] or '?'}: {f['finding_summary']}")
    lines.append(f"（{len(findings)} 件を表示）")
    lines.append("これらを考慮して編集してください。同じアンチパターンの繰り返しは避けること。")

    # #1: dismiss コマンドのワンライナー提示（Act フェーズのディスカバラビリティ向上）
    finding_ids = ", ".join(str(f["id"]) for f in findings[:3])
    lines.append(f'誤検知なら: python "C:\\Users\\Tenormusica\\.claude\\scripts\\review-feedback.py" dismiss <ID> --reason "理由"  (例: ID={finding_ids})')

    # #3: 学習済み FP パターンセクション（ユーザーが2回以上 dismiss 承認したパターン）
    if fp_patterns:
        lines.append("--- 学習済みパターン（過去にFPとして却下済み） ---")
        for p in fp_patterns:
            # fp_reason 内の改行・制御文字を除去し、長さを制限（コンテキスト圧迫防止）
            reason = str(p['fp_reason']).replace("\n", " ").replace("\r", "").strip()[:80]
            lines.append(f"  [{p['category']}] {reason} ({p['cnt']}回却下)")
        lines.append("上記パターンに類似する新規指摘は慎重に判断すること。")

    lines.append("=== END FINDINGS ===")
    return "\n".join(lines)


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    # Edit/Write/MultiEdit のみ対象
    if tool_name not in ("Edit", "Write", "MultiEdit"):
        sys.exit(0)

    # 対象ファイルパスを収集し、バックスラッシュをスラッシュに正規化する
    # MultiEdit: 全 edits から重複除去して収集（先頭のみ対象にすると他ファイルの findings が見落とされる）
    file_paths: list[str] = []
    if tool_name == "MultiEdit":
        seen: set[str] = set()
        for edit in tool_input.get("edits", []):
            fp = edit.get("file_path")
            if fp:
                fp = fp.replace("\\", "/")
                if fp not in seen:
                    file_paths.append(fp)
                    seen.add(fp)
    else:
        fp = tool_input.get("file_path") or tool_input.get("path")
        if fp:
            file_paths = [fp.replace("\\", "/")]

    if not file_paths:
        sys.exit(0)

    session_id = payload.get("session_id", "")  # 空文字列フォールバック（"unknown" にしない）
    cwd = payload.get("cwd")  # Write 新規ファイル時のフォールバック用

    if not DB_PATH.exists():
        sys.exit(0)

    # DB 接続を一箇所で管理（get_findings + get_fp_patterns で共有）
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        # 各ファイルの findings を収集し、出力があるものだけ注入する
        outputs = []
        first_repo_root = None  # FP パターン取得用（最初のファイルの repo_root を使い回す）
        for fp in file_paths:
            findings, is_fallback, repo_root = get_findings(fp, session_id, conn, cwd=cwd)
            if first_repo_root is None and repo_root:
                first_repo_root = repo_root
            if findings:
                outputs.append((fp, findings, is_fallback))

        if not outputs:
            sys.exit(0)

        # FP パターンを1回だけ取得（全ファイル共通で表示。同一 conn を再利用）
        fp_patterns = get_fp_patterns(conn, first_repo_root)
    finally:
        conn.close()

    # 注入テキスト生成
    injection_texts = []
    for fp, findings, is_fallback in outputs:
        injection_texts.append(format_injection(fp, findings, is_fallback, fp_patterns))
        # FP パターンは最初のファイルにのみ追加（重複表示防止）
        fp_patterns = None

    # stdout に注入文を出力 → Claude Code がコンテキストに追加する
    print("\n\n".join(injection_texts))
    sys.exit(0)


if __name__ == "__main__":
    main()
