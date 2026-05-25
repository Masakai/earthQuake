#!/usr/bin/env python3
"""
国土数値情報（国土交通省）の行政区域データ（N03）をダウンロードし、
都道府県別・市区町村別GeoJSONに変換して保存する。

出典：国土交通省国土数値情報ダウンロードサイト
      https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-N03-2024.html

ライセンス：公共データ利用規約（PDL1.0）

使い方:
    .venv/bin/python src/download_geojson.py

保存先: data/geojson/{都道府県コード2桁}/{市区町村コード5桁}.json
"""
import io
import json
import pathlib
import time
import urllib.request
import zipfile

BASE_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2024"
FILENAME_TMPL = "N03-20240101_{pref}_GML.zip"
OUT_DIR = pathlib.Path(__file__).parent.parent / "data" / "geojson"

PREF_CODES = [f"{i:02d}" for i in range(1, 48)]


def process_pref(pref_code: str) -> int:
    pref_dir = OUT_DIR / pref_code
    pref_dir.mkdir(parents=True, exist_ok=True)

    # すでに全ファイルが存在する場合はスキップ
    existing = list(pref_dir.glob("*.json"))
    if existing:
        print(f"[{pref_code}] スキップ（{len(existing)}件既存）")
        return len(existing)

    url = f"{BASE_URL}/{FILENAME_TMPL.format(pref=pref_code)}"
    print(f"[{pref_code}] ダウンロード中...", end=" ", flush=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rs4d-geojson/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except Exception as e:
        print(f"失敗: {e}")
        return 0

    print(f"{len(data)//1024}KB 変換中...", end=" ", flush=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            geojson_names = [n for n in zf.namelist() if n.endswith('.geojson')]
            if not geojson_names:
                print("GeoJSONファイルなし")
                return 0
            raw = zf.read(geojson_names[0])

        fc = json.loads(raw.decode('utf-8'))

        # 市区町村コード（N03_007）ごとに分割して保存
        city_features: dict[str, list] = {}
        for feat in fc.get('features', []):
            code = feat.get('properties', {}).get('N03_007', '').strip()
            if not code:
                continue
            city_features.setdefault(code, []).append(feat)

        saved = 0
        for city_code, features in city_features.items():
            out_path = pref_dir / f"{city_code}.json"
            out_fc = {"type": "FeatureCollection", "features": features}
            out_path.write_text(json.dumps(out_fc, ensure_ascii=False), encoding='utf-8')
            saved += 1

        print(f"{saved}市区町村 保存完了")
        return saved

    except Exception as e:
        print(f"変換失敗: {e}")
        return 0


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"保存先: {OUT_DIR}")
    print(f"出典：国土交通省国土数値情報ダウンロードサイト (PDL1.0)\n")

    total = 0
    for pref_code in PREF_CODES:
        total += process_pref(pref_code)
        time.sleep(0.5)  # サーバー負荷軽減

    print(f"\n完了。合計 {total} 市区町村のGeoJSONを保存しました。")


if __name__ == "__main__":
    main()
