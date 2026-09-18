# 週末3CARDS

土日メインレース対象、リスク度別3枚カード（+確変4枚目=鳳）で買い目と一言分析を提示する競馬予想PWA。

設計フェーズ（タスク1〜5）は完了。実装フェーズはA/B/B2/Cフェッチャー・logic・成績集計・UIまで到達し、
**フェッチャーは A/B/B2/C/D/E/F すべて実装済み**。残るは基準タイム表の構築と本番疎通確認。
**仕様書・引き継ぎ書はすべて `docs/` に同梱**（下記「ドキュメント」参照）。

## ディレクトリ構成

```
.
├── config/                    # チューニング対象パラメータ（各仕様書の設定ファイルをそのまま配置）
│   ├── speed_index.json       # スピード指数仕様§5
│   ├── myomi.json             # 妙味メーター仕様§8
│   ├── cards.json             # 買い目生成仕様§7
│   ├── scraper.json           # メインレースの絞り込みルール等（OPEN_QUESTIONS B-2）
│   ├── base_times.json        # スピード指数仕様§4（生成物・今は空プレースホルダー）
│   └── base_times.README.md
├── scraper/
│   ├── common/
│   │   ├── constants.py       # 開催場romaji対応表・正規化ルール（取得項目仕様§2.1）
│   │   ├── leading_api.py     # D・E共通のリーディングAJAX API（パーサーのみ未実装）
│   │   └── http.py            # レート制限付きHTTP取得（取得項目仕様§1.1 マナー設計）
│   ├── fetchers/
│   │   ├── a_race_list.py     # 開催日別レース一覧　　　✅実装済
│   │   ├── b_shutuba.py       # 出馬表　　　　　　　　　✅実装済
│   │   ├── b2_odds.py         # オッズAPI（AJAX直叩き）✅実装済
│   │   ├── c_horse_history.py # 馬の戦績（直近5走）　　 ✅実装済
│   │   ├── d_jockey_leading.py# 騎手リーディング　　　　✅実装済
│   │   ├── e_trainer_leading.py# 調教師リーディング　　 ✅実装済（Dと同一API）
│   │   └── f_results.py       # レース結果・払戻　　　　✅実装済
│   └── build_raw.py           # A〜Eを束ねて raw/{week_id}.json を生成　✅実装済
├── logic/                     # ✅実装済（仕様書の検算サンプルでテスト済み）
│   ├── speed_index.py         # ①スピード指数（スピード指数仕様）
│   ├── aptitude.py            # ②適性スコア（買い目生成仕様§1.4）
│   ├── human_score.py         # ③人的スコア（買い目生成仕様§1.5）
│   ├── base_score.py          # 合成スコア（①②③のz標準化合成。④は入れない）
│   ├── prob_model.py          # softmax p / 市場支持率q（妙味・買い目で共有）
│   ├── myomi.py               # 妙味メーター
│   ├── cards.py               # 印付与・キャラ別買い目生成
│   └── build_predictions.py   # raw → predictions.json オーケストレーター
├── scripts/
│   └── build_base_times.py    # base_times.json 初期構築（1回きり）⬜未実装
├── results/
│   └── build_results.py       # results.json 生成（タスク7）✅実装済
├── raw/                       # フェッチャー出力（後方検証のためコミット対象）
├── data/
│   ├── comments.json          # 運用者が手書きするセリフファイル
│   ├── predictions.json       # build_predictions.py が生成（未生成）
│   └── results.json           # build_results.py が生成（未生成）
├── tests/                     # パーサーのオフラインテスト（実サンプルHTMLは各自配置＝下記）
├── docs/                      # 仕様書・引き継ぎ書・サンプル・UIモック
└── .github/workflows/run_pipeline.yml  # workflow_dispatch 手動トリガー
```

## ドキュメント

### 仕様書（`docs/specs/`）— この順で読むと流れがわかる

| ファイル | 内容 |
|---|---|
| `データスキーマ仕様_v1.2.md` | ファイル間の契約（predictions / comments / results） |
| `取得項目_共通内部フォーマット仕様_v1.1.md` | フェッチャーの入出力契約（`raw/{week_id}.json`） |
| `スピード指数仕様_v1.md` | ①スピード指数の算出ロジック |
| `妙味メーター仕様_v1.md` | 妙味（0〜100）と鳳の降臨判定 |
| `買い目生成仕様_v1.md` | 合成スコア・印・キャラ別買い目 |

### 引き継ぎ書（`docs/handoff/`）

v1〜v8。**最新はv8**（成果物の統合・logic実装完了）。各版は前版からの差分なので、
着手前は **v8 → v7 → v6** の順で読むとよい。v1〜v5は決定の経緯を残した歴史的資料。

### その他

- `docs/samples/` … `raw` / `predictions` / `comments` / `results` の各サンプルJSON（＝実装の「目標の形」）
  - `raw.sample.json` は logic を単独で動かすための入力サンプル（1レース10頭・過去走つき）
- `docs/ui/keiba-3cards-mock-v7.html` … PWAの画面（単体HTML・キャラ絵4人をbase64で内蔵）。
  `data/predictions.json` などが読めれば実データを表示し、読めなければサンプル表示にフォールバックする
- `docs/ui/adapter.js` … JSON → 画面のデータ構造への変換（`tests/test_adapter.js` で検証）
- `docs/ui/予想師キャラクター外見設定_v1.md` … 4キャラの属性・外見・色調（セリフを書くときの拠り所）
- `docs/ui/SD_プロンプト集_予想師4キャラ.md` … キャラ絵の生成プロンプト（アニメ塗り路線）
- `docs/ui/archive/` … 旧世代モック（キャラ・妙味メーター導入前）
- `docs/OPEN_QUESTIONS.md` … 未確定・要確認事項の集約（着手前にここを見る）

## テストの実行

```bash
pip install -r requirements.txt

python3 tests/test_logic.py              # logic 全モジュール（仕様書の検算サンプルを固定）
python3 tests/test_build_predictions.py  # raw → predictions.json のエンドツーエンド
python3 tests/test_build_raw.py          # 絞り込み・週内キャッシュ・マージ（ネットワーク不要）
python3 tests/test_build_results.py      # 成績集計（サンプルの払戻を再現できるか）
python3 tests/test_fetchers_def.py       # D/E/F のパーサー
node    tests/test_adapter.js           # JSON → UI のデータ変換

python3 tests/test_a_race_list.py        # ※実サンプルHTMLの配置が必要（無ければスキップ）
python3 tests/test_c_horse_history.py    # ※同上
python3 tests/test_b_shutuba.py          # ※同上
```

`tests/test_logic.py` は**仕様書の検算サンプルをそのままテストにしている**ので、
通れば実装が仕様の数値と一致していることになる（スピード指数§1.4の指数82.7、
妙味メーター§4の myomi 73.8、買い目生成§6の sel 値と源さんの軸＝C）。

実サンプルHTMLはnetkeibaの著作物のため**このリポジトリには含めていない**（本リポジトリはパブリック）。
配置すべきファイルと入手方法は `tests/samples/README.md` を参照。

## パイプラインを回す

GitHub Actions の `週末3CARDS パイプライン` を workflow_dispatch で実行すると、下記が順に走る
（土日を別々に実行する運用。日曜ぶんは既存の `raw/{week_id}.json` にマージされる）。

```bash
python -m scraper.build_raw --week 2026-W27 --dates 2026-07-05   # netkeiba → raw/2026-W27.json
python -m logic.build_predictions --week 2026-W27                # raw → data/predictions.json
```

対象レースは `config/scraper.json` の `main_race` で決まる（既定＝各開催場の11R）。
`docs/samples/raw.sample.json` を `raw/` に置けば、スクレイピング無しで logic だけを試せる。

> ⚠ `config/base_times.json` が空のままだと、スピード指数の usable 判定で全ての過去走が落ち、
> **エラーにならないまま①が丸ごと効かなくなる**（警告ログは出る）。
> `scripts/build_base_times.py` の実装が①を動かす前提条件。

## 次のステップ

**フェッチャーは A/B/B2/C/D/E/F すべて実装済み。** 残っているのは下記。

1. **`scripts/build_base_times.py`**（スピード指数①の前提。取得元ページの確定が必要）
   … 空のままだと①が丸ごと欠損する。これが入って初めて①②③が揃う
2. **本番での疎通確認**（OPEN_QUESTIONS C-1）。Cookie無しでAJAX系APIが通るかは未検証
3. PWA の仕上げ（画面は実データ表示済み。残るは GitHub Pages の有効化と、
   manifest / Service Worker などPWAとしての体裁）
