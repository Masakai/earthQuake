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

# ===== imac（本番計算ループ機）からの自局データ取得設定 =====
# 自局トリガ取得（計算ループ）は本番の imac で動いており、最新の trigger_log は
# imac 側にある。レポート生成・GitHub Pages 公開はこの MacBook（git リポジトリ）から
# 行うため、生成前に imac から trigger_log を取得する。鍵認証(BatchMode)で接続できる
# ことが前提。失敗してもレポート生成は続行する。
# P2P 地震リストは API から直接取れるため imac 同期は不要（この機で fetch_p2p_daily を実行）。
SSH        = '/usr/bin/ssh'
SCP        = '/usr/bin/scp'
IMAC_HOST  = 'imac'
IMAC_TRIGGER_LOG = '~/Dropbox/earthQuake/logs/trigger_log.jsonl'


def sync_trigger_log_from_imac() -> bool:
    """imac から最新の trigger_log（自局トリガ）をこの機へ取得する。

    成功で True。失敗しても呼び出し側はレポート生成を続行する
    （古いローカル trigger_log でのフォールバック）。
    """
    chk = subprocess.run(
        [SSH, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', IMAC_HOST, 'echo ok'],
        capture_output=True, text=True,
    )
    if chk.returncode != 0:
        log(f'[WARN] imac へ SSH 接続できません。ローカルの trigger_log で生成します: '
            f'{chk.stderr.strip()}')
        return False

    trigger_dest = BASE_DIR / 'logs' / 'trigger_log.jsonl'
    r1 = subprocess.run(
        [SCP, '-o', 'BatchMode=yes', f'{IMAC_HOST}:{IMAC_TRIGGER_LOG}', str(trigger_dest)],
        capture_output=True, text=True,
    )
    if r1.returncode == 0:
        log('imac から trigger_log.jsonl を取得しました')
        return True
    log(f'[WARN] trigger_log の取得に失敗: {r1.stderr.strip()}')
    return False


def fetch_p2p_local() -> bool:
    """この機で fetch_p2p_daily.py を実行し P2P キャッシュを最新化する。"""
    python = BASE_DIR / '.venv' / 'bin' / 'python'
    script = BASE_DIR / 'src' / 'fetch_p2p_daily.py'
    r = subprocess.run([str(python), str(script)],
                       cwd=str(BASE_DIR), capture_output=True, text=True)
    for line in (r.stdout + r.stderr).splitlines():
        log(line)
    if r.returncode == 0:
        log('P2P キャッシュを最新化しました')
        return True
    log(f'[WARN] P2P 取得に失敗 (returncode={r.returncode})')
    return False


def log(msg: str):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True)


def generate_ogp(year: int, month: int) -> bool:
    """OGP画像を生成して data/monthly_report/ に保存する。"""
    yyyymm = f'{year}{month:02d}'
    ogp_script = BASE_DIR / 'data' / 'monthly_report' / f'generate_ogp_{yyyymm}.py'

    if not ogp_script.exists():
        log(f'[WARN] OGP生成スクリプトが見つかりません: {ogp_script}')
        return False

    python = BASE_DIR / '.venv' / 'bin' / 'python'
    result = subprocess.run(
        [str(python), str(ogp_script)],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log(f'[WARN] OGP画像生成失敗: {result.stderr.strip()}')
        return False

    log(f'OGP画像生成完了: {result.stdout.strip()}')
    return True


def publish_to_pages(year: int, month: int, generated_at: str) -> bool:
    """docs/reports/ にレポートをコピーして master へ push する。"""
    yyyymm = f'{year}{month:02d}'
    report_name = f'report_{yyyymm}.html'
    ogp_name    = f'ogp_{yyyymm}.png'
    report_src  = BASE_DIR / 'data' / 'monthly_report' / report_name
    ogp_src     = BASE_DIR / 'data' / 'monthly_report' / ogp_name

    if not report_src.exists():
        log(f'[ERR] レポートファイルが見つかりません: {report_src}')
        return False

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = REPORTS_DIR / report_name
    dest.write_bytes(report_src.read_bytes())
    log(f'docs/reports/{report_name} にコピーしました')

    # OGP画像をコピー
    files_to_add = [f'docs/reports/{report_name}', 'docs/index.html']
    if ogp_src.exists():
        ogp_dest = REPORTS_DIR / ogp_name
        ogp_dest.write_bytes(ogp_src.read_bytes())
        log(f'docs/reports/{ogp_name} にコピーしました')
        files_to_add.append(f'docs/reports/{ogp_name}')
    else:
        log(f'[WARN] OGP画像が見つかりません: {ogp_src}')

    # docs/index.html にリンクを追加
    _update_index(year, month, generated_at)

    # コミット
    run([GIT, 'add'] + files_to_add)
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
    """docs/index.html の月次レポート索引セクションに新エントリを追加する（重複スキップ）。"""
    index_path  = DOCS_DIR / 'index.html'
    report_name = f'report_{year}{month:02d}.html'
    link_href   = f'reports/{report_name}'

    content = index_path.read_text(encoding='utf-8')
    if report_name in content:
        return

    new_entry = (
        f'            <a class="report-card" href="{link_href}">\n'
        f'                <span class="report-month">{year}年{month}月</span>\n'
        f'                <span class="report-desc">P2P地震情報まとめ・自局検出記録</span>\n'
        f'                <span class="report-arrow">→</span>\n'
        f'            </a>'
    )

    # report-list の末尾カード直後に挿入
    marker = '\n        </div>\n        <p class="report-notice">'
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

    # 0a. imac から最新の自局 trigger_log を取得（計算ループは imac で稼働）
    sync_trigger_log_from_imac()
    # 0b. P2P 地震リストをこの機で最新化（API から直接取得）
    fetch_p2p_local()

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

    # 2. OGP画像生成
    generate_ogp(year, month)

    # 3. GitHub Pages (master/docs) へ公開
    generated_at = datetime.datetime.now().strftime('%Y-%m-%d')
    ok = publish_to_pages(year, month, generated_at)
    if not ok:
        log('[WARN] GitHub Pages への公開に失敗しました（レポート自体は生成済み）')
        sys.exit(1)


if __name__ == '__main__':
    main()
