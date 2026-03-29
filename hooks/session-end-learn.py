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
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "review-feedback.db"
# PreToolUse hook のセッション別 dedup ファイル管理ディレクトリ
STATE_DIR = Path.home() / ".claude" / "inject-state"
# PostToolUse hook のセッション別編集カウントディレクトリ
COUNTER_DIR = Path.home() / ".claude" / "edit-counter"


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
            # バックスラッシュをスラッシュに統一（pre-tool-inject-findings.py と同じ正規化）
            normalized = result.stdout.strip().replace("\\", "/")
            repo_root = normalized.replace("//", "/") if normalized.startswith("//") else normalized
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # CLAUDE.md 探索: cwd → git root → Path.cwd() の順にフォールバック
    if cwd_str:
        candidate = Path(cwd_str) / "CLAUDE.md"
        if candidate.exists():
            return candidate, repo_root

    if repo_root:
        candidate = Path(repo_root) / "CLAUDE.md"
        if candidate.exists():
            return candidate, repo_root

    # 3. Path.cwd() フォールバック
    candidate = Path.cwd() / "CLAUDE.md"
    if candidate.exists():
        # グローバル CLAUDE.md（~/.CLAUDE.md 相当）への誤書き込みを防ぐ:
        # cwd がホームディレクトリの場合に C:\Users\<user>\CLAUDE.md を掴んでしまうケースを除外
        if candidate.resolve() == (Path.home() / "CLAUDE.md").resolve():
            return None, repo_root
        return candidate, repo_root

    return None, repo_root  # プロジェクト固有 CLAUDE.md が見つからない場合はスキップ


def _cleanup_inject_state() -> None:
    """
    クリーンアップ処理（main() より先に実行）:
    - 24時間以上古いセッション dedup ファイルを削除
    - 24時間以上古い編集カウントファイルを削除
    - edit-counter.txt（旧グローバル形式）のローテーション（10000行超で古い半分を削除）
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

    # edit-counter: セッション別ファイルを削除
    if COUNTER_DIR.exists():
        for f in COUNTER_DIR.glob("*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    # edit-counter.txt（旧グローバル形式）のローテーション（後方互換）
    legacy_counter = Path.home() / ".claude" / "edit-counter.txt"
    if legacy_counter.exists():
        try:
            lines = legacy_counter.read_text(encoding="utf-8").splitlines()
            if len(lines) > 10000:
                legacy_counter.write_text("\n".join(lines[-5000:]) + "\n", encoding="utf-8")
        except OSError:
            pass


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
    except sqlite3.Error:
        return
    try:
        # Phase 1 ALTER TABLE 完了前は dismissed カラムが存在しないためスキップ
        try:
            # severity ガード: critical の findings は auto-promotion 禁止（明示的 opt-in のみ）
            # repo_root フィルタ: 他プロジェクトの dismissed パターンを学習しない（クロスコンタミネーション防止）
            if repo_root:
                raw = conn.execute("""
                    SELECT category, fp_reason, COUNT(*) AS cnt
                    FROM findings
                    WHERE dismissed = 1
                      AND dismissed_by = 'user'
                      AND fp_reason IS NOT NULL
                      AND fp_reason != ''
                      AND severity != 'critical'
                      AND replace(COALESCE(repo_root, ''), '\\', '/') = ?
                    GROUP BY category, fp_reason
                    HAVING cnt >= 2     -- 2回以上承認されたパターンのみ学習
                    ORDER BY cnt DESC
                    LIMIT 20
                """, (repo_root,)).fetchall()
            else:
                # repo_root 不明時はフィルタなし（git 管理外からの SessionEnd）
                raw = conn.execute("""
                    SELECT category, fp_reason, COUNT(*) AS cnt
                    FROM findings
                    WHERE dismissed = 1
                      AND dismissed_by = 'user'
                      AND fp_reason IS NOT NULL
                      AND fp_reason != ''
                      AND severity != 'critical'
                    GROUP BY category, fp_reason
                    HAVING cnt >= 2     -- 2回以上承認されたパターンのみ学習
                    ORDER BY cnt DESC
                    LIMIT 20
                """).fetchall()
            # sqlite3.Row は接続クローズ後の参照が不安定なため、
            # conn.close() を呼ぶ前に Python ネイティブの tuple に変換する
            rows = [tuple(r) for r in raw]
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

    new_entries = [f"- [{r[0]}] {_sanitize_fp_reason(r[1] or '')} （{r[2]}回承認）" for r in rows]
    # block の先頭は '\n##' （改行1つ + 見出し）。
    # content 末尾を '\n' に正規化してから結合することで、ファイル内では空行1行が挿入される。
    # （content末尾\n + block先頭\n = \n\n → 空行1行）
    # 注意: 日付ラベルは CLAUDE.md に書かない（グローバルルール: 日付・進捗宣言禁止）
    block = (
        "\n## 学習済み false positive パターン（自動生成）\n"
        + "\n".join(new_entries)
        + "\n"  # 末尾改行: エディタの「末尾改行なし」警告を防ぐ
    )

    content = claude_md_path.read_text(encoding="utf-8")

    # 既存の自動追記ブロックを更新（重複防止）
    # splitlines() で行単位処理（CRLF 対応: re.DOTALL + \n^## パターンが CRLF で誤動作するのを防ぐ）
    marker = "## 学習済み false positive パターン"
    if any(line.startswith(marker) for line in content.splitlines()):
        lines = content.splitlines(keepends=True)
        result_lines = []
        in_block = False
        for line in lines:
            stripped = line.rstrip("\r\n")
            if stripped.startswith(marker):
                in_block = True
                continue
            if in_block and re.match(r'^#{1,6}\s', stripped):
                # 次のセクション開始で block 終了
                # 末尾ブロックの場合: 次のセクション見出しが現れずに for を抜けるため
                # in_block=True のままループ終了するが、そのブロック内の行は
                # result_lines に一切追加されていないため、末尾ブロックは正しく削除済み。
                # re.sub(r'[\r\n]+$', '\n', ...) が残存する余分な空行を吸収するため
                # ファイル末尾の整形も保証される。
                in_block = False
                result_lines.append(line)
                continue
            if not in_block:
                result_lines.append(line)
        # 末尾の改行を正規化: 余分な改行を除去して \n 1つに統一
        # block が '\n##' 始まりのため、content 末尾が \n1つなら \n\n（空行1行）が挿入される
        content = re.sub(r'[\r\n]+$', '\n', "".join(result_lines))
    else:
        # 既存ブロックがない場合も末尾を \n 1つに正規化
        content = re.sub(r'[\r\n]+$', '\n', content)

    # アトミック書き込み: temp ファイルに書いてから os.replace でアトミックに置換する
    # CLAUDE.md の同時書き込みによる lost update を防ぐ（NTFS では MoveFileEx がアトミック）
    # block は '\n## で始まるため content 末尾の \n と合わせて \n\n（空行1行）が挿入される
    tmp_path = None  # except ブロックで未定義の場合の NameError を防ぐ
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8',
            dir=claude_md_path.parent,
            suffix='.tmp', delete=False
        ) as tmp:
            tmp.write(content + block)
            tmp_path = tmp.name
        os.replace(tmp_path, str(claude_md_path))
    except OSError as e:
        # アトミック書き込み失敗時は temp ファイルを削除してフォールバック
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        print(f"[session-end-learn] CLAUDE.md 書き込みエラー: {e}", file=sys.stderr)


if __name__ == "__main__":
    # クリーンアップを main() より先に実行（セッション終了時の後片付けとして機能させる）
    try:
        _cleanup_inject_state()
    except OSError:
        pass
    main()
