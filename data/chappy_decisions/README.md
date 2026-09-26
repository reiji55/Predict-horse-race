# チャットChappy decision log

発走前にチャットで作った1000円案の記録（shadow 研究用）。正式Chappyカード（`data/chappy_manual/`）とは別物。

- 置き場所: `data/chappy_decisions/{race_id}/{YYYYmmddTHHMMSS}.json`（1案1ファイル・追記のみ）
- ひな形: `python -m research.decision_log template --race {race_id} --out <path>`
- 検証: `python -m research.decision_log validate <path>`（CIでも実行）
- **発走前にコミットすること。** Gitの初回コミット時刻で発走前性を検証する

詳細: `docs/SHADOW_RESEARCH.md` §5
