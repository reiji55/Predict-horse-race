# tests/samples — 実サンプルHTMLの配置場所

パーサーのオフラインテストは、netkeibaの実HTMLをここに置いて実行する。

## 同梱済み（このまま `python3 tests/test_*.py` が通る）

| ファイル | 対象テスト | 内容 |
|---|---|---|
| `race_list_get_date_list_20260620.html` | test_a_race_list.py | A・ステップ1（日付タブ一覧） |
| `race_list_sub_20260620.html` | test_a_race_list.py | A・ステップ2（レース一覧本体・2026-06-20土 東京/阪神/函館） |
| `horse_valkyrie.html` | test_c_horse_history.py | C（ヴァルキリーバース `db.netkeiba.com/horse/2022104764`） |

## 未同梱（各自で配置が必要）

| ファイル | 対象テスト | 入手方法 |
|---|---|---|
| `shutuba_fuchu.html` | test_b_shutuba.py | 府中牝馬S 2026 の出馬表ページ（枠順確定後）のPC版HTML |
| `shutuba_tanabata.html` | test_b_shutuba.py | 七夕賞 2026 の出馬表ページ（枠順確定前）のPC版HTML |
| （B2オッズAPIのレスポンス） | test_b_shutuba.py | オッズAPIの生レスポンス |

> B用サンプルが無い状態で `test_b_shutuba.py` を実行すると FileNotFoundError になるが、
> **B自体は実サンプル2件で検証済み**（引き継ぎ書v4 §2）。サンプルファイルが手元から
> 失われているだけなので、再取得して配置すればそのまま通る。

## 注意

実サンプルHTMLはnetkeibaの著作物なので、**個人利用の範囲にとどめ、リポジトリを公開する場合は
`.gitignore` でこのディレクトリを除外すること**。
