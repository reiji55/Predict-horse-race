# 週末3CARDS

土日メインレース対象、リスク度別3枚カード（+確変4枚目=鳳）で買い目と一言分析を提示する競馬予想PWA。

設計フェーズ（タスク1〜5）は完了。現在は実装フェーズ（タスク6：スクレイパー実装）。
**仕様書・引き継ぎ書はすべて `docs/` に同梱**（下記「ドキュメント」参照）。

## ディレクトリ構成

```
.
├── config/                    # チューニング対象パラメータ（各仕様書の設定ファイルをそのまま配置）
│   ├── speed_index.json       # スピード指数仕様§5
│   ├── myomi.json             # 妙味メーター仕様§8
│   ├── cards.json             # 買い目生成仕様§7
│   ├── base_times.json        # スピード指数仕様§4（生成物・今は空プレースホルダー）
│   └── base_times.README.md
├── scraper/
│   ├── common/
│   │   ├── constants.py       # 開催場romaji対応表・正規化ルール（取得項目仕様§2.1）
│   │   └── http.py            # レート制限付きHTTP取得（取得項目仕様§1.1 マナー設計）
│   ├── fetchers/
│   │   ├── a_race_list.py     # 開催日別レース一覧　　　✅実装済
│   │   ├── b_shutuba.py       # 出馬表　　　　　　　　　✅実装済
│   │   ├── b2_odds.py         # オッズAPI（AJAX直叩き）✅実装済
│   │   ├── c_horse_history.py # 馬の戦績（直近5走）　　 ✅実装済
│   │   ├── d_jockey_leading.py# 騎手リーディング　　　　🟡調査中（引き継ぎ書v7）
│   │   ├── e_trainer_leading.py# 調教師リーディング　　 ⬜未着手
│   │   └── f_results.py       # レース結果・払戻　　　　⬜未着手（タスク7）
│   └── build_raw.py           # A〜Eを束ねて raw/{week_id}.json を生成　⬜未実装
├── logic/                     # ⬜全モジュール未実装（数式は仕様書で確定済み）
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
│   └── build_results.py       # results.json 生成（タスク7）⬜未実装
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
| `データスキーマ仕様_v1.1.md` | ファイル間の契約（predictions / comments / results） |
| `取得項目_共通内部フォーマット仕様_v1.md` | フェッチャーの入出力契約（`raw/{week_id}.json`） |
| `スピード指数仕様_v1.md` | ①スピード指数の算出ロジック |
| `妙味メーター仕様_v1.md` | 妙味（0〜100）と鳳の降臨判定 |
| `買い目生成仕様_v1.md` | 合成スコア・印・キャラ別買い目 |

### 引き継ぎ書（`docs/handoff/`）

v1〜v7。**最新はv7**（D騎手リーディングの調査段階）。v7はv6からの差分のみなので、v6と併読すること。
v1〜v5は決定の経緯を残した歴史的資料。

### その他

- `docs/samples/` … `predictions` / `comments` / `results` の各サンプルJSON（＝実装の「目標の形」）
- `docs/ui/keiba-3cards-mock-v7.html` … UIモック最新版（単体HTML・キャラ絵4人をbase64で内蔵）
- `docs/ui/archive/` … 旧世代モック（キャラ・妙味メーター導入前）

## テストの実行

```bash
pip install -r requirements.txt
python3 tests/test_a_race_list.py
python3 tests/test_c_horse_history.py
```

実サンプルHTMLはnetkeibaの著作物のため**このリポジトリには含めていない**（本リポジトリはパブリック）。
配置すべきファイルと入手方法は `tests/samples/README.md` を参照。

## 次のステップ

1. D（騎手リーディング）・E（調教師リーディング）フェッチャーの実装（引き継ぎ書v7 §2）
2. `logic/` の実装（仕様書の数式どおり。検算サンプルがそのままテストになる）
3. `scripts/build_base_times.py`（スピード指数の前提。取得元ページの確定が必要）
4. GitHub Actionsワークフローの有効化（TODOコメントを実処理に置き換え）
