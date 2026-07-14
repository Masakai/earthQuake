# 設計書: HVSR週次モニタリング機能

- 作成日: 2026-07-14
- 対象プロジェクト: earthQuake
- 要件トレーサビリティ: ユーザー合意事項（本セッション、2026-07-14会話）。試算根拠は2026-06-26山梨県東部・富士五湖M5.6（震度5強）データの試験計算。2026-07-14追記: 別セッションでの外部提案（帯域0.5-20Hz・窓長100-200秒案）を照合し不採用と判断。SESAME (2004) 信頼性クライテリアの一部を記録項目として追加（ユーザー承認済み）
- 関連Issue / PR: なし（新規機能）

## 背景・目的

R38DC観測点（RS4D、静岡県付近、緯度35.1426・経度138.9331）で、常時微動を用いたHVSR（水平/上下スペクトル比、Nakamura法）を週次で計算・記録し、地盤特性の経時変化をWebダッシュボードで可視化する。

2026-06-26 山梨県東部・富士五湖M5.6（震度5強）のデータを使い、ENZ/ENN/ENE（MEMS 3成分、水平2成分の幾何平均とZ成分の比）でHVSRを試算した。試算区間は以下の2つ、いずれも「1窓・60秒のみ」:

| 区間 | ピーク周波数 |
|------|------|
| 常時微動区間（P波到達前60秒） | 0.83 Hz |
| 主要動区間（P波到達後60秒） | 3.0 Hz |

区間間でピーク周波数が大きく食い違った。原因は次の2点と考えられる。

1. **統計的不安定性**: 1窓・60秒だけでは、SESAME (2004) が要求する「40〜60窓のスタッキング」に遠く及ばず、単一窓のノイズ・非定常成分をそのまま反映してしまう。
2. **Nakamura法の前提外**: Nakamura法は常時微動（定常的な環境ノイズ）を対象とする手法であり、主要動（地震動の実体波・表面波）はこの前提の外にある。地震動区間でのH/V比は、常時微動が反映する表層地盤の増幅特性とは異なる物理量を計算してしまっている可能性が高い。

この経緯を踏まえ、**常時微動を十分な長さ・複数窓でスタッキングしたHVSRのみを「地盤特性の指標」として扱う**方針を確定する。地震動区間のH/V比は本機能のスコープに含めない。

本機能はあくまで「変化があれば気づけるようにする」モニタリング目的であり、崖崩れ検知等の警報機能・降雨相関分析は**スコープ外**とする（後述「オープンクエスチョン」および「セキュリティ・互換性・その他の考慮事項」参照）。

### 使用チャンネル・帯域・窓長に関する外部提案の照合（2026-07-14追記、不採用）

別のClaude Chatセッションから「RS4Dは基本4.5Hz地震計なので低周波（<0.5Hz）は感度が落ちる、実用上は0.5〜20Hz帯域が妥当」「ウィンドウ長は100〜200秒が妥当」という提案を受け、ユーザーが本セッションの記憶（`project_rs4d_spec.md`）と照合した。

結論として**この提案は採用しない**。RS4DはEHZ（ジオフォン、4.5Hz、低周波ロールオフあり）とENZ/ENN/ENE（MEMS加速度計、V6+ではDC〜44Hzでフラット応答）の混成センサーであり、HVSR計算に使うのはENZ/ENN/ENE（MEMS）である。MEMSは低周波までフラットな応答を持つため、「4.5Hz地震計だから低周波が苦手」という前提はHVSR計算に使うチャンネルには当てはまらない。この提案はEHZ（ジオフォン）とENZ/ENN/ENE（MEMS）を区別せずに一般論を当てはめたものと判断する。

したがって、既存設計の**0.2〜20Hzの周波数範囲・40秒の窓長は変更しない**。この判断はセンサー仕様の事実確認（`project_rs4d_spec.md`）に基づくものであり、本改版での唯一の「提案不採用」事項である。

## 本番運用環境に関する前提（重要）

本プロジェクトの本番運用環境は開発機（このリポジトリを直接編集しているMac）ではなく、別マシンの**iMac**である。

- 接続: `ssh imac`
- 本番ディレクトリ: `/Users/masakai/earthquake`（**iMac側のユーザーは `masakai`**。開発機側の `sakaimasanori` とはユーザー名・パスが異なる点に注意）
- Python: venv、Python 3.12.1（Intel Mac、python.org製Python、brew未導入）
- **iMacは git 管理外**。コード反映は `git pull` ではなく、開発機からの **scp によるファイル直接転送**が既存の確立された手順（`log_server-changes.md` に2026-06-14〜06-27の複数実績あり）
- リアルタイムWebダッシュボード（`jma_intensity_web.py`、UDP受信含む）はiMac上で常時稼働しており、launchdの `com.riverruns.earthquake-web`（KeepAlive常駐）として登録されている
- 過去にサーバー上のPythonパッケージ健全性チェック（env-healthcheckエージェント）を行った実績があり、Dropbox同期で`.venv`内のファイルが欠落する問題が過去にあった

本機能（HVSR週次計測）のlaunchd登録・蓄積ファイルの実体・週次バッチの実行は、**iMac側**で行う前提とする。理由は次の「アーキテクチャ全体像」および「検討した選択肢と却下理由」で述べる。

## 設計概要

### アーキテクチャ全体像

```
[深夜の常時微動データ]
        │
        ▼
┌─────────────────────────┐     launchd 週次実行（iMac側で登録・実行）
│ src/hvsr_weekly.py       │◀─── (例: 毎週月曜 04:00)
│ - MiniSEEDダウンロード    │
│ - アンチトリガで地震除外   │
│ - 40秒窓×N でHVSR計算     │
│ - スタッキング・対数ビニング │
└───────────┬─────────────┘
            │ 追記
            ▼
  data/hvsr_history.jsonl   ← 蓄積ファイル（週1レコード、実体はiMac側 /Users/masakai/earthquake/data/ 配下）
            │
            │ 読み取りのみ（起動時 or 短TTLキャッシュ）
            ▼
┌─────────────────────────┐
│ src/jma_intensity_web.py │  ← iMac上で常時稼働中のプロセスがそのまま読む
│ - GET /api/hvsr_history   │  ← 新規読み取り専用API
│   （SharedState非依存）    │
└───────────┬─────────────┘
            │
            ▼
   dashboard.html 新規パネル
   「HVSR週次推移（試験）」
```

**リアルタイム系（SharedState・broadcast_loop・UDP受信ループ・recv_loop_fn・compute_loop）には一切変更を加えない。** これはユーザーが明示的に強くこだわっている制約であり、本設計の最上位の非機能要件として扱う。

- 計測・蓄積（`hvsr_weekly.py`）と表示（`jma_intensity_web.py`への1エンドポイント追加）を完全に分離する
- `hvsr_weekly.py` は `analyze_rs.py` 系統から独立した新規バッチスクリプトとし、`analyze_rs.py` 自体は変更しない（`download_channel` 等のロジックはコピーして流用し、元の関数は触らない。理由は「実装タスク」の項で後述）
- Webダッシュボード側は `data/bp_history.jsonl` を起動時ロード・追記していく既存の `_bp_history_load` / `_bp_history_append` パターン（読み取り専用キャッシュ + 短周期ポーリング不要な静的ファイル）を踏襲する
- **計算バッチ（`hvsr_weekly.py`）・蓄積ファイル・表示プロセス（`jma_intensity_web.py`）は全てiMac側に同居させる。** 開発機（Dropbox配下のgitリポジトリ）では週次バッチを実行しない。理由は次項。

### なぜiMac側でlaunchd実行・蓄積ファイル実体を持つのか

既存の類似バッチには次のパターンがある。

| パターン | 例 | 実行場所 | 理由 |
|---|---|---|---|
| git操作を伴うバッチ | `run_monthly_report_if_last_day.py` | 開発機 | `git commit && push` でGitHub Pagesを更新するため。iMacは「git管理外」で、このパターンは実行できない |
| API取得のみのバッチ | `fetch_p2p_daily.py` | 開発機 | git非依存。現状は開発機のlaunchdで完結している |
| 常時稼働・LAN依存サービス | `jma_intensity_web.py`（`com.riverruns.earthquake-web`） | iMac | UDP常時受信・自局LAN機器（Raspberry Shake本体）へのアクセスが必要なため常時稼働が必須 |

HVSR週次計測自体（FDSN取得→計算→JSONL追記）は、git操作もLAN依存もなく、原理上は`fetch_p2p_daily.py`と同じく開発機側のlaunchdでも完結できる処理である。

しかし、**表示先の `jma_intensity_web.py` はiMac上で常時稼働している**ため、新設する `GET /api/hvsr_history` が読みに行く `data/hvsr_history.jsonl` の実体は、iMac側の `/Users/masakai/earthquake/data/` に存在しなければならない。

開発機でバッチを実行し、生成物をiMacへ都度転送する案も検討した。scp自体は既存のデプロイ手順として確立されている（`log_server-changes.md`に複数実績あり）が、それらはいずれも「コード変更時に手動で1回行うデプロイ操作」であり、**週次・非対話的に自動でdev→iMacへpushし続ける仕組み**は前例がない。逆方向（iMac→開発機のpull、`run_monthly_report_if_last_day.py`の`sync_trigger_log_from_imac()`）にはSSH BatchMode + scpの確立した前例があるが、その逆方向の自動化は新規に組む必要がある。

このため、**計算・蓄積ファイル書き込み・Web表示を同一ホスト（iMac）に閉じる**方針を採用する。開発機側での実行は行わない。これはユーザーからの明示指示でもあり、かつ「表示系との同居」という設計上の合理性もある選択である。

### HVSR計算アルゴリズムの詳細方針

#### 使用データ・除外チャンネル

- 使用: ENZ / ENN / ENE（MEMS 3成分）。水平2成分（ENN, ENE）は幾何平均 `H = sqrt(ENN_amp × ENE_amp)` で合成し、Z成分（ENZ）との比 `H/V` を周波数ごとに計算する。
- 除外: EHZ（ジオフォン）。水平成分がなくHVSRの計算原理上使用不可（`project_lpgm_class.md` でも同じ理由でEHZは長周期帯の指標算出から除外されている）。
- **周波数帯域・窓長は0.2〜20Hz・40秒のまま変更しない**（前述「使用チャンネル・帯域・窓長に関する外部提案の照合」を参照。ENZ/ENN/ENEがMEMSであり低周波までフラット応答を持つため、「4.5Hz地震計だから低周波が苦手」という前提はここでは成立しない）。

#### 対象時間帯の選定（常時微動区間の確保）

深夜帯（人為ノイズが最小になる時間）に固定の取得ブロックを設定する。

- **取得区間**: 深夜 **02:00〜05:00 JST（3時間）** を毎週の固定取得ブロックとする。
- **理由**:
  - SESAMEガイドライン (2004) は、時間窓長は目的周波数の逆数の少なくとも10倍以上とすることを推奨する。試算で得られた常時微動ピーク0.83Hzを基準にすると10倍則の下限は約12秒だが、これは単発の試験（1イベント・1窓）から得られた一点の値であり、週次で継続監視する対象のピーク周波数が今後変動する可能性（特に軟弱地盤で0.3〜0.5Hz帯まで下がるケース）を考慮し、**窓長は0.83Hzのみに最適化しない**。
  - SESAMEはまた、安定した統計を得るには**40〜60窓**のスタッキングを推奨している。
  - 上記を踏まえ、本設計では**窓長40秒 × 目標45窓 = 総有効データ30分**を最低ラインとする。
  - 深夜の生データにも交通振動・小規模な地震・機器ノイズによる非定常区間が混在し、アンチトリガ（後述）で一定割合が棄却される。3時間（180分）の取得は、30分の有効データ確保に対して6倍の余裕を持たせた値であり、深夜静穏時間帯でも棄却率が高い場合（悪天候・遠地地震の頻発等）に対応するためのマージンとして設定した。
  - 深夜帯を選ぶことで、人的活動由来の非定常ノイズ（交通・footsteps等、SESAMEが「アンチトリガで除外すべき典型例」として挙げるもの）の混入率を下げ、アンチトリガでの棄却率そのものを抑える狙いもある。
- ダウンロード量は 100sps × 3成分（ENZ/ENN/ENE、EHZは不要）× 3時間 ≒ 324,000サンプル/成分。`analyze_rs.py` が420秒程度の区間で数十〜100KB台のMiniSEEDを扱っている実績（`data/`内の既存キャッシュファイルで確認、104,960 bytesなど）から比例計算すると、3時間分は1成分あたり概ね数百KB〜1MB程度に収まる見込みで、週次1回の実行であれば負荷は軽微と判断する。

#### 地震区間の除外（二重のフィルタ）

既存の `analyze_rs.py::compute_stalta()` の**ratio計算ロジック自体**（STA窓・LTA窓・カウント式）を流用するが、**閾値の意味論は完全に別物として新規に定義する**。両者を混同しないことが実装上の要点。

| | `analyze_rs.py` の既存用途（リアルタイム地震検知） | 本機能で新規定義する用途（常時微動の定常性判定） |
|---|---|---|
| 目的 | 地震を検知してトリガを立てる | 非定常区間（地震・突発ノイズ）を含む窓を「使わない」と判定する |
| 典型閾値 | `trig=3.5`（この値を超えたらトリガ） | SESAME準拠のアンチトリガ: `STA/LTA` がおおむね `[0.5, 2.0]` の範囲外になった時刻を含む窓は棄却 |
| 使う関数 | `compute_stalta(vec, fs, sta_s, lta_s)` の戻り値をそのまま利用 | 同じ関数を呼び出すが、判定ロジック（棄却条件）は `hvsr_weekly.py` 側に新規実装 |

`trig=3.5` をそのまま棄却閾値に転用すると、STA/LTAが0.5〜2.0を大きく超えない限り棄却されず、地震以外の非定常成分（車両通過・突風等の弱い非定常ノイズ）を除去できない。したがって `hvsr_weekly.py` は `compute_stalta()` を import して呼び出すのみとし、棄却条件（アンチトリガのしきい値・除外幅）は新規のパラメータとして別途持たせる。

処理手順:
1. 3時間の生波形（ENZ/ENN/ENE）を40秒窓・50%オーバーラップで分割する
2. 各窓について、STA=1.0秒 / LTA=20.0秒（`analyze_rs.py` のデフォルトと同じ窓長を流用し計算コストと実績のあるパラメータを踏襲）でSTA/LTA比の時系列を計算する（`compute_stalta()` をそのまま呼ぶ）
3. 窓内のSTA/LTA比が `[0.5, 2.0]` の範囲を外れる時刻を含む場合、その窓を棄却する（P2P地震情報のAPIとの突合はしない。あくまで波形自身の定常性のみで判定し、外部依存を増やさない）
4. 有効窓が45窓に満たない場合でも処理は継続する。ただし結果レコードに `status: "insufficient_data"` を付与する（後述データ形式）。有効窓が0の場合はレコード自体を `status: "failed"` として理由とともに記録し、HVSR値は `null` とする（「イベント0件」に化けさせず、失敗を可視化する。既存の `_read_trigger_events` のコメントにある「障害を『イベント0件』に化けさせない」という本プロジェクトの一貫した方針に従う）

#### テーパー処理

各40秒窓の両端に **5%コサインテーパー**（SESAMEガイドライン標準）を適用してからFFTする。スペクトル漏れ（spectral leakage）を抑制する目的。

#### FFT・H/V比計算・スタッキング

各有効窓について:
1. ENZ, ENN, ENE それぞれにテーパーを適用し、FFTで振幅スペクトルを得る
2. 水平成分の幾何平均: `H(f) = sqrt(|ENN(f)| × |ENE(f)|)`
3. `HV(f) = H(f) / |ENZ(f)|`
4. 全有効窓の `HV(f)` を対数平均（幾何平均）でスタッキングし、週の代表HVSR曲線を得る（算術平均ではなく対数平均を用いるのは、H/V比が対数正規分布に近い性質を持つためで、SESAMEガイドラインでも標準的に採用される）

**窓別ピーク周波数の抽出（SESAME安定性クライテリア記録用、2026-07-14追記）**: 上記4のスタッキングとは別に、各有効窓の**生（未平滑化）の `HV(f)`** から窓ごとのピーク周波数を個別に抽出し、`peak_freq_per_window` の系列として保持する。スタッキング後の代表曲線にKonno-Ohmachi平滑化（後述）を適用するのとは別処理であり、窓別ピーク抽出には平滑化を適用しない。理由: SESAMEの安定性クライテリア（`σf`）は「個々の窓から得られるf0推定値がどれだけばらつくか」を評価する指標であり、各窓ごとに（毎回平滑化を掛けるコストを払わず）生カーブから素朴にピークを取る運用が一般的である。この結果、生カーブ由来のピークは平滑化後より多少ノイジーに出ることを許容する（本機能は警報・自動判定に使わない参考記録のみのため、実害はない）。

#### 対数ビニング・平滑化

前回試算は「平滑化ビンが粗い」ことも課題として指摘されている。本設計では単純な対数等間隔ビニングではなく、**Konno-Ohmachi平滑化**（`obspy.signal.konnoohmachismoothing.konno_ohmachi_smoothing`、venv内でimport可能なことを確認済み、obspy 1.5.0）を採用する。

- 平滑化係数 `b = 40`（SESAMEガイドラインで安定した統計が報告されている標準値。b<30ではピークが歪む、b=40〜50が実務上の標準）
- 出力周波数軸: 0.2Hz〜20Hz の範囲で対数等間隔81点程度（既存の `compute_spectrogram` が0.5〜fs/2の範囲を扱っているのと対比しやすい範囲設定とする。下限0.2Hzは40秒窓のFFT分解能 `1/40 = 0.025Hz` に対して十分な余裕を持つ）
- ピーク周波数はKonno-Ohmachi平滑化後の曲線から抽出する（生の対数ビニングでは前回試算のように粗いピークになりやすいため、平滑化後の曲線で最大値を取る）。この「代表ピーク周波数」（`peak_frequency_hz`）と、上記「窓別ピーク周波数の抽出」で述べた`peak_freq_per_window`（窓ごとの生カーブ由来）は別の値であり、混同しないこと。

#### 品質指標（記録するが警報には使わない）

SESAMEガイドラインにはHVSRピークの信頼性を判定するクライテリア（f0の変動幅、A0のピーク明瞭度等）が存在するが、**本機能では警報・自動判定には使わない**（ユーザー合意「崖崩れ検知等の警報機能は作らない」を厳守）。ただし、週次推移をユーザー自身が目で評価する助けとして、以下を記録に残す:
- 有効窓数（`n_windows_used`）
- 全窓に対する棄却率（`reject_ratio`）
- ピーク周波数におけるH/V振幅（`peak_amplitude`）

**SESAME (2004) 信頼性クライテリアの一部（2026-07-14追記）**

SESAME (2004) ガイドライン `Guidelines for the Implementation of the H/V Spectral Ratio Technique on Ambient Vibrations`（SESAME European research project, WP12 Deliverable D23.12, December 2004）は、H/V結果の信頼性判定として**2種類・計9個の下位クライテリア**を定義している。

1. **「reliable curve（信頼できる曲線）」の3条件**（p.30、3.2節、i/ii/iiiのAND、すべて満たすことが推奨）
   - i) `f0 > 10 / lw`（`lw`=窓長）
   - ii) `nc = lw・nw・f0 > 200`（`nw`=有効窓数、`nc`=有意サイクル数）
   - iii) `σA(f) < 2`（f0>0.5Hzの場合）または `< 3`（f0<0.5Hzの場合）、周波数範囲 `[0.5f0, 2f0]` 全体で

2. **「clear peak（明瞭なピーク）」の6条件**（p.10・p.31、3.3.1節、**6項目中5項目**を満たせば信頼できるf0推定と判定）
   - i/ii) 振幅コントラスト条件（f0/4〜f0、f0〜4f0の各範囲に `A0/AHV(f) > 2` となる点が存在する）
   - iii) `A0 > 2`
   - iv) ±1標準偏差でのピーク周波数の安定性（±5%以内）
   - v) `σf < ε(f0)`（周波数帯域依存の閾値、Table 3）
   - vi) `σA(f0) < θ(f0)`（同上、Table 3）

Table 3（p.31、原文の周波数帯域区分・閾値をそのまま引用）:

| 周波数帯域 [Hz] | < 0.2 | 0.2–0.5 | 0.5–1.0 | 1.0–2.0 | > 2.0 |
|---|---|---|---|---|---|
| ε(f0) [Hz] | 0.25 f0 | 0.20 f0 | 0.15 f0 | 0.10 f0 | 0.05 f0 |
| θ(f0) for σA(f0) | 3.0 | 2.5 | 2.0 | 1.78 | 1.58 |

本機能はユーザー承認済みの方針として、この9個の下位クライテリアのうち**3個のみ**を選択的に記録する（**SESAME原典の「reliable curve」3条件AND判定、「clear peak」5-of-6判定のいずれの正式な合否結果でもない**点に注意。将来の実装者・利用者が`sesame_criteria`フィールドを見て「SESAME判定の完全な合否」と誤解しないよう、`docs/MANUAL.md`にもこの限定範囲を明記する）:

- **`window_length_ok`**（「reliable curve」条件i）由来）: `f0 > 10/lw` を満たすか。窓長`lw=40`秒固定のため、閾値は毎回 `10/40 = 0.25Hz` で不変。`peak_frequency_hz > 0.25` の真偽値をそのまま記録すればよい（クライテリアの計算式自体は固定なので、実装は単純な比較で足りる）。
- **`amplitude_ok`**（「clear peak」条件iii）由来）: `A0 > 2` を満たすか。既存の`peak_amplitude`フィールドがA0に相当するため、新規に計算し直す必要はなく `peak_amplitude >= 2.0` の真偽値を追加するのみ。**SESAME原典は厳密な超過条件（`A0 > 2`）だが、本フィールドはユーザー指定により `>= 2.0`（等号を含む）とする。**この差異は軽微だが、将来の値の再現性のためにここに明記しておく。
- **`stability_ok`**（「clear peak」条件v）由来）: `σf < ε(f0)` を満たすか。`σf`は上記「窓別ピーク周波数の抽出」で述べた`peak_freq_per_window`系列の標準偏差（`peak_freq_std_hz`として記録）。`ε(f0)`はTable 3の該当帯域の値を`peak_frequency_hz`から都度算出し比較する（帯域は`peak_frequency_hz`自体の値で判定し、`σf`の値ではない点に注意）。

**選択しなかった残り6条件（reliable curve条件ii・iii、clear peak条件i・ii・iv・vi）は、本機能の範囲外とする。** 実装が複雑になる割に「参考記録」という位置づけを超えるメリットが乏しいと判断したため（特にiv・vi・iiは`AH/V(f)`の窓別カーブ全体の保持・比較を要し、i/iiの振幅コントラスト条件はピーク以外の周波数点の振幅比較まで必要になる）。将来これらを追加する場合は、`stability_ok`と同様に`peak_freq_per_window`のような窓別中間データの保持が前提になる。

## 検討した選択肢と却下理由

| 選択肢 | 却下理由 |
|---|---|
| 地震データ（試算に使った主要動区間）も含めてHVSRを計算し続ける | Nakamura法の前提外であり、常時微動由来の地盤増幅特性とは異なる物理量を混在させることになる。ユーザーとの合意でも「常時微動を十分な長さ・複数窓でスタッキングする必要がある」と明確に結論づけている |
| `analyze_rs.py` を直接拡張してHVSR計算を追加する | `analyze_rs.py` は地震イベント単発の後処理解析（P2P地震選択・震源地図・スペクトログラム等）を目的とした既存スクリプトであり、責務が異なる。週次バッチという別のライフサイクル・別のトリガ条件（launchdでの定時実行 vs 手動/WebUI起動）を持つため、独立スクリプト`hvsr_weekly.py`として新規作成する方が責務分離として妥当（`monthly_report.py` と `fetch_p2p_daily.py` が独立している既存パターンとも整合する） |
| リアルタイム計算ループ（`compute_loop`）にHVSR計算を組み込む | ユーザーが明示的に禁止した変更禁止事項に抵触する。またHVSRは長時間データのバッチ処理が前提であり、リアルタイムループの設計思想（秒単位の逐次計算）と根本的に噛み合わない |
| 単純な対数等間隔ビニング（前回試算と同方式）を維持する | 前回試算で「平滑化ビンが粗い」ことが課題として指摘されており、Konno-Ohmachi平滑化という業界標準の代替手法がobspyに既存関数として存在する（追加依存不要）。改善の余地が明確にあるため採用しない |
| HVSRピークに基づく閾値超過アラート（崖崩れ検知等）を実装する | ユーザーが明示的にスコープ外と指定。根拠のある閾値が存在せず、誤った安心感・誤検知のリスクがある |
| 降雨量APIとの自動連携で悪天候週を自動フラグする | ユーザーが明示的にスコープ外と指定。降雨がEHZ背景レベルを押し上げる既知の現象（`rain_detection_ehz.md`）はあるが、EHZはHVSR計算に使わないため直接の影響経路が異なり、自動連携よりも人手のメモ欄で十分と判断 |
| WebUI側でHVSRを都度計算してAPIで返す（オンデマンド計算） | 40秒×45窓のFFT・Konno-Ohmachi平滑化は数秒〜十数秒かかりうる処理であり、WebUIのリクエスト応答に含めるとSharedState非依存の原則は保てても応答性が悪化する。週次で1回バッチ計算し蓄積ファイルを読むだけにする方が「読み取るだけ」の実装として単純かつ安全 |
| 週次バッチを開発機側のlaunchdで実行し、生成物をiMacへ自動push転送する | `fetch_p2p_daily.py`と同じくgit非依存のため、原理上は開発機で完結できる。しかし表示先の`jma_intensity_web.py`はiMac上で常時稼働しており、蓄積ファイルの実体はiMac側に必要。scpによるdev→iMac転送自体は既存のデプロイ手順として確立されているが、それらは全て「コード変更時の手動・一回限りの操作」であり、週次・非対話的な自動push（鍵管理・スクリプト化されたscp・失敗時のリトライ処理等）を新規に組む前例がない。逆方向（iMac→開発機のpull、`sync_trigger_log_from_imac()`）には確立した前例があるが、その逆は新規開発が必要。計算・蓄積・表示をiMac側に閉じる方が既存パターンの延長で単純 |
| RS4Dは4.5Hz地震計相当なので帯域を0.5〜20Hz・窓長100〜200秒に変更する（外部提案、2026-07-14照合） | 事実誤認。HVSR計算に使うENZ/ENN/ENEはMEMS加速度計（V6+でDC〜44Hzフラット応答）であり、4.5Hzジオフォン（EHZ）の低周波ロールオフはこれらのチャンネルには当てはまらない。既存の0.2〜20Hz・窓長40秒の根拠（`project_rs4d_spec.md`のセンサー仕様、SESAMEの10倍則・40〜60窓推奨）と矛盾するため不採用 |
| SESAME信頼性クライテリア9個すべてを実装し、正式な「reliable curve」「clear peak」判定として記録する | 実装コストに対して「参考記録」という位置づけを超える価値が乏しい。特にclear peak条件i・ii（振幅コントラスト、ピーク以外の周波数点との比較が必要）・iv（±1σでのカーブ全体比較）・vi（σA(f0)の帯域別評価）は窓別カーブ全体の保持・追加の比較ロジックを要する。ユーザー承認済みの方針は「3個を選択的に記録し、正式なSESAME合否判定ではないことを明記する」であり、警報・自動判定に使わないという既存方針とも整合する |

## 影響範囲

| ファイル | 変更種別 | 変更内容 |
|---------|---------|---------|
| `src/hvsr_weekly.py` | 新規作成 | HVSR週次計算バッチ本体。`analyze_rs.py` の `download_channel` / `download_channel_seedlink` / `compute_stalta` のロジックをコピーして流用（import ではなく複製。理由は「実装タスク」項に明記）。窓別ピーク周波数抽出・SESAME簡易クライテリア判定ロジックを含む（2026-07-14追記） |
| `data/hvsr_history.jsonl` | 新規作成（実行時に自動生成） | HVSR週次計算結果の蓄積ファイル（1行1週）。**実体はiMac側 `/Users/masakai/earthquake/data/` 配下**（開発機のDropbox配下リポジトリには生成されない）。`sesame_criteria`フィールドを追加（2026-07-14追記） |
| `src/jma_intensity_web.py` | 修正 | 読み取り専用エンドポイント `GET /api/hvsr_history` を追加。`_bp_history_load`/`_bp_history_append` と同様の「起動時ロード＋短TTLキャッシュ」パターンで実装。**SharedState・broadcast_loop・lifespan内のスレッド起動処理には変更を加えない**（新規エンドポイントの追加のみ） |
| `src/templates/dashboard.html` | 修正 | 新規パネル「HVSR週次推移（試験）」を追加。既存の `bphistory-panel` と同様のChart.js折れ線グラフ＋ Konno-Ohmachi平滑化後のHVSR曲線（最新週）の2種を表示。SESAME簡易クライテリアバッジの表示を追加（2026-07-14追記） |
| `~/Library/LaunchAgents/com.riverruns.earthquake-hvsr-weekly.plist`（**iMac側**） | 新規作成 | iMac本番機のlaunchdに登録する定時実行設定。plistのテンプレート自体は開発機のリポジトリ内（例: `scripts/launchd/`）でバージョン管理し、実体はscpでiMac側 `/Users/masakai/Library/LaunchAgents/` へ転送・配置する（開発機側の `~/Library/LaunchAgents/` に置いても本番としては機能しない）。命名は既存の稼働中サービス `com.riverruns.earthquake-web` の命名規則（`com.riverruns.*`）に揃える |
| `docs/MANUAL.md` | 修正 | 新規セクション「16. HVSR週次モニタリング」を追加。既存の「10. 月次レポート」と同様の記述粒度で、目的・実行方法・データ形式・APIエンドポイント・本番運用（iMac）・limitationsを記載。SESAME簡易クライテリアの記録範囲・限定的な位置づけの説明を含む（2026-07-14追記） |

## 実装タスク

1. **`src/hvsr_weekly.py` の新規作成**
   - `analyze_rs.py` から以下をコピーして本ファイル内に複製する（`analyze_rs.py` 自体は変更しない）:
     - `download_channel()` / `download_channel_seedlink()`（MiniSEED取得。公式FDSN→自局SeedLinkフォールバックのロジックごと流用）
     - `compute_stalta()`（STA/LTA比計算のコアロジック）
     - SSL証明書コンテキストの設定（`_SSL_CTX` 周り、certifi対応。iMac本番のpython.org製Pythonは証明書バンドル未設定が起きやすいことが2026-06-16の実績で判明済みのため、この点は必須で流用する）
   - **importではなくコピーする理由**: `analyze_rs.py` はモジュールのトップレベルで `geopandas` の読み込みと `matplotlib`（`matplotlib.use('Agg')`含む）の初期化を行っている（震源地図・スペクトログラム描画のため）。週次バッチである `hvsr_weekly.py` はグラフ描画・地図描画を一切行わないため、`from analyze_rs import download_channel` のような素朴なimportをすると、不要な重い依存（geopandas・matplotlib・フォント設定等）が毎回ロードされてしまう。責務の異なる重量級モジュールへの依存を避けるため、必要な関数のみをコピーする方針とする。
   - **保守上の注意点（メンテナンスノート）**: この複製により、将来 `analyze_rs.py` 側の `download_channel_seedlink()`（例: 未来時刻クランプ処理等）にバグ修正が入っても、`hvsr_weekly.py` 側には自動反映されない。両ファイルの当該関数冒頭に「このロジックは `analyze_rs.py`/`hvsr_weekly.py` の対となる関数と重複しています。修正時は両方を確認してください」という趣旨のコメントを残すこと。`docs/MANUAL.md` にも同様の注意書きを1行残す
   - 深夜02:00〜05:00 JST（当日 or 実行日前日、実行タイミングに応じて自動算出）の区間で ENZ/ENN/ENE をダウンロード
   - 40秒窓・50%オーバーラップで分割、5%コサインテーパー適用
   - 各窓についてSTA/LTA比（STA=1.0秒/LTA=20.0秒）を計算し、`[0.5, 2.0]` を外れる時刻を含む窓を棄却
   - 有効窓についてFFT→水平2成分幾何平均→H/V比→対数平均でスタッキング
   - **各有効窓の生（未平滑化）`HV(f)`から窓別ピーク周波数を個別に抽出し、`peak_freq_per_window`系列として保持する**（2026-07-14追記。「FFT・H/V比計算・スタッキング」項参照。スタッキング後の代表曲線とは別の中間データ）
   - Konno-Ohmachi平滑化（b=40）を0.2〜20Hzの対数等間隔周波数軸に適用
   - ピーク周波数・ピーク振幅・有効窓数・棄却率を算出
   - **SESAME簡易クライテリア3項目を算出する**（2026-07-14追記。「品質指標」項参照）:
     - `window_length_ok`: `peak_frequency_hz > 10/40 (=0.25)` の真偽値
     - `amplitude_ok`: `peak_amplitude >= 2.0` の真偽値
     - `peak_freq_std_hz`: `peak_freq_per_window` の標準偏差
     - `stability_ok`: `peak_freq_std_hz < ε(peak_frequency_hz)`（Table 3の帯域別閾値、`peak_frequency_hz`の値に応じて`0.25f0`〜`0.05f0`のいずれかを適用）の真偽値
   - `data/hvsr_history.jsonl` に1行追記（後述データ形式）
   - `--dry-run`（ダウンロードのみ確認）、`--date`（過去日の手動再計算用、バックフィル対応）オプションを用意する
   - ログ出力は `logs/hvsr_weekly.log` に `fetch_p2p_daily.py` と同様の `log()` パターン（タイムスタンプ付き1行ログ、`print`とファイル追記の両方）で記録する
   - **開発（コーディング・単体テスト・キャッシュ済みMiniSEEDでのオフライン検証）は開発機側で行い、本番投入（iMacへの配置・launchd登録）のみをiMac側で行う分担とする**（既存の `jma_intensity_web.py` 等の開発フローと同じ）

2. **`data/hvsr_history.jsonl` のデータ形式定義**（詳細は次項。`sesame_criteria`フィールドを含む）

3. **`src/jma_intensity_web.py` への読み取り専用API追加**
   - `GET /api/hvsr_history` エンドポイントを追加（`_read_trigger_events` / `/api/events` と同じ「ファイルを読んでJSON化するだけ」のパターン）
   - クエリパラメータ: `limit`（既定52週=1年分、最大520週=10年分）
   - 起動時に全件をメモリにロードし、リクエスト時はファイルの mtime が変化していれば再読み込みする軽量キャッシュ（`_bp_history` のようなdequeへの都度追記ではなく、週次でしか更新されないため mtime チェック＋全量再読み込みで十分。lifespan・SharedState・broadcast_loopには一切触れない）
   - 既存の `/api/events` と同様、無認証・読み取り専用であることを `docs/MANUAL.md` にも明記する
   - `sesame_criteria`フィールドはそのままJSON化して返す（バックエンド側での追加加工は不要）

4. **`src/templates/dashboard.html` への新規パネル追加**（詳細は「WebUIパネルのUI設計」の項）
   - 起動時に `/api/hvsr_history` を1回fetchして描画（WebSocket経由のリアルタイム更新は行わない。週次更新のデータにWebSocketの1秒更新は不要でSharedStateにも触れないため）
   - Chart.js で「ピーク周波数の週次推移」の折れ線グラフ、および「最新週のHVSR曲線」の折れ線グラフの2種を表示
   - 最新週の`sesame_criteria`の3項目を示す小さなバッジ・アイコンを表示する（詳細は「WebUIパネルのUI設計」の項）

5. **iMac本番機へのデプロイ（launchd plist登録を含む）**

   開発機での実装・テスト完了後、以下の手順でiMac本番機へ反映する。この手順は developer / release-manager フェーズ、または実際の運用作業時に実施するものであり、architectフェーズの範囲外である。既存の確立済み手順（`log_server-changes.md` に記録された2026-06-14〜06-27の複数実績）に準拠する。

   1. 開発機でコミット（feature branchでの作業を推奨。`feedback_branch_strategy.md` の方針に従い、根幹に関わる新機能追加のためfeatureブランチで作業する）
   2. `ssh imac` で接続し、本番側の既存ファイル（`src/jma_intensity_web.py` 等、今回改修する既存ファイルのみ）が開発機の親コミットと完全一致することを `diff` で確認する（本番側に未反映の独自編集が無いことの確認）
   3. **本番ファイルのバックアップを取る**（`cp file file.bak.YYYYMMDD` 形式）。対象は改修する既存ファイルのみ（`src/jma_intensity_web.py`、`src/templates/dashboard.html`）。新規ファイル（`hvsr_weekly.py`、plist）はバックアップ不要
   4. scpで以下をiMacへ転送する
      - `src/hvsr_weekly.py`（新規）→ `/Users/masakai/earthquake/src/`
      - `src/jma_intensity_web.py`（改修）→ 同上
      - `src/templates/dashboard.html`（改修）→ `/Users/masakai/earthquake/src/templates/`
      - plistファイル → iMac側 `/Users/masakai/Library/LaunchAgents/com.riverruns.earthquake-hvsr-weekly.plist`
   5. iMac本番venv（`/Users/masakai/earthquake/.venv`）で `py_compile` / `import` チェックを行う（既存デプロイ手順と同様）
   6. 新規plistは `launchctl load ~/Library/LaunchAgents/com.riverruns.earthquake-hvsr-weekly.plist` で登録する。これは新規サービスの初回登録であり、既存の常駐サービス再起動に使う `launchctl kickstart -k gui/$UID/com.riverruns.earthquake-web` とは操作が異なる点に注意（`jma_intensity_web.py` 改修分の反映には従来通り `kickstart` を使う）
   7. 検証: `GET /api/version` でバージョン反映を確認、`GET /api/hvsr_history` が空配列 `[]` を返すことを確認する（初回バッチ実行前）。次回のlaunchd起動、または手動で1回 `hvsr_weekly.py` を実行した後、`data/hvsr_history.jsonl` にエントリが追記されることを確認する
   8. **サーバー上のファイルを変更したら操作ログをメモリに記録する**（`/Users/sakaimasanori/.claude/projects/-Users-sakaimasanori-Dropbox-earthQuake/memory/log_server-changes.md` に日時・対象ファイル・変更内容の要約・バックアップファイル名を記載する。これは既存の全デプロイ実績で一貫して行われている運用であり、本機能のデプロイでも踏襲する）

   デプロイ前に、iMac側venvの依存パッケージ状況を確認する（「セキュリティ・互換性・その他の考慮事項」の依存パッケージ確認の項を参照）。

6. **`docs/MANUAL.md` の更新**（新規セクション追加。SESAME簡易クライテリアの記録範囲の説明を含む）

## WebUIパネルのUI設計

`project_web_dashboard.md`（3カラムレイアウト設計メモ）・`project_theme_switch.md`（ライト/ダークモード切り替え計画メモ）を踏まえ、以下の方針とする。実際の `src/templates/dashboard.html` を確認したところ、右カラム（`#col-right`、grid比率 `1fr 1.4fr 1fr` の右端、`flex-direction: column`）に `p2p-panel` → `bandpower-panel` → `bphistory-panel` の順で3パネルが縦に並んでいる。

### 配置

- **新規 `hvsr-panel` は `#col-right` 内、`bphistory-panel` の直後に追加する。** 理由: 既存の右カラムは「観測点そのものの生波形由来の指標」（P2P地震情報を除けば `bandpower-panel` と `bphistory-panel` はいずれもENZ由来の周波数解析結果）を並べる場所として使われており、HVSR（同じくENZ/ENN/ENEの周波数解析結果）もこの文脈に自然に収まる。中央カラム（地図・I値/STA/LTA推移）や左カラム（現地計測震度）はリアルタイム性の高い情報専用であり、週次更新のHVSRを混在させると情報の更新頻度感が食い違うため避ける。
- `bphistory-panel` は `flex: 1 1 0` で残り空間を埋める設定になっているため、新規パネルを追加する場合は `bphistory-panel` を `flex: 0 0 auto`（既存の `bandpower-panel` と同じ固定高さ運用）に変更し、新規 `hvsr-panel` を最下段の `flex: 1 1 0` に変更する案と、単純に両パネルとも固定高さ・スクロール許容にする案の両方が考えられる。**最終的なCSS配分は実装時にレイアウト崩れがないか実機（ブラウザ）で確認しながら調整すること**（本設計書では配置の順序と根拠のみ確定し、flex比の最終値は実装フェーズの裁量とする）。
- レビュー指摘（中重大度）: `#col-right`は`overflow: hidden`であり、パネル合計高さがカラム全体を超えた場合スクロールバーは出ず末尾パネルが無言で見切れる。実装フェーズでは、画面高さが低い環境（1080p未満、ウィンドウ分割時等）でのcanvas下端の見切れがないことをスクリーンショット付きで確認し、必要に応じて`#col-right`または`hvsr-panel`個別に`overflow-y: auto`を設けるフォールバックを検討すること。

### パネル構成（案）

```html
<div class="panel" id="hvsr-panel">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
        <div class="panel-title" style="margin-bottom:0;">HVSR週次推移（試験）</div>
        <span style="font-size:10px;color:#8b949e;">常時微動・Nakamura法</span>
    </div>
    <!-- (a) ピーク周波数の週次推移（折れ線） -->
    <div style="position:relative; height:45%; min-height:70px;">
        <canvas id="hvsrTrendChart"></canvas>
    </div>
    <!-- (b) 最新週のHVSR曲線（周波数-H/V比） -->
    <div style="position:relative; height:45%; min-height:70px; margin-top:8px;">
        <canvas id="hvsrCurveChart"></canvas>
    </div>
    <div id="hvsr-status-note" style="font-size:10px;color:#8b949e;margin-top:4px;"></div>
    <!-- (c) SESAME簡易クライテリアバッジ（2026-07-14追記） -->
    <div id="hvsr-sesame-badges" style="display:flex;gap:6px;margin-top:4px;font-size:9px;" title="SESAME(2004)信頼性クライテリアの一部を参考表示（自動警報ではありません）">
        <span class="sesame-badge" data-key="window_length_ok">窓長</span>
        <span class="sesame-badge" data-key="amplitude_ok">振幅</span>
        <span class="sesame-badge" data-key="stability_ok">安定性</span>
    </div>
</div>
```

- (a) は横軸=週（`week_start`）、縦軸=`peak_frequency_hz`。既存の `bpHistoryChart` と同じ Chart.js line chart 設定を踏襲する。
- (b) は最新1週分の `freq_hz` / `hv_ratio` 配列をそのままプロットする、対数X軸（周波数）のline chart。
- `status` が `"insufficient_data"` または `"failed"` の週は、(a)のグラフ上でその週のポイントを通常と異なるマーカー（例: 塗りつぶしなしの丸）で表示し、`hvsr-status-note` に「最新週: データ不足のため参考値（有効窓 12/539）」等のテキストを表示する。これにより崖崩れ検知的な自動判定を行わずとも、ユーザー自身がデータ品質を目視判断できるようにする。
- **(c) SESAME簡易クライテリアバッジ（2026-07-14追記）**: 最新週の`sesame_criteria`（`window_length_ok`/`amplitude_ok`/`stability_ok`）を、3つの小さなラベル（例: 「窓長」「振幅」「安定性」）＋色分け（満たす=緑系、満たさない=グレーまたは黄系。震度スケール色・EEWアラート色とは別系統の配色を使い赤系は使わない）で表示する。
  - **これは自動警報・自動判定ではなく、SESAME (2004) の一部クライテリアを参考情報として可視化するものであることを、バッジのtitle属性（ツールチップ）および`docs/MANUAL.md`に明記する。**
  - 3項目すべて満たす場合でも「地盤特性が正しく求まっている」ことを保証するものではない（前述「限界・リスク」参照）。逆に満たさない場合でも、モニタリング目的では「今週の推定値の信頼度が相対的に低い」という参考情報に留め、値自体を非表示にしたり自動的に除外したりはしない（週次推移の連続性を優先する）。
  - `status`が`"failed"`の週（有効窓0件）はバッジ自体を表示しない（クライテリア計算の前提となる値が存在しないため）。`"insufficient_data"`の週はバッジを表示するが、既存の`hvsr-status-note`の注記と合わせて参考値であることが分かるようにする。

### 配色（テーマ切り替え計画との整合）

`project_theme_switch.md`によれば、ダッシュボードは現在ダークモード固定でCSS色がハードコードされており、将来的に `:root` へのCSS変数化（`--bg-primary`, `--text-secondary`, `--accent-blue` 等）とライトモード対応が計画中（未着手）である。

- 本設計時点ではCSS変数化がまだ行われていないため、新規 `hvsr-panel` も**既存パネルと同じ方式（直接のハードコードされた色指定）で実装する**。ここで独自にCSS変数を先取り導入すると、テーマ切り替え実装時に「一部だけ変数化されたパネル」が混在し、変更差分の見通しが悪くなる。
- ただし配色の値自体は、テーマ切り替えメモに列挙された既存パレットをそのまま踏襲する: パネル背景相当に `#161b22`、本文テキストに `#e6edf3`、補助テキストに `#8b949e`（既存 `bphistory-panel` 等で使われている値）、グラフ線色に既存の帯域パワー配色と重複しない新規アクセント色（例: `#a371f7` 系の紫、既存の4帯域色 `#58a6ff`/`#f97316`/`#3fb950`/`#da3633` と衝突しない色を選ぶ）を用いる。
- SESAME簡易クライテリアバッジの配色も同様に既存パレットの範囲内とする（満たす=既存の緑`#3fb950`系、満たさない=補助テキスト同系のグレー`#8b949e`系。警報色である`#da3633`（赤）はバッジには使わない。これは「自動警報ではない」という位置づけをUI上でも一貫させるため）。
- 震度スケール色・EEWアラート色（テーマ非依存で固定と明記されている）は本パネルでは使用しない（HVSRは震度・警報とは無関係な指標であるため、混同を避ける意味でも別系統の色を使う）。
- 将来CSS変数化が実施される際は、`hvsr-panel` 内のハードコード色も他パネルと同時に一括置換される対象に含める旨を `docs/MANUAL.md` または `project_theme_switch.md` のメモに追記しておくことが望ましい（実装フェーズでの申し送り事項）。

## テスト方針

### ユニットテスト
- HVSR計算のコア関数（テーパー適用、FFT、H/V比計算、Konno-Ohmachi平滑化呼び出し、スタッキング）を独立関数として実装し、既知の合成波形（例: 特定周波数の正弦波+ノイズ）に対して期待通りのピーク周波数が出ることをテストする
- アンチトリガの棄却ロジック（STA/LTA比が[0.5, 2.0]を外れる窓の除外）を、人工的に地震様の非定常波形を混ぜた合成データで検証する
- 有効窓数不足時に `status: "insufficient_data"` が正しく付与されることのテスト
- 有効窓0件時に `status: "failed"` かつ HVSR値が `null` になることのテスト
- **SESAME簡易クライテリア3項目の算出テスト（2026-07-14追記）**:
  - `window_length_ok`: `peak_frequency_hz`が0.25Hzを跨ぐ境界値（0.24/0.25/0.26Hz相当の合成データ）で正しく真偽が切り替わること
  - `amplitude_ok`: `peak_amplitude`が1.9/2.0/2.1の境界でのテスト（`>=2.0`であり`>2`ではないことを明示的に確認）
  - `stability_ok`: 複数の合成窓（既知のピーク周波数±既知のばらつき）を与え、`peak_freq_std_hz`が正しく計算され、Table 3の該当帯域の閾値と比較した真偽値が一致すること。特に帯域境界（0.2/0.5/1.0/2.0Hz）をまたぐケースで正しい`ε(f0)`係数が選択されることを確認する
  - `status: "failed"`（有効窓0件）の場合、`sesame_criteria`全体が`null`または該当バッジ非表示相当の値になることのテスト

### 統合テスト
- `data/`内の既存MiniSEEDキャッシュ（`AM.R38DC.00.ENZ.20260626_222700_420s.ms` 等）を使い、`hvsr_weekly.py`のダウンロード以降のロジックのみをキャッシュ済みデータで動かして end-to-end 動作確認する（`analyze_rs.py --no-download` と同じ発想）。**この検証は開発機側で完結できる**（開発機のデータキャッシュを使い、iMacへの接続は不要）
- `GET /api/hvsr_history` が `data/hvsr_history.jsonl` の内容を正しくJSON化して返すことを確認する（`test_api_events.py` と同様のテスト構成を新設: `test_api_hvsr_history.py` 案）。これも開発機側でダミーのJSONLファイルを用意して検証できる。`sesame_criteria`フィールドを含むレコードが正しく往復することも確認する

### 手動確認項目
- 実際に深夜帯（02:00〜05:00 JST）でダウンロードを1回実行し、取得データ量・処理時間・出力されるHVSR曲線の形状を目視確認する（初回はiMac本番機、または開発機で手動実行してオフライン確認したうえでiMacへ投入する）
- WebUIパネルが正しく表示され、既存パネルのレイアウト・配色トーンと違和感なく統合されていることを確認する
- **SESAME簡易クライテリアバッジが、`sesame_criteria`の真偽値と対応した表示（色・ラベル）になっていること、ツールチップに「自動警報ではない」旨が表示されることを目視確認する（2026-07-14追記）**
- 悪天候が明確な日（降雨の強い日）のデータで試験実行し、EHZ背景上昇の影響がENZ/ENN/ENE側にどの程度波及するかを目視確認し、天候メモ欄の運用が現実的かを確認する
- iMac本番でのlaunchd定時実行が実際に週次で起動することを、初回登録後の次回起動タイミングで確認する（`log show` 等でlaunchd起動ログを確認する、または `data/hvsr_history.jsonl` に新規エントリが増えたことで間接確認する）

## ドキュメント更新が必要なファイル

- `docs/MANUAL.md`: 新規セクション「16. HVSR週次モニタリング」を追加。目的・アルゴリズム概要（窓長40秒・45窓目標・Konno-Ohmachi b=40等の主要パラメータ）・実行方法・`GET /api/hvsr_history` のAPI仕様・**本番運用がiMac側であること・launchd登録手順の概要**・limitations（単独観測点であることの限界、崖崩れ検知等への拡張はしない旨）を記載。**SESAME簡易クライテリア（`sesame_criteria`）が、SESAME原典9下位クライテリアのうち3個のみを選択的に記録したものであり、正式な合否判定ではないことを明記する（2026-07-14追記）**
- `docs/CHANGELOG.md`: 新機能追加のエントリを次回バージョンアップ時に追加（release-manager作業時）

## セキュリティ・互換性・その他の考慮事項

### セキュリティ
- `GET /api/hvsr_history` は `/api/events` と同様、無認証・読み取り専用エンドポイントとする。既存の `--web-bind` の既定値（`0.0.0.0`）に関する注意（`docs/MANUAL.md` の既存の警告文）がそのまま本エンドポイントにも適用されることを明記する
- HVSRデータ自体に個人情報・機微情報は含まれないため、`/api/events`以上の追加のアクセス制御は不要と判断する

### 後方互換性
- `jma_intensity_web.py` への変更はエンドポイント追加のみであり、既存のWebSocketペイロード（`broadcast_loop`の`payload`辞書）・既存APIの挙動には一切影響しない
- `analyze_rs.py` は無変更（コピー元としてのみ参照）。既存のCLI引数・出力形式に影響なし

### 運用への影響
- 週次バッチは深夜帯に実行されるため、リアルタイム系（UDP受信・compute_loop）とは別プロセスとして動作し、CPU・メモリ競合のリスクは低い（`monthly_report.py`等の既存バッチも同様の考え方で運用されている）
- ダウンロード量は3時間分×3成分で、既存の月次・日次バッチと比べても軽微
- iMacはWebダッシュボード常駐（`com.riverruns.earthquake-web`）に加えて新規の週次launchdジョブ（`com.riverruns.earthquake-hvsr-weekly`）が加わるが、深夜帯の3時間データ取得＋バッチ計算という短時間ジョブであり、常駐サービスへの影響は軽微と見込む

### 本番ファイル変更時の運用ルール（iMacへのデプロイ実施時に適用）
- グローバル開発ルール（`~/.claude/CLAUDE.md`）に従い、iMac本番の既存ファイルを変更する前には `.bak.YYYYMMDD` 形式でバックアップを取る
- 変更後は `log_server-changes.md`（`/Users/sakaimasanori/.claude/projects/-Users-sakaimasanori-Dropbox-earthQuake/memory/log_server-changes.md`）に日時・対象ファイル・変更内容の要約・バックアップファイル名を記録する
- この手順は**今回のarchitectフェーズでは実施しない**。実際のデプロイ作業時（developer/release-managerフェーズ、または実運用作業）に適用する

### 依存パッケージの確認事項（iMac側venv）
- `hvsr_weekly.py` は `obspy` / `numpy` / `scipy` に依存する（`microseism.py` と同じ）。開発機の `requirements.lock.txt` では `numpy==2.4.6` / `obspy==1.5.0` / `scipy==1.17.1` だが、**iMac本番venvに同等のバージョンが導入済みか、本設計時点では未確認**
- 2026-06-14の実績（`log_server-changes.md`）では、iMacはIntel Mac・python.org製Python 3.12.1・brew未導入という制約下で、`geopandas` 導入時に依存 `pyproj` のwheelが存在せずソースビルドに失敗した前例がある（`proj executable not found`）。最終的に `pip install --only-binary=:all:` でwheelのみ導入する対処を行った
- `obspy` も同様にwheel提供状況が環境に依存しうるため、実装フェーズの開始前に `ssh imac` して `.venv/bin/pip show obspy numpy scipy` 等でバージョンを確認し、未導入・大幅なバージョン不一致がある場合は同様に `--only-binary=:all:` でのwheel導入を検討する。導入作業を伴う場合は、上記「本番ファイル変更時の運用ルール」と同様にpip freezeのバックアップ（`pip_freeze.bak.YYYYMMDD.txt`）を取ってから行う
- `microseism.py` は既にH/V計算ロジックを持つが、iMac本番側で実際に実行された実績があるかどうかは本設計時点では確認できていない（オープンクエスチョン参照）

### 限界・リスク（明記が必須の事項）

1. **単独観測点であることの限界**: R38DC 1局のみのデータであり、複数観測点での相互比較・妥当性検証ができない。得られるHVSR曲線が「その観測点の地盤特性」を正しく反映しているかを他観測点データで裏付けることはできない。
2. **MEMSノイズフロアの限界**: ENZ/ENN/ENEはMEMS加速度計であり、`project_lpgm_class.md`で確認された通りノイズフロアが未知数である。特に低周波数帯（0.1Hz以下）や振幅の小さい定常ノイズでは、MEMS自体のノイズがH/V比に混入する可能性があり、得られたピークが真の地盤共振周波数かMEMS自身の特性由来かを区別する手段が本設計にはない。
3. **地盤増幅率は未知数のまま**: HVSRのピーク振幅（A0）は地盤の増幅度合いを反映するとされるが、本設計では振幅の絶対値を定量評価する基準を持たない。週次推移としての相対比較（「先週と比べてピークがどう動いたか」）に限定した参考指標として扱う。
4. **崖崩れ検知等への拡張は行わない**: 本機能はモニタリング目的に限定する。ユーザーとの合意により、根拠のある閾値が存在しないため警報機能は実装しない。将来的にこの制約を変更する場合は、別途、閾値設定の科学的根拠の検証を伴う設計判断が必要になる。
5. **降雨等の悪天候時のデータ信頼性低下**: EHZの降雨検出実績（`rain_detection_ehz.md`）から、雨天時は背景ノイズレベルが上昇することが分かっている。EHZ自体はHVSR計算に使わないが、同一サイトでの雨天は地盤の含水率変化・地表水の流動ノイズ等を通じてENZ/ENN/ENEにも何らかの影響を与えうる。本設計では自動的な降雨判定・自動連携は行わず、`hvsr_history.jsonl`のレコードに任意の`weather_note`フィールド（手動記入可、既定は空文字）を持たせるにとどめる。
6. **iMac側の依存パッケージ未確認**: 「依存パッケージの確認事項」に記載の通り、`obspy`等がiMac本番venvに導入済みかは本設計時点で未確認。実装フェーズ開始前の確認作業が前提となる。
7. **SESAME簡易クライテリアは選択的な部分実装である（2026-07-14追記）**: `sesame_criteria`はSESAME (2004) 原典の9個の下位クライテリアのうち3個のみを記録したものであり、「reliable curve」3条件のAND判定、「clear peak」5-of-6判定のいずれの正式な合否結果でもない。3項目すべてを満たす場合でも、SESAME原典の意味での「非常に信頼できるf0推定」を意味しない。この限定を`docs/MANUAL.md`およびWebUIのツールチップで明記し、誤読を防ぐ。

## データ形式案: `data/hvsr_history.jsonl`

1行1週のJSON Lines形式。既存の `data/bp_history.jsonl`（1行1分）・`data/p2p_cache/*.jsonl`のJSONL慣習に合わせる。**ファイルの実体はiMac側 `/Users/masakai/earthquake/data/hvsr_history.jsonl`。**

```json
{
  "week_start": "2026-07-13",
  "computed_at": "2026-07-14T04:03:12+09:00",
  "station": "R38DC",
  "capture_window": {"start": "2026-07-14T02:00:00+09:00", "end": "2026-07-14T05:00:00+09:00"},
  "status": "ok",
  "n_windows_total": 539,
  "n_windows_used": 103,
  "reject_ratio": 0.809,
  "window_length_s": 40.0,
  "window_overlap": 0.5,
  "peak_frequency_hz": 0.91,
  "peak_amplitude": 3.42,
  "freq_hz": [0.2, 0.21, "...(対数等間隔81点程度)", 20.0],
  "hv_ratio": [1.05, 1.08, "...", 0.34],
  "smoothing": {"method": "konno_ohmachi", "b": 40},
  "sesame_criteria": {
    "window_length_ok": true,
    "amplitude_ok": true,
    "stability_ok": false,
    "peak_freq_std_hz": 0.12
  },
  "weather_note": ""
}
```

注: `n_windows_total` は3時間（10800秒）の取得区間を窓長40秒・50%オーバーラップ（ステップ20秒）で分割した場合の総数 `floor((10800-40)/20)+1 = 539`。0%オーバーラップと誤読しないよう `window_overlap` フィールドを明示的に持たせる。

フィールド定義:

| フィールド | 型 | 説明 |
|---|---|---|
| `week_start` | string (YYYY-MM-DD) | 対象週の開始日（月曜日、JST基準） |
| `computed_at` | string (ISO8601, JST) | バッチ実行日時 |
| `station` | string | 観測点コード（将来の複数局対応に備え固定値でも保持） |
| `capture_window` | object | 実際にデータを取得した時刻範囲 |
| `status` | string | `"ok"` / `"insufficient_data"`（有効窓数が45未満だが1件以上ある）/ `"failed"`（有効窓0件、または取得失敗） |
| `n_windows_total` | int | 取得区間から切り出した全窓数（`window_overlap` を反映した数） |
| `n_windows_used` | int | アンチトリガを通過した有効窓数 |
| `reject_ratio` | float | 棄却率（`1 - n_windows_used/n_windows_total`） |
| `window_length_s` | float | 窓長（秒）。将来パラメータ変更時の追跡用に毎回記録 |
| `window_overlap` | float | 窓オーバーラップ率（0.0〜1.0）。`n_windows_total` の算出根拠として毎回記録 |
| `peak_frequency_hz` | float or null | Konno-Ohmachi平滑化後のピーク周波数。`status="failed"`時は`null` |
| `peak_amplitude` | float or null | ピーク周波数におけるH/V振幅。`status="failed"`時は`null` |
| `freq_hz` | array[float] or null | 平滑化後の周波数軸（WebUIでのHVSR曲線描画用） |
| `hv_ratio` | array[float] or null | 上記に対応するH/V比 |
| `smoothing` | object | 平滑化手法・パラメータ（将来の手法変更時のトレーサビリティ） |
| `sesame_criteria` | object or null | SESAME (2004) 信頼性クライテリアの一部（3項目）。`status="failed"`時は`null`。**SESAME原典の正式な合否判定ではなく選択的な部分記録**（詳細は「品質指標」項・「限界・リスク」項7を参照） |
| `sesame_criteria.window_length_ok` | bool | `peak_frequency_hz > 10/window_length_s`（窓長40秒固定なら閾値0.25Hz）を満たすか |
| `sesame_criteria.amplitude_ok` | bool | `peak_amplitude >= 2.0` を満たすか（SESAME原典は`> 2`の厳密不等号だが、本フィールドは`>=`を採用） |
| `sesame_criteria.stability_ok` | bool | `peak_freq_std_hz < ε(peak_frequency_hz)`（SESAME Table 3の周波数帯域別閾値。帯域<0.2Hz→`0.25f0`、0.2-0.5Hz→`0.20f0`、0.5-1.0Hz→`0.15f0`、1.0-2.0Hz→`0.10f0`、>2.0Hz→`0.05f0`）を満たすか |
| `sesame_criteria.peak_freq_std_hz` | float | 各有効窓の生（未平滑化）`HV(f)`から個別に抽出したピーク周波数系列の標準偏差 |
| `weather_note` | string | 手動記入用の天候メモ欄（既定は空文字、自動連携なし） |

## オープンクエスチョン

1. **深夜取得ブロックの具体的な時刻（02:00〜05:00 JST）は暫定案**。実際のR38DC周辺の深夜交通量・生活ノイズパターンを踏まえ、運用開始後に最適な時間帯へ調整する余地を残す。
2. **launchd実行曜日・時刻（毎週月曜04:00案）も暫定**。深夜取得ブロック（02:00〜05:00終了）の後、十分なマージン（1時間程度）を置いて計算バッチを起動する想定だが、確定は実装フェーズで運用担当と調整する。
3. **iMac側の依存パッケージ（obspy/numpy/scipy）導入状況の確認**。本設計時点では未確認。実装フェーズ開始前に `ssh imac` して `.venv/bin/pip show obspy numpy scipy` 等でバージョンを確認し、必要であれば導入作業（バックアップ・ログ記録を伴う）を行う。`microseism.py` が既にiMac本番で実行された実績があるかどうかも合わせて確認する。
4. **有効窓45という目標窓数は初期値**。実運用で頻繁に `insufficient_data` になる場合、取得ブロックを3時間から延長する、または目標窓数を下げる（ただしSESAMEの推奨下限40窓を下回らない範囲で）といった調整が必要になる可能性がある。数週間の運用実績を見てから確定させる。
5. **バックフィル（過去分の一括計算）の要否**。既存の `data/` 内MiniSEEDキャッシュは地震イベント用（短時間・`--from-p2p`起点）であり、深夜の常時微動データそのもののキャッシュは存在しない。本機能開始前の期間に遡ってHVSR推移を再構築することはできない（そもそも観測データ自体が残っていない可能性が高い）。この点はユーザーに確認済みではないため、必要であれば別途相談する。
6. **既存launchd plist（月次レポート・P2P日次）の実際の設定内容は依然未確認**。リポジトリ内にplist実体が存在せず、`~/Library/LaunchAgents`（開発機・iMacいずれも）も本セッションのツール権限では読み取れない。実装フェーズで `ssh imac` して `launchctl list | grep riverruns` 等により、命名規則・ログパス・既存の実行間隔指定方法の実物を確認し、新規plistをそれに揃えることを推奨する。
7. **SESAME簡易クライテリアの残り6項目を将来追加するか（2026-07-14追記）**: 本設計では9個中3個のみを選択的に実装する。将来「reliable curve」の`nc>200`条件や「clear peak」の振幅コントラスト条件（i/ii）・±1σ全体安定性（iv）・`σA(f0)<θ(f0)`（vi）まで拡張する場合、`peak_freq_per_window`と同様に窓別の`AH/V(f)`カーブ全体（またはその要約統計）を中間データとして保持する設計変更が必要になる。現時点ではユーザー承認済みの3項目に限定し、拡張の要否は運用実績を見てから判断する。
