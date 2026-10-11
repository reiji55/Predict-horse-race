# チャット実験

チャットで作った買い目のロジック（仮説）の、発走前の記録（shadow 研究用）。正式成績・UI・予想には使わない。

- ルール: `data/chat_experiments/{experiment_id}/rule_{version}.md`（最初のレースより前に1回だけ追加。書き換えない）
- 記録: `data/chat_experiments/{experiment_id}/races/{race_id}/{YYYYmmddTHHMMSS}.json`（1レース1案・追記のみ）
- 検証: `python -m research.chat_experiment validate <path>`（CI でも実行）
- **GitHub の Web 画面か ChatGPT のコネクタで、発走前に main へコミットすること。** Git のコミット時刻で発走前性を検証する

詳細: `docs/research/CHAT_EXPERIMENTS.md`、ルールのひな形: `docs/research/CHAT_EXPERIMENT_RULE_TEMPLATE.md`
