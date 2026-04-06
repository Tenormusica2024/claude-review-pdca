#!/usr/bin/env python3
"""
SessionEnd hook: ユーザー承認済み dismissed パターンを CLAUDE.md に追記 + inject-state クリーンアップ
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

# hook は任意の cwd から実行されるため、config.py がある hooks/ を sys.path に追加
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 環境で cp932 stdout に日本語を出力するための UTF-8 強制
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import DB_PATH, INJECT_STATE_DIR as STATE_DIR, EDIT_COUNTER_DIR as COUNTER_DIR, normalize_git_root


def _find_claude_md(payload: dict) -> tuple[Path | None, str | None]:
    """
    プロジェクト固有の CLAUDE.md とリポジトリルートを探す（3段フォールバック）。
    SessionEnd の cwd はプロジェクトルートとは限らないため動的に解決する。

    Returns:
        (claude_md_path, repo_root): CLAUDE.md の Path と正規化済み repo_root。
        repo_root は学習クエリのプロジェクトスコープフィルタに使用する。
    """
    repo_root = None

    # 1. payload["cwd"] を優先（Claude Code が渡す作業ディレクトリ）
    cwd_str = payload.get("cwd")

    # 2. git rev-parse --show-toplevel で git root を取得
    try:
        search_dir = cwd_str or str(Path.cwd())
        result = subprocess.run(
            ["git", "-C", search_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            repo_root = normalize_git_root(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # グローバル CLAUDE.md（~/CLAUDE.md）への誤書き込みを防ぐ共通チェック
    # cwd やホームディレクトリ直下で実行された場合に C:\Users\<user>\CLAUDE.md を掴まないようにする
    home_claude_md = (Path.home() / "CLAUDE.md").resolve()

    # CLAUDE.md 探索: cwd → git root → Path.cwd() の順にフォールバック
    if cwd_str:
        candidate = Path(cwd_str) / "CLAUDE.md"
        if candidate.exists() and candidate.resolve() != home_claude_md:
            return candidate, repo_root

    if repo_root:
        candidate = Path(repo_root) / "CLAUDE.md"
        if candidate.exists() and candidate.resolve() != home_claude_md:
            return candidate, repo_root

    # 3. Path.cwd() フォールバック
    candidate = Path.cwd() / "CLAUDE.md"
    if candidate.exists() and candidate.resolve() != home_claude_md:
        return candidate, repo_root

    return None, repo_root  # プロジェクト固有 CLAUDE.md が見つからない場合はスキップ


STALE_DAYS = 90  # pending → stale 自動遷移の閾値（gc-stale CLI と同一値）


def _gc_stale_findings() -> int:
    """90日以上 pending の findings を stale に自動遷移する。
    セッション終了時に実行することで、明示的な gc-stale CLI 不要にする。
    Returns: 遷移した件数。"""
    if not DB_PATH.exists():
        return 0
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
    except (sqlite3.Error, OSError):
        return 0
    try:
        cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime('%Y-%m-%dT%H:%M:%S')
        now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        cursor = conn.execute("""
            UPDATE findings
            SET resolution = 'stale', resolved_at = ?
            WHERE resolution = 'pending'
              AND created_at < ?
              AND dismissed = 0
        """, (now, cutoff))
        count = cursor.rowcount
        conn.commit()
        return count
    except (sqlite3.Error, OSError):
        return 0
    finally:
        conn.close()


def _cleanup_inject_state() -> None:
    """
    クリーンアップ処理（main() より先に実行）:
    - 24時間以上古いセッション dedup ファイルを削除
    - 24時間以上古い編集カウントファイルを削除
    - edit-counter.txt（旧グローバル形式）のローテーション（10000行超で古い半分を削除）
    - 90日超 pending findings の stale 自動遷移
    クリーンアップを main() の前に実行することで、セッション終了時の一括後片付けとして機能させる。
    """
    cutoff = time.time() - 86400  # 24時間

    # inject-state: .txt（現行形式）と .json（旧形式の残骸。現行コードは .txt のみ生成するが
    # 過去バージョンで .json を生成していた可能性があるため互換クリーンアップとして残す）
    if STATE_DIR.exists():
        for pattern in ("*.txt", "*.json"):
            for f in STATE_DIR.glob(pattern):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass  # 削除失敗は無視（別プロセスが使用中の可能性）

    # edit-counter: セッション別ファイルを削除（{session_id}.txt と {session_id}_files.txt の両方を含む）
    if COUNTER_DIR.exists():
        for f in COUNTER_DIR.glob("*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    # edit-counter.txt（旧グローバル形式）のローテーション（後方互換）
    # 注意: read_text → write_text 間に別プロセスが append すると lost update が起きる。
    # ただし旧形式はもう新規生成されないため、残存ファイルのサイズ抑制が目的。
    legacy_counter = Path.home() / ".claude" / "edit-counter.txt"
    if legacy_counter.exists():
        try:
            lines = legacy_counter.read_text(encoding="utf-8").splitlines()
            if len(lines) > 10000:
                legacy_counter.write_text("\n".join(lines[-5000:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    # stale GC: 90日超 pending を自動遷移（セッション終了の度に軽量チェック）
    stale_count = _gc_stale_findings()
    if stale_count > 0:
        print(f"[session-end-learn] {stale_count} 件の古い pending findings を stale に遷移", file=sys.stderr)


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        payload = {}

    if not DB_PATH.exists():
        return

    # プロジェクト固有の CLAUDE.md を動的に探す（cwd 依存を排除）
    claude_md_path, repo_root = _find_claude_md(payload)
    if claude_md_path is None:
        return  # プロジェクト固有 CLAUDE.md が存在しない場合はスキップ（グローバルは書かない）

    # sqlite3 の with 構文はトランザクション管理のみで close() を保証しないため
    # try/finally で明示的に close() する
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row  # pre-tool-inject-findings.py と一貫性を保つ
    except sqlite3.Error:
        return
    try:
        # Phase 1 ALTER TABLE 完了前は dismissed カラムが存在しないためスキップ
        try:
            # severity ガード: critical の findings は auto-promotion 禁止（明示的 opt-in のみ）
            # repo_root フィルタ: 他プロジェクトの dismissed パターンを学習しない（クロスコンタミネーション防止）
            # repo_root あり→スコープフィルタ付き / なし→フィルタなし（git 管理外からの SessionEnd）
            base_query = """
                SELECT category, fp_reason, COUNT(*) AS cnt
                FROM findings
                WHERE dismissed = 1
                  AND dismissed_by = 'user'
                  AND fp_reason IS NOT NULL
                  AND fp_reason != ''
                  AND severity != 'critical'
                  AND resolution = 'pending'
                  {repo_filter}
                GROUP BY category, fp_reason
                HAVING cnt >= 2     -- 2回以上承認されたパターンのみ学習
                ORDER BY cnt DESC
                LIMIT 20
            """
            if repo_root:
                raw = conn.execute(
                    base_query.format(repo_filter="AND replace(COALESCE(repo_root, ''), '\\', '/') = ?"),
                    (repo_root,)
                ).fetchall()
            else:
                raw = conn.execute(
                    base_query.format(repo_filter="")
                ).fetchall()
            # sqlite3.Row は接続クローズ後の参照が不安定なため、
            # conn.close() を呼ぶ前に Python ネイティブの dict に変換する
            rows = [dict(r) for r in raw]
        except sqlite3.OperationalError:
            return  # dismissed カラム未追加時はスキップ
    except sqlite3.Error:
        return
    finally:
        conn.close()

    if not rows:
        return

    def _sanitize_fp_reason(reason: str) -> str:
        """fp_reason を CLAUDE.md に安全に書き込める形に整形する。
        改行をスペースに変換し、先頭 # を全角変換することで CLAUDE.md の構造破損を防ぐ。
        """
        reason = reason.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        if reason.startswith("#"):
            reason = "＃" + reason[1:]  # 全角 ＃ に変換して markdown 見出しとして解釈されないよう
        return reason[:80]

    new_entries = [f"- [{r['category']}] {_sanitize_fp_reason(r['fp_reason'] or '')} （{r['cnt']}回承認）" for r in rows]
    # HTMLコメントマーカーで自動生成部分を囲む（ユーザー手動追記を保護する）
    # ユーザーがマーカー外（見出しの下、開始マーカーの上）に書いた行は置換されない
    # 注意: 日付ラベルは CLAUDE.md に書かない（グローバルルール: 日付・進捗宣言禁止）
    AUTO_START = "<!-- auto-generated:fp-patterns -->"
    AUTO_END = "<!-- end-auto-generated:fp-patterns -->"
    auto_block = (
        AUTO_START + "\n"
        + "\n".join(new_entries) + "\n"
        + AUTO_END + "\n"
    )

    # utf-8-sig: BOM 付き UTF-8 ファイルの BOM を自動除去（Windows エディタが BOM を付与する場合がある）
    content = claude_md_path.read_text(encoding="utf-8-sig")

    section_header = "## 学習済み false positive パターン（自動生成）"

    if AUTO_START in content and AUTO_END in content:
        # マーカーが存在する場合: マーカー間のみ置換（ユーザー追記は保持）
        start_idx = content.index(AUTO_START)
        end_idx = content.index(AUTO_END) + len(AUTO_END)
        # AUTO_END 直後の改行も含めて置換
        if end_idx < len(content) and content[end_idx] == "\n":
            end_idx += 1
        content = content[:start_idx] + auto_block + content[end_idx:]
    elif any(line.rstrip("\r\n").startswith("## 学習済み false positive パターン") for line in content.splitlines()):
        # 旧形式（マーカーなし）の既存ブロックを新形式に移行
        # splitlines() で行単位処理（CRLF 対応）
        lines = content.splitlines(keepends=True)
        result_lines = []
        preserved_user_lines = []  # ユーザー手動追記を保護する
        in_block = False
        for line in lines:
            stripped = line.rstrip("\r\n")
            if stripped.startswith("## 学習済み false positive パターン"):
                in_block = True
                continue
            if in_block and re.match(r'^#{1,6}\s', stripped):
                in_block = False
                result_lines.append(line)
                continue
            if in_block:
                # 旧形式ブロック内の行: 自動生成エントリ（"- [" で始まる）以外はユーザー追記として保護
                if not stripped.startswith("- ["):
                    if stripped:  # 空行は除外
                        preserved_user_lines.append(line)
            else:
                result_lines.append(line)
        content = re.sub(r'[\r\n]+$', '\n', "".join(result_lines))
        # 新形式のブロック（見出し + マーカー）を末尾に追加
        # ユーザー手動追記があれば auto_block の前に挿入して保護する
        if preserved_user_lines:
            user_block = "".join(preserved_user_lines)
            if not user_block.endswith("\n"):
                user_block += "\n"
            content += "\n" + section_header + "\n" + user_block + auto_block
        else:
            content += "\n" + section_header + "\n" + auto_block
    else:
        # 既存ブロックがない場合: 見出し + マーカー付きブロックを末尾に追加
        content = re.sub(r'[\r\n]+$', '\n', content)
        content += "\n" + section_header + "\n" + auto_block

    # アトミック書き込み: temp ファイルに書いてから os.replace でアトミックに置換する
    # CLAUDE.md の同時書き込みによる lost update を防ぐ（NTFS では MoveFileEx がアトミック）
    # リトライ: Windows で他プロセス（VSCode, Obsidian 等）がファイルをロックしている場合の
    # PermissionError に対応。1回リトライ（0.5秒待機）で多くのケースを救える。
    max_attempts = 2
    for attempt in range(max_attempts):
        tmp_path = None  # except ブロックで未定義の場合の NameError を防ぐ
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8',
                dir=claude_md_path.parent,
                suffix='.tmp', delete=False
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            os.replace(tmp_path, str(claude_md_path))
            break  # 成功したらループ終了
        except OSError as e:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            if attempt < max_attempts - 1:
                # リトライ前に短い待機（ファイルロック解放を待つ）
                time.sleep(0.5)
                continue
            # 最終試行も失敗: 直接書き込みにフォールバック
            try:
                claude_md_path.write_text(content, encoding="utf-8")
            except OSError as e2:
                print(f"[session-end-learn] CLAUDE.md 書き込みエラー（フォールバックも失敗）: {e} / {e2}", file=sys.stderr)


if __name__ == "__main__":
    # クリーンアップを main() より先に実行（セッション終了時の後片付けとして機能させる）
    try:
        _cleanup_inject_state()
    except Exception as e:
        print(f"[session-end-learn] cleanup error (non-fatal): {e}", file=sys.stderr)
    main()
