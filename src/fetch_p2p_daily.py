#!/usr/bin/env python3
"""
P2P地震情報 日次データ収集スクリプト。
当日分の地震データを取得し data/p2p_cache/YYYYMM.jsonl に追記する。
launchdから毎日1回呼び出される想定。

使い方:
    python src/fetch_p2p_daily.py          # 当日分
    python src/fetch_p2p_daily.py 2026 5   # 指定年月の全日分（手動補完用）
"""

import argparse
import datetime
import json
import pathlib
import sys
import time
import urllib.request

BASE_DIR  = pathlib.Path(__file__).parent.parent
CACHE_DIR = BASE_DIR / 'data' / 'p2p_cache'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE  = BASE_DIR / 'logs' / 'fetch_p2p.log'
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def fetch_page(offset: int, limit: int = 100) -> list[dict]:
    url = f'https://api.p2pquake.net/v2/history?codes=551&limit={limit}&offset={offset}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def parse_quake(eq: dict) -> dict | None:
    """APIレスポンスの1エントリを正規化する。不正・震源なしはNoneを返す。"""
    info = eq.get('earthquake', {})
    t_str = info.get('time', '')
    if not t_str:
        return None
    try:
        dt = datetime.datetime.strptime(t_str[:16], '%Y/%m/%d %H:%M')
    except ValueError:
        return None
    hypo = info.get('hypocenter', {})
    lat  = hypo.get('latitude',  None)
    lon  = hypo.get('longitude', None)
    name = hypo.get('name',      '')
    if not name or lat is None or lon is None:
        return None
    return {
        'id':    eq.get('id', ''),
        'time':  t_str,
        'year':  dt.year,
        'month': dt.month,
        'day':   dt.day,
        'name':  name,
        'lat':   lat,
        'lon':   lon,
        'mag':   hypo.get('magnitude', -1),
        'depth': hypo.get('depth',     -1),
        'scale': info.get('maxScale',  -1),
    }


def quake_key(rec: dict) -> str:
    """同一地震を識別するキー: 発生時刻(分まで) + 震源名。"""
    return rec['time'][:16] + '_' + rec['name']


def load_cache(year: int, month: int) -> dict[str, dict]:
    """キャッシュファイルを読み込み、quake_key→レコードのdictを返す。
    同一地震で複数報がある場合は scale が大きい（確定報）を優先する。
    """
    path = CACHE_DIR / f'{year}{month:02d}.jsonl'
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            key = quake_key(rec)
            if key not in records or rec['scale'] > records[key]['scale']:
                records[key] = rec
        except Exception:
            pass
    return records


def save_records(year: int, month: int, records: dict[str, dict]):
    """レコードをJSONLファイルに上書き保存する（時刻順ソート）。"""
    path = CACHE_DIR / f'{year}{month:02d}.jsonl'
    sorted_recs = sorted(records.values(), key=lambda r: r['time'])
    with path.open('w', encoding='utf-8') as f:
        for rec in sorted_recs:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def fetch_for_month(year: int, month: int) -> int:
    """指定年月のデータをAPIから取得してキャッシュに追記。追加件数を返す。"""
    target_ym = (year, month)
    cutoff = datetime.datetime(year, month, 1)

    existing = load_cache(year, month)
    added = 0
    offset = 0

    while True:
        try:
            batch = fetch_page(offset)
        except Exception as e:
            log(f'[WARN] API取得失敗 offset={offset}: {e}')
            break

        if not batch:
            break

        exhausted = False
        for eq in batch:
            rec = parse_quake(eq)
            if rec is None:
                continue
            dt = datetime.datetime.strptime(rec['time'][:16], '%Y/%m/%d %H:%M')
            if (dt.year, dt.month) < target_ym:
                exhausted = True
                break
            if (dt.year, dt.month) != target_ym:
                continue
            key = quake_key(rec)
            prev = existing.get(key)
            if prev and prev['scale'] >= rec['scale']:
                continue
            if not prev:
                added += 1
            existing[key] = rec

        if exhausted:
            break
        offset += 100
        time.sleep(0.3)

    save_records(year, month, existing)
    return added


def main():
    ap = argparse.ArgumentParser(description='P2P地震情報 日次収集')
    ap.add_argument('year',  type=int, nargs='?', default=datetime.date.today().year)
    ap.add_argument('month', type=int, nargs='?', default=datetime.date.today().month)
    args = ap.parse_args()

    year, month = args.year, args.month
    log(f'収集開始: {year}年{month}月')
    added = fetch_for_month(year, month)
    cache_path = CACHE_DIR / f'{year}{month:02d}.jsonl'
    total = sum(1 for _ in cache_path.open(encoding='utf-8')) if cache_path.exists() else 0
    log(f'収集完了: {added}件追加 / 累計{total}件 → {cache_path}')


if __name__ == '__main__':
    main()
