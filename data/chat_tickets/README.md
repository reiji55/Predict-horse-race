# data/chat_tickets/

チャットで出した本線（edge-v0）の参考券の記録。`python -m research.chat_tickets make` で作り、発走前に main にコミットする。

- 置き場所：`{race_id}/{YYYYMMDDTHHMMSS}.json`（JST の作成時刻）
- 一度コミットした記録は消さない・書き換えない。出し直すときは新しい記録を足す。
- 予想・正式成績・UI には使わない。仕様と確かめ方は [docs/research/CHAT_TICKETS.md](../../docs/research/CHAT_TICKETS.md)。
