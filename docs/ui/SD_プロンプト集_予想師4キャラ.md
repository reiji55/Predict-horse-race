# Stable Diffusion プロンプト集 — 予想師4キャラ (v2: アニメ塗り路線)

## 画風について
v1では「ミスタードリラー風」を狙ってちびキャラ・マスコット系のプロンプトを組みましたが、
実際に出た絵は **精密な線画・繊細な陰影の美少年/美形アニメイラスト** という別方向でした。
見比べた結果、こちらの方向で正式採用に決定。以下は採用した画風を言語化し、
残り3キャラをこのタッチで揃えるためのプロンプトです。

## 共通スタイル (STYLE) — 各プロンプト冒頭に付ける
```
anime illustration, detailed cel shading, clean sharp lineart, semi-realistic proportions,
soft gradient shading on face, glossy detailed eyes, portrait bust-up, front facing,
plain white or light background, high quality anime key visual style, calm natural expression
```

## 共通ネガティブ (NEGATIVE) — 全キャラ共通で使う
```
chibi, deformed, mascot, cartoon flat coloring, 3d render, photorealistic photo,
extra fingers, deformed hands, text, watermark, signature, multiple characters,
cluttered background, low quality, blurry, bad anatomy
```

---

## 🧑‍🎓 ケイ (低リスク・数理院生) — 採用済み・基準サンプル
```
[STYLE], a young male graduate student in his early-to-mid 20s, slim build,
dark messy short hair with a bit of volume on top, round black-framed glasses,
warm brown eyes, calm faint gentle smile, light stubble optional,
gray hoodie layered over a light shirt, green lanyard with a university ID card,
holding the ID card up slightly, soft green color accents
```
Negative: `[NEGATIVE]`

---

## 👨‍👧 哲さん (中リスク・週末の馬券パパ)
```
[STYLE], a friendly Japanese father in his early 40s, average build with a slight belly,
short neat hair with a receding hairline, warm relaxed smile, kind tired eyes,
casual polo shirt or checked short-sleeve shirt, holding a folded race program,
a wristband from a racecourse on one wrist, warm amber and gold color accents,
approachable "weekend dad" atmosphere
```
Negative: `[NEGATIVE]`

---

## 👴 源さん (高リスク・ベレー帽の大穴師)
```
[STYLE], a scruffy old gambler man in his late 60s to 70s, thin and weathered,
tanned wrinkled skin, white stubble beard, wearing a worn navy beret,
messy white hair peeking from under the beret, faded old jacket with a scarf,
big mischievous grin with one eye slightly squinted, holding a rolled up newspaper,
muted dusty red color accents, charming shabby "racecourse regular" vibe
```
Negative: `[NEGATIVE]`

---

## 🎩 鳳 / オオトリ (確変・伝説の予想師)

※1回目: シルクハットで「若い美青年の睨み顔」に。サングラスに変更したところ、
2回目は「中世ファンタジーの騎士/悪役」風になってしまいました(マント状コート・金の縁取り装飾・
風になびく前髪・鋭い流し目)。狙いは **もっとデフォルメの効いたポップな絵柄で、
渋谷を歩いてそうな今どきの金持ちアニキ** なので、以下は3回目の修正版です。

```
[STYLE], (deformed proportions:1.2), playful pop anime style, slightly exaggerated cute features,
a modern flashy Japanese "big brother" (aniki) type man in his 30s, tanned skin,
stylish modern undercut hairstyle, wearing dark stylish sunglasses,
a wide friendly grin or playful smirk (approachable, not scary),
modern trendy open-collar shirt or casual jacket (NOT a long coat or cape),
gold chain necklace, gold rings, holding a stack of cash with a fun confident pose,
vibrant pop gold and black color scheme, modern Shibuya nightlife wealthy vibe,
casual confident swagger, charismatic but friendly energy
```
Negative: `[NEGATIVE], fantasy, medieval, cape, long coat, ornate gold trim armor, serious solemn glare, intense hero stare, light novel villain, wind-blown dramatic hair, realistic tall proportions, cold expression`

**変更ポイント(3回目)**:
- スタイル: `(deformed proportions:1.2)` で頭身を詰め、`playful pop anime style` でポップさを強調
- 見た目のコンセプト自体を変更: 「中世的ダンディ紳士」→ **「現代・渋谷系の金持ちアニキ」**。長いコートやマント、金の装飾トリムを明示的に除外
- 表情: 鋭い流し目・不敵な微笑み → **人懐こい笑み/ニヤッとした表情**(怖さより愛嬌)
- ネガティブに `fantasy, medieval, cape, long coat, ornate gold trim armor, light novel villain, wind-blown dramatic hair` を追加し、ファンタジー方向への引っ張られを遮断


---

## 使い方メモ
- `[STYLE]` `[NEGATIVE]` は上の共通ブロックを実際に貼り付けて使う
- ケイの絵と同じモデル・同じ設定(サンプラー、CFGスケールなど)を使うと画風が揃いやすい
- 4人で画風を揃えるコツ: **同じseedを固定**し、キャラ記述部分だけ差し替える
- 背景は生成時は白/淡色のままでよい。アプリ側でカードカラーの背景に乗せるので、切り抜きやすい単色背景が扱いやすい
- 縦横比は 1:1(アイコン用途)。768x768〜896x896程度で生成 → こちらで256pxに縮小して埋め込み
