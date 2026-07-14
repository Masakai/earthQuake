# earthQuakeプロジェクト 現状評価レポート

- 作成日: 2026-07-14
- 対象: リポジトリ全体（コード・ドキュメント・外部公開ページ）
- 目的: プロジェクトの現状分析と、READMEおよびGitHub Pagesの改訂・その他機能改善プランの前提となる事実整理

本レポートは断定的な性能評価ではなく、コード・ドキュメント・公開ページを実際に確認した結果の記録である。実装済み機能と未実装機能を明確に区別する。

---

## 1. システムの全体構成

3つの実行系統が並存する。

1. **リアルタイム系**（常時稼働、iMac本番機）: `jma_intensity_web.py`（FastAPI+uvicorn、HTTP 8080/UDP 8888）が`jma_intensity_tui.py`のコア（`SharedState`・`compute_loop`・`recv_loop_fn`・`AlertSpeaker`）をimportして使う。TUI版（`jma_intensity_tui.py`単体実行）も同じコアを共有する。
2. **オンデマンド解析系**: `analyze_rs.py`（波形解析・スペクトログラム）、`analyze_knet.py`（K-NET/KiK-net解析）、`microseism.py`（マイクロセイズム診断図）。Webダッシュボードの「解析」ボタンから`analyze_rs.py`がサブプロセスとして起動される（`jma_intensity_web.py:584-626`）。
3. **バッチ系**（launchd定期実行）: `fetch_p2p_daily.py`（日次P2Pキャッシュ収集、開発機）、`monthly_report.py`+`run_monthly_report_if_last_day.py`（月次レポート生成、開発機）、`hvsr_weekly.py`（週次HVSR計算、iMac、2026-07-14新設）。

**運用ホストが機能ごとに分かれている**点が特徴的である。常時稼働・LAN依存（Raspberry Shake本体へのUDP/SeedLinkアクセス）が必要な機能はiMac、git操作を要する機能（月次レポート→GitHub Pages push）は開発機、という分担になっている（`docs/MANUAL.md`・プロジェクトメモリに詳細）。iMac側はgit管理外でscpによる手動デプロイに依存する。

## 2. 主要コンポーネント

| ファイル | 役割 | 行数目安 |
|---|---|---|
| `src/jma_intensity_realtime.py` | JMAフィルタ・Ringバッファ・震度換算のコアライブラリ | 13,346 |
| `src/jma_intensity_tui.py` | SharedState・計算ループ・音声アラート（web.pyが依存するコア） | 44,000 |
| `src/jma_intensity_web.py` | Webダッシュボード本体（FastAPI） | 38,480 |
| `src/analyze_rs.py` | 波形解析・スペクトログラム・震源地図 | 52,327 |
| `src/analyze_knet.py` | K-NET/KiK-net強震記録解析 | 25,889 |
| `src/microseism.py` | マイクロセイズム診断図（3成分PSD・H/V比・昼夜比較） | 66,143 |
| `src/monthly_report.py` | 月次レポート生成 | 46,649 |
| `src/beachball_map.py` | 発震機構解（地震球）マップ | 34,088 |
| `src/hvsr_weekly.py` | HVSR週次計算バッチ（新設） | 25,306 |
| `src/fetch_p2p_daily.py` | P2P地震情報の日次キャッシュ収集 | 6,865 |
| `src/run_monthly_report_if_last_day.py` | 月次レポート生成の月初トリガ | 9,256 |

## 3. データフロー

```
Raspberry Shake 4D
  └─ UDP DATACAST ──> jma_intensity_tui.recv_loop_fn ──> SharedState
                                                            ├─ compute_loop（震度算出・STA/LTA検出・音声アラート）
                                                            └─ jma_intensity_web.broadcast_loop ──> WebSocket ──> ブラウザ

P2P地震情報API ──> fetch_p2p_daily.py（日次キャッシュ） ──> monthly_report.py ──> HTML ──> GitHub Pages push
                └─> jma_intensity_web.py（WebUIのP2Pパネル、リアルタイム表示）

FDSN公式/自局SeedLink ──> analyze_rs.py（オンデマンド解析） / hvsr_weekly.py（週次バッチ） ──> data/hvsr_history.jsonl ──> GET /api/hvsr_history ──> WebUI
```

## 4. リアルタイム処理の流れ

`recv_loop_fn`がUDPを受信しRingバッファに蓄積、`compute_loop`が一定間隔でJMAフィルタ・0.3秒閾値・計測震度計算・STA/LTA判定を行い`SharedState`を更新する。`jma_intensity_web.py`の`broadcast_loop`は1秒間隔で`SharedState`のスナップショットをWebSocket経由の全クライアントへ配信する（`jma_intensity_web.py:326`）。

2026-07-14に追加された`GET /api/hvsr_history`はこのリアルタイム経路に一切参加しない、起動時1回fetchの読み取り専用エンドポイントであることをコードで確認済み（`jma_intensity_web.py:901`、`_hvsr_history_snapshot()`はmtimeベースの軽量キャッシュ）。

## 5. レポート生成の流れ

`monthly_report.py`がP2P日次キャッシュ・`trigger_log.jsonl`（自局検出照合）・地質メモを読み込み、震源分布図（geopandas）・発震機構解マップ（`beachball_map.py`）・統計・自局トピック・「解説・総評」文章を含むHTMLを`data/monthly_report/report_YYYYMM.html`に出力する。`run_monthly_report_if_last_day.py`が毎日05:00に実行され、月初のみ前月分を生成しGitHub Pagesへpushする。

実際に生成された2026年6月レポート（`docs/reports/report_202606.html`、814KB）を確認したところ、以下のセクションが既に存在する。

- 震源分布図／自局トピック（地盤特性の恒久注記込み）／発震機構解マップ／全体像／注目イベント／地域別の活動傾向／総括／解説・総評／日別発生件数／マグニチュード分布／地震一覧

**重要な訂正**: 「全体像」「注目イベント」「地域別の活動傾向」「総括」は`generate_commentary()`（`monthly_report.py:639`）による完全自動生成（統計値を条件分岐でテンプレート文に流し込む方式）である。一方「解説・総評」は`load_manual_commentary()`（`monthly_report.py:838`）が`data/monthly_report/commentary_YYYYMM.html`という**手動執筆ファイル**を読み込んで表示する仕組みであり、自動生成ではない（ファイルがなければプレースホルダ文言を表示する）。つまり詳細な自局データ言及・距離減衰の考察は自動化されたものではなく、都度手作業で書かれている。この点はユーザー指摘「月報が事実の列挙に寄りやすい」の実態を正確に説明する重要な事実であり、**自動生成部分（総括セクション等）は依然としてテンプレート文の域を出ていない**。以下は自動生成・手動執筆いずれの部分にも含まれていない。

- 検出率（自局トリガ数のうち何件が真の地震か、外部指摘との自動突合による定量値）としての明示
- 誤検出候補の切り分け（現在は「全国地震と時刻一致した27〜30件」の言及のみで、一致しなかった残り約2250件が何なのかの分類はない）
- 欠測時間・データ品質の言及
- 前月比較
- HVSR推移への言及（`grep`で1件のみヒット、実質的な連携なし）

## 6. GitHub Pages公開の流れ

`docs/index.html`（プロジェクトサイトトップ）、`docs/reports/*.html`（月次・特別レポート）がGitHub Pagesとして公開される。外部調査（fork調査、2026-07-14実施）の結果は以下の通り。

- **プロジェクトサイトトップ**: スクリーンショット3枚・機能10項目・月次レポートへの導線複数・GitHubへの導線を確認。ただし機能一覧にHVSR・地盤解析・月次レポート自動生成が独立項目として明示されていない可能性がある。タイトルは「リアルタイム震度モニター」（`docs/index.html`の`<title>`実測値と一致）。
- **月次レポート例**: エグゼクティブサマリーに相当する「解説・総評」はあるが免責文一行のみで事実/考察の分離が形式的。HVSR・マイクロセイズムへの言及は外部調査で確認できず。
- **Qiita記事**: 技術解説記事。GitHubへの直リンクはあるが、プロジェクトサイト（GitHub Pages）への言及はない。
- **ブログ記事一覧（3本）**: 「震度差の発見→地盤調査」という物語は存在するが、HVSRへの到達（物語の最新章）は未執筆。各記事からGitHub/プロジェクトサイトへの直接リンクはない。

**4媒体（GitHub / GitHub Pages / Qiita / ブログ）はほぼ相互リンクなしで孤立している。** Qiita→GitHubの一方向リンクのみ確認された。

## 7. コード上の強み

- JMAフィルタ・計測震度計算がNIED K-NET公式統計値との照合で検証されている（README:193-215、震度階級完全一致）。検証範囲と検証範囲外が明記されており、誠実な線引きがある。
- リアルタイム系とオンデマンド解析系・バッチ系が明確に分離されている。2026-07-14のHVSR機能追加時も設計書段階でこの分離が最上位制約として扱われ、実際にコード上も`SharedState`・`broadcast_loop`への変更はゼロだった（確認済み）。
- `hvsr_weekly.py`・`analyze_rs.py`とも、SSL証明書設定（iMac本番のpython.org製Pythonでの既知の問題への対処）・SeedLinkフォールバックなど、過去の障害から得た教訓がコードコメントとして残っている。
- テストスイートが機能ごとに分離されている（`verify_filter.py`41件、`test_api_events.py`23件、`test_hvsr_weekly.py`34件、`test_api_hvsr_history.py`12件、`test_city_matching.py`8件、`test_template_parity.py`）。

## 8. コード上の弱み

- **CI/CDが存在しない**（`.github/workflows/`が空）。テストは手動実行に依存しており、コミット・プッシュ時の自動検証がない。
- `hvsr_weekly.py`の`weather_note`フィールドが常に空文字でハードコードされている（`hvsr_weekly.py:530,550`）。降雨・強風がH/V比に影響する可能性（既存メモリ`project_rain_detection_ehz.md`で確認済みの知見）があるにもかかわらず、天候情報との突合機構が未実装。
- `peak_frequency_from_curve()`（`hvsr_weekly.py:286`）は単純に`np.nanargmax`で最大値のインデックスを取るのみで、複数の局所ピークやノイズフロアの単調増加を区別しない。実際に2026-07-14の初回実データ実行では、ピークがナイキスト近傍（15.89Hz）に出て曲線全体がほぼフラット（H/V比1.2〜1.6）という、明確な卓越周波数が検出できない結果になった（SESAME簡易基準の`amplitude_ok`/`stability_ok`とも`false`で、この曖昧さ自体は正しく検出されている）。
- `POST /api/analyze`の`duration`パラメータに上限バリデーションがない（`jma_intensity_web.py:646`、`int(body.get("duration", 420))`）。`subprocess.run(timeout=300)`で実害は抑えられているが、意図的に大きな値を送ると解析ジョブが無駄なリソースを消費する余地がある。
- `analyze_rs.py`（トップレベルでgeopandas・matplotlib読込）と`hvsr_weekly.py`の間で、`download_channel`/`download_channel_seedlink`/`compute_stalta`のロジックがコピーされ二重管理になっている（意図的な設計判断だが、将来の修正漏れリスクとして両ファイルにコメントで明記済み）。

## 9. ドキュメント上の強み

- `README.md`の「計測震度アルゴリズムの正当性検証」節は、検証範囲・検証範囲外を明記しており誠実。
- `docs/MANUAL.md`が詳細（本レポート未読了、フェーズ2以降で参照）。
- K-NET/KiK-netデータのNIED謝辞・DOI記載、VOICEVOXクレジット表記が整備されており、外部データ・素材のライセンス遵守が丁寧。

## 10. ドキュメント上の弱み（ユーザー指摘と一致する点）

- README冒頭が「Raspberry Shake 4Dの…ダッシュボード」という機能説明から始まり、1文の価値提案・差別化点・想定読者が示されない。
- README・GitHub Pagesとも、月次レポート自動生成・HVSR・地盤差検証・発震機構解マップという独自機能が「一覧の中の1項目」として埋没しており、これらを束ねる物語（設置→震度差発見→地盤調査→HVSR→継続観測）が提示されていない。
- 月次レポートに「今月何が重要だったか」の要約はあるが、検出率・誤検出候補・データ品質・前月比較・HVSR推移という定量的な自己評価指標が欠けている。
- 4媒体（GitHub/Pages/Qiita/ブログ）の相互リンクがほぼない。

## 11. 保守性

`analyze_rs.py`と`hvsr_weekly.py`の関数複製は意図的なトレードオフ（依存の軽量化）として文書化されており、無秩序な重複ではない。一方でCIがないため、複製元（`analyze_rs.py`）の変更が複製先（`hvsr_weekly.py`）に反映されているかは目視確認に依存する。

## 12. テスト容易性

pytestベースのテストスイートは整備されているが、実行はローカル・手動のみ。`test_template_parity.py`（Jinja2テンプレートとTUI表示の整合検証）はUIの崩れを検知する仕組みとして有効だが、CIに組み込まれていないため、PRやプッシュ時に自動で走らない。

## 13. 障害時の挙動

過去に台風6号接近時の停電（2026-06-03）で`bp_history`にデータ欠損が生じた記録がプロジェクトメモリにある。この種の欠測をレポート・WebUI側で明示的に扱う仕組み（「この期間はデータ欠損」の表示）は現状確認できていない。`compute_loop`・`recv_loop_fn`の例外処理は個別に存在するが（`jma_intensity_tui.py`内`try`/`except`が30箇所）、停電後の再起動時の欠測期間を後から可視化する機構は月報にもWebUIにも見当たらない。

## 14. データ品質上のリスク

- HVSR初回実データ（2026-07-13深夜）で有効窓237/539（棄却率56%）という、設計書の想定棄却率より高めの値が出た。深夜帯でも交通・機器ノイズの影響が想定より大きい可能性があり、継続監視が必要。
- 月次レポートの自局トリガ2500件超のうち、外部地震情報と時刻一致するのは30件程度（1%程度）。残り99%が何であるか（生活振動・交通振動・センサーノイズ等）の分類は現状ない。

## 15. 地震と生活ノイズの誤判定リスク

現在の検出はSTA/LTA単一指標（`compute_stalta`のratio、EHZにバンドパスフィルタ適用）に依存する。継続時間・三成分比・卓越周波数・高周波成分比といった追加特徴量による分類は未実装（ユーザー指摘のフェーズ4「イベント分類」に対応する項目、現状は「未実装」と明確に言える）。

## 16. セキュリティ上の懸念

- `POST /api/config`・`POST /api/analyze`は無認証。LAN内公開が前提と推測されるが、リポジトリ・ドキュメント上に「本システムはLAN内限定を前提とし、外部公開する場合はリバースプロキシ側で認証を行うこと」といった明示的な注意書きは確認できなかった。
- `.env`はgitignore対象で、git履歴への混入も確認されなかった（`git log --all -- .env`で0件）。この点は問題なし。
- iMac本番機がgit管理外でscp手動デプロイに依存しているため、デプロイ手順の実行者が変わった場合や記録が古い場合に本番の実ファイルと開発機のコードが乖離するリスクがある（実際に2026-07-14のデプロイ作業で、iMacがv1.5.2のままv1.6.0の変更が2週間以上未反映だったことが判明した）。

## 17. 運用上の懸念

- CIがないため、README・GitHub Pages・レポート生成コードに実装と乖離した記述が混入しても自動検知されない。
- 4媒体の孤立により、Qiita・ブログ経由の新規訪問者がプロジェクトの全体像（HVSR・月次レポート等）に到達しにくい。

---

以上がフェーズ1（現状分析）の報告である。フェーズ2（改善計画）・フェーズ3（情報設計）は別ドキュメントで続ける。
