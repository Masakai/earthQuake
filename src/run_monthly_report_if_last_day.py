#!/usr/bin/env python3
"""
毎月1日のみ、前月分の monthly_report.py を実行し GitHub Pages へ公開するラッパー。
launchd から毎日 05:00 に呼び出される。

処理フロー:
  1. monthly_report.py を実行して report_YYYYMM.html を生成
  2. gh-pages ブランチに HTML をコピーして git push
  3. index.html のレポートリストを更新
"""

import datetime
import pathlib
import re
import subprocess
import sys

BASE_DIR = pathlib.Path(__file__).parent.parent
LOG_FILE = BASE_DIR / 'logs' / 'fetch_p2p.log'
GIT      = '/usr/bin/git'


def log(msg: str):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def run(cmd: list[str], cwd: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd or str(BASE_DIR),
                          capture_output=True, text=True)


def publish_to_gh_pages(year: int, month: int, generated_at: str):
    """生成済み HTML を gh-pages ブランチに push する。"""
    report_name = f'report_{year}{month:02d}.html'
    report_src  = BASE_DIR / 'data' / 'monthly_report' / report_name

    if not report_src.exists():
        log(f'[ERR] レポートファイルが見つかりません: {report_src}')
        return False

    # gh-pages ブランチに切り替え
    r = run([GIT, 'checkout', 'gh-pages'])
    if r.returncode != 0:
        log(f'[ERR] gh-pages チェックアウト失敗: {r.stderr.strip()}')
        return False

    try:
        # レポート HTML をコピー
        dest = BASE_DIR / report_name
        dest.write_bytes(report_src.read_bytes())

        # index.html を更新（既存リストに新エントリを追加）
        _update_index(year, month, generated_at)

        # コミット & push
        run([GIT, 'add', report_name, 'index.html'])

        # ステージされた変更があるか確認
        staged = run([GIT, 'diff', '--cached', '--quiet'])
        if staged.returncode == 0:
            # 差分なし（既に同内容でコミット済み）
            log('変更なし。push をスキップします')
            return True

        msg = f'report: {year}年{month}月 月次レポート公開'
        r = run([GIT, 'commit', '-m', msg])
        if r.returncode != 0:
            log(f'[ERR] git commit 失敗: {r.stderr.strip()}')
            return False

        r = run([GIT, 'push', 'origin', 'gh-pages'])
        if r.returncode != 0:
            log(f'[ERR] git push 失敗: {r.stderr.strip()}')
            return False

        log(f'GitHub Pages 公開完了: https://masakai.github.io/earthQuake/{report_name}')
        return True

    finally:
        # 必ず master に戻る
        run([GIT, 'checkout', 'master'])


def _update_index(year: int, month: int, generated_at: str):
    """index.html のレポートリストに新エントリを追加する（重複スキップ）。"""
    index_path  = BASE_DIR / 'index.html'
    report_name = f'report_{year}{month:02d}.html'
    new_entry   = (
        f'    <li><a href="{report_name}">\n'
        f'      {year}年{month}月 地震レポート\n'
        f'      <div class="meta">{generated_at} 生成</div>\n'
        f'    </a></li>'
    )

    content = index_path.read_text(encoding='utf-8')

    # 既に同月のエントリがあればスキップ
    if report_name in content:
        return

    # <!-- レポートリンクはここに追加されていきます --> の直後に挿入
    marker = '<!-- レポートリンクはここに追加されていきます -->'
    content = content.replace(marker, marker + '\n' + new_entry)
    index_path.write_text(content, encoding='utf-8')


def main():
    today = datetime.date.today()

    if today.day != 1:
        sys.exit(0)

    # 前月を計算
    first_of_this_month = today.replace(day=1)
    last_month = first_of_this_month - datetime.timedelta(days=1)
    year, month = last_month.year, last_month.month

    log(f'月初({today})のため前月({year}年{month}月)の月次レポートを生成します')

    # 1. monthly_report.py 実行
    script = BASE_DIR / 'src' / 'monthly_report.py'
    python = BASE_DIR / '.venv' / 'bin' / 'python'
    result = subprocess.run(
        [str(python), str(script), str(year), str(month)],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        log(line)
    for line in result.stderr.splitlines():
        log(f'[ERR] {line}')
    if result.returncode != 0:
        log(f'月次レポート生成失敗 (returncode={result.returncode})')
        sys.exit(1)
    log('月次レポート生成完了')

    # 2. GitHub Pages へ公開
    generated_at = datetime.datetime.now().strftime('%Y-%m-%d')
    ok = publish_to_gh_pages(year, month, generated_at)
    if not ok:
        log('[WARN] GitHub Pages への公開に失敗しました（レポート自体は生成済み）')
        sys.exit(1)


if __name__ == '__main__':
    main()
