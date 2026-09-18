# tests/samples — 実サンプルHTMLの配置場所

パーサーのオフラインテストは、netkeibaの実HTMLをここに置いて実行する。

**このリポジトリはパブリックなので、実サンプルHTMLは1つもコミットしていない**
（`.gitignore` でこのディレクトリの中身を除外し、このREADMEだけを追跡している）。
手元に置いたぶんだけテストが走り、**無いファイルのテストは自動でスキップされる**
（落ちない）ので、全部揃っていなくても構わない。

| ファイル | 対象テスト | 入手元 |
|---|---|---|
| `race_list_get_date_list_20260620.html` | test_a_race_list.py | A・ステップ1（日付タブ一覧）のAJAXレスポンス |
| `race_list_sub_20260620.html` | test_a_race_list.py | A・ステップ2（レース一覧本体）のAJAXレスポンス |
| `horse_valkyrie.html` | test_c_horse_history.py | `db.netkeiba.com/horse/2022104764`（ヴァルキリーバース） |
| `shutuba_fuchu.html` | test_b_shutuba.py | `race.netkeiba.com/race/shutuba.html?race_id=202605030611`（府中牝馬S・枠順確定後） |
| `shutuba_tanabata.html` | test_b_shutuba.py | 七夕賞2026の出馬表（枠順確定前）。**未入手** |
| `odds_fuchu.txt` | test_b_shutuba.py | オッズAPIの生レスポンス。**未入手** |

### 保存のしかた（iPad/Safari）

1. Safariで対象ページを開く（**デスクトップ用Webサイトを表示**にしておく）
2. 共有 → オプション → **Webアーカイブ** → ファイルに保存
3. AJAXのレスポンス（APIを直接叩くもの）は、ショートカットAppの
   「URLの内容を取得」→「ファイルに保存」のほうが確実

> `tests/fixtures/` のほうは、実サンプルの**構造だけを再現した自作の小さいファイル**なので
> コミットしてある（D/E/Fのパーサーの回帰テストはそちらで回る）。

## 注意

実サンプルHTMLはnetkeibaの著作物なので、**個人利用の範囲にとどめ、リポジトリを公開する場合は
`.gitignore` でこのディレクトリを除外すること**。
