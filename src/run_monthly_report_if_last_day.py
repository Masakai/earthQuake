#!/usr/bin/env python3
"""
毎月1日のみ、前月分の monthly_report.py を実行し GitHub Pages へ公開するラッパー。
launchd から毎日 05:00 に呼び出される。

処理フロー:
  1. monthly_report.py を実行して data/monthly_report/report_YYYYMM.html を生成
  2. docs/reports/ にコピー
  3. docs/index.html のリンクバーに新エントリを追加
  4. master ブランチへ git commit & push
     → GitHub Pages (master/docs) が自動更新される
"""

import datetime
import pathlib
import subprocess
import sys

BASE_DIR   = pathlib.Path(__file__).parent.parent
LOG_FILE   = BASE_DIR / 'logs' / 'fetch_p2p.log'
DOCS_DIR   = BASE_DIR / 'docs'
REPORTS_DIR = DOCS_DIR / 'reports'
GIT        = '/usr/bin/git'


def log(msg: str):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True)


def publish_to_pages(year: int, month: int, generated_at: str) -> bool:
    """docs/reports/ にレポートをコピーして master へ push する。"""
    report_name = f'report_{year}{month:02d}.html'
    report_src  = BASE_DIR / 'data' / 'monthly_report' / report_name

    if not report_src.exists():
        log(f'[ERR] レポートファイルが見つかりません: {report_src}')
        return False

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = REPORTS_DIR / report_name
    dest.write_bytes(report_src.read_bytes())
    log(f'docs/reports/{report_name} にコピーしました')

    # docs/index.html にリンクを追加
    _update_index(year, month, generated_at)

    # コミット
    run([GIT, 'add', f'docs/reports/{report_name}', 'docs/index.html'])
    staged = run([GIT, 'diff', '--cached', '--quiet'])
    if staged.returncode == 0:
        log('変更なし。push をスキップします')
        return True

    msg = f'report: {year}年{month}月 月次レポート公開'
    r = run([GIT, 'commit', '-m', msg])
    if r.returncode != 0:
        log(f'[ERR] git commit 失敗: {r.stderr.strip()}')
        return False

    r = run([GIT, 'push', 'origin', 'master'])
    if r.returncode != 0:
        log(f'[ERR] git push 失敗: {r.stderr.strip()}')
        return False

    url = f'https://masakai.github.io/earthQuake/reports/{report_name}'
    log(f'GitHub Pages 公開完了: {url}')
    return True


def _update_index(year: int, month: int, generated_at: str):
    """docs/index.html のリンクバーに月次レポートのエントリを追加する（重複スキップ）。"""
    index_path  = DOCS_DIR / 'index.html'
    report_name = f'report_{year}{month:02d}.html'
    link_href   = f'reports/{report_name}'

    content = index_path.read_text(encoding='utf-8')
    if report_name in content:
        return

    new_entry = (
        f'        <div class="link-item">\n'
        f'            <a href="{link_href}">📊 {year}年{month}月 月次地震レポート</a>\n'
        f'            <span>AM.R38DC 観測点による P2P地震情報まとめ（{generated_at} 生成・私的記録）</span>\n'
        f'        </div>'
    )

    # link-bar-inner 閉じ </div> の直前に挿入
    # docs/index.html の実際の構造: 最後の link-item → </div>\n</div>\n\n<!-- フッター -->\n<footer>
    marker = '\n    </div>\n</div>\n\n<!-- フッター -->'
    if marker not in content:
        log('[WARN] index.html のマーカーが見つかりません。リンク追加をスキップします')
        return

    content = content.replace(marker, '\n' + new_entry + marker, 1)
    index_path.write_text(content, encoding='utf-8')
    log(f'docs/index.html にレポートリンクを追加しました')


def main():
    today = datetime.date.today()

    if today.day != 1:
        sys.exit(0)

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

    # 2. GitHub Pages (master/docs) へ公開
    generated_at = datetime.datetime.now().strftime('%Y-%m-%d')
    ok = publish_to_pages(year, month, generated_at)
    if not ok:
        log('[WARN] GitHub Pages への公開に失敗しました（レポート自体は生成済み）')
        sys.exit(1)


if __name__ == '__main__':
    main()
