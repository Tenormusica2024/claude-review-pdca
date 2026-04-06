#!/usr/bin/env python3
"""
repo_root バックフィルスクリプト。

findings テーブルの repo_root IS NULL レコードに対して、
file_path から git リポジトリルートを推定して更新する。

使い方:
  python scripts/backfill-repo-root.py          # dry-run（変更なし）
  python scripts/backfill-repo-root.py --apply   # 実際に更新
"""
import sys
import sqlite3
import subprocess
import argparse
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
# normalize_git_root は config.py から import（DRY: 3箇所の重複を排除）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import DB_PATH, normalize_git_root


def resolve_absolute_path(file_path: str) -> str | None:
    """相対パスを絶対パスに解決する。
    file_path が相対パスの場合、ホームディレクトリを基準に解決する。
    存在しないパスは None を返す。"""
    p = Path(file_path)
    if not p.is_absolute():
        # 相対パスはホームディレクトリ基準で解決（Claude Code の cwd がホームのため）
        p = Path.home() / p
    return str(p) if p.exists() else None


def get_git_root(file_path: str) -> str | None:
    """ファイルパスから git リポジトリルートを取得する。"""
    parent = Path(file_path).parent
    if not parent.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            return normalize_git_root(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


# git root のキャッシュ（同一ディレクトリの二重呼び出し防止）
_git_root_cache: dict[str, str | None] = {}


def get_git_root_cached(file_path: str) -> str | None:
    """ディレクトリ単位でキャッシュした git root 取得。"""
    parent = str(Path(file_path).parent)
    if parent not in _git_root_cache:
        _git_root_cache[parent] = get_git_root(file_path)
    return _git_root_cache[parent]


def main():
    parser = argparse.ArgumentParser(description="repo_root バックフィル")
    parser.add_argument("--apply", action="store_true", help="実際に DB を更新する（省略時は dry-run）")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row

    # repo_root IS NULL のレコードを取得
    rows = conn.execute("""
        SELECT id, file_path
        FROM findings
        WHERE repo_root IS NULL AND file_path IS NOT NULL
        ORDER BY id
    """).fetchall()

    print(f"対象レコード数: {len(rows)}")
    if not rows:
        print("バックフィル対象なし")
        conn.close()
        return

    # 統計
    resolved = 0
    not_found = 0
    no_git = 0
    updates: list[tuple[str, int]] = []  # (repo_root, id)
    stats: dict[str, int] = defaultdict(int)

    for row in rows:
        fid = row["id"]
        fp = row["file_path"]

        # 絶対パスに解決
        abs_path = resolve_absolute_path(fp)
        if abs_path is None:
            not_found += 1
            continue

        # git root 取得（キャッシュ活用）
        root = get_git_root_cached(abs_path)
        if root is None:
            no_git += 1
            continue

        updates.append((root, fid))
        stats[root] += 1
        resolved += 1

    # --- Phase 2: セッションベース推定 ---
    # 同一セッション内で repo_root が「1種類だけ」判明しているケースに限定して推定
    # 複数 repo_root があるセッションは安全のためスキップ
    session_resolved = 0  # Phase 2 未実行時でもレポートで参照するため事前初期化
    remaining_ids = {row["id"] for row in rows} - {u[1] for u in updates}
    if remaining_ids:
        session_roots = conn.execute("""
            SELECT session_id, GROUP_CONCAT(DISTINCT repo_root) as roots,
                   COUNT(DISTINCT repo_root) as root_count
            FROM findings
            WHERE session_id IS NOT NULL AND session_id != ''
              AND repo_root IS NOT NULL
            GROUP BY session_id
            HAVING root_count = 1
        """).fetchall()

        # セッション → 唯一の repo_root マッピング
        session_root_map = {r["session_id"]: r["roots"] for r in session_roots}

        session_multi_root = 0
        # 残りの NULL レコードのセッションを確認
        remaining_rows = conn.execute("""
            SELECT id, session_id
            FROM findings
            WHERE repo_root IS NULL AND session_id IS NOT NULL AND session_id != ''
            ORDER BY id
        """).fetchall()

        for row in remaining_rows:
            fid = row["id"]
            sid = row["session_id"]
            if sid in session_root_map:
                root = session_root_map[sid]
                updates.append((root, fid))
                stats[root] = stats.get(root, 0) + 1
                session_resolved += 1

        print(f"\n=== Phase 2: セッションベース推定 ===")
        print(f"  セッション推定可能: {session_resolved}")
        resolved += session_resolved

    # レポート
    print(f"\n=== バックフィル総合結果 ===")
    print(f"  Phase 1 (git root): {resolved - (session_resolved if remaining_ids else 0)}")
    if remaining_ids:
        print(f"  Phase 2 (session): {session_resolved}")
    print(f"  合計解決: {resolved}")
    print(f"  ファイル不存在: {not_found}")
    print(f"  git 管理外: {no_git}")
    print(f"\n=== repo_root 分布 ===")
    for root, cnt in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {root}: {cnt} 件")

    if args.apply and updates:
        print(f"\n--- {len(updates)} 件を更新中 ---")
        conn.executemany("""
            UPDATE findings SET repo_root = ? WHERE id = ?
        """, updates)
        conn.commit()
        print("更新完了")
    elif updates:
        print(f"\n[dry-run] --apply を付けると {len(updates)} 件が更新されます")

    conn.close()


if __name__ == "__main__":
    main()
