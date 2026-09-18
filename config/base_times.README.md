# base_times.json について

このファイルは手で編集しない。`scripts/build_base_times.py`（スピード指数仕様_v1 §4）が
1回きりの初期構築で自動生成する成果物の置き場所（今は空の `{}` プレースホルダー）。

生成後の形（venue×surface×distごとのOP水準タイム）：

```json
{
  "小倉": {
    "芝": { "1200": 67.8, "1800": 108.4 },
    "ダ":  { "1000": 58.2 }
  }
}
```

生成コマンド（`--dry-run` を付けると書き出さずレポートだけ出る）：

```bash
python -m scripts.build_base_times --source raw --dry-run          # 手持ちの raw から（ネットワーク不要）
python -m scripts.build_base_times --source file --input times.csv  # 手元のJSON/CSVから
python -m scripts.build_base_times --source netkeiba --start-year 2023 --end-year 2026
```

1コースあたり `--min-samples`（既定5）本に満たなければ表に載せない。
載らなかったコースの過去走は usable 判定#4 で落ちるので、①が欠損するだけで
エラーにはならない（警告ログは出る）。

再生成タイミング：年1回程度で十分（スピード指数仕様_v1 §4）。
