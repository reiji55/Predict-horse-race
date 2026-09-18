"""
共通定数・正規化テーブル。

出典：
- データスキーマ仕様_v1.2.md §1（開催場ローマ字の正式対応表）
- 取得項目_共通内部フォーマット仕様_v1.md §2.1（正規化ルール）
"""

# JRA中央10場の日本語→ローマ字対応表（データスキーマ仕様 v1.2 §1で固定）
VENUE_ROMAJI = {
    "札幌": "sapporo",
    "函館": "hakodate",
    "福島": "fukushima",
    "新潟": "niigata",
    "東京": "tokyo",
    "中京": "chukyo",
    "京都": "kyoto",
    "阪神": "hanshin",
    "小倉": "kokura",
    "中山": "nakayama",
}

# 馬場状態の統一表記（取得項目仕様§2.1）。netkeibaの「稍」「不」等はここでvalueの正規表記に変換する
GOING_NORMALIZE = {
    "良": "良",
    "稍重": "稍重",
    "稍": "稍重",
    "重": "重",
    "不良": "不良",
    "不": "不良",
}

# クラス表記の統一（取得項目仕様§2.1／スピード指数仕様§1.3のclass_offsetキーと一致させる）
CLASS_KEYS = ["mi", "1win", "2win", "3win", "op", "g3", "g2", "g1"]

# 条件文字列→クラスキーの対応（b_shutuba・c_horse_historyで共有。二重定義しない）
# 出馬表（RaceData02）と馬戦績ページ（レース名列）の両方で使われる表記のゆらぎをまとめて吸収する。
CONDITION_GRADE_MAP = [
    ("新馬", "mi"),        # 新馬戦は未勝利と同じ入門クラスとして扱う（class_offsetに専用キー無し）
    ("未勝利", "mi"),
    ("１勝クラス", "1win"),
    ("1勝クラス", "1win"),
    ("２勝クラス", "2win"),
    ("2勝クラス", "2win"),
    ("３勝クラス", "3win"),
    ("3勝クラス", "3win"),
    ("オープン", "op"),
    # 出馬表の「過去5走」ページ（c2_shutuba_past）はクラスをアイコンの短い文字で出す
    # （"3勝クラス" ではなく "3勝"）。"3勝クラス" より後ろに置くことで、
    # 長い表記が先に一致するようにしてある。
    ("３勝", "3win"), ("3勝", "3win"),
    ("２勝", "2win"), ("2勝", "2win"),
    ("１勝", "1win"), ("1勝", "1win"),
]

# 馬戦績ページのレース名列にある括弧内グレード表記（例"(GIII)" "(L)"）→クラスキー
# "L"（Listed）はOPクラス内の重賞未満グレードのためopに丸める（買い目生成仕様のgrade定義にLは無い）
# Jpn表記（地方交流重賞）も同格として扱う。"OP" は過去5走ページのアイコン文字。

GRADE_TAG_MAP = {
    "GI": "g1", "GII": "g2", "GIII": "g3", "L": "op",
    "JpnI": "g1", "JpnII": "g2", "JpnIII": "g3",
    "OP": "op",
}


def normalize_class_label(text: str) -> str | None:
    """
    クラス表記の文字列を class キー（mi/1win/…/g1）に正規化する。

    グレード表記（GIII・JpnII・OP・L）を先に見て、次に条件文字列（未勝利・2勝クラス…）を見る。
    どちらにも当てはまらなければ None（取得項目仕様§2.0「取れなかったらnull」）。

    表記のゆれを吸収する場所をここ1箇所にまとめてある：
    馬戦績ページ（c_horse_history）は括弧内の "(GIII)"、
    出馬表の過去5走ページ（c2_shutuba_past）はアイコンの "3勝"、
    出馬表（b_shutuba）は "3歳以上2勝クラス" のような条件文と、経路ごとに書き方が違う。
    """
    text = (text or "").strip()
    if not text:
        return None
    if text in GRADE_TAG_MAP:
        return GRADE_TAG_MAP[text]
    for keyword, cls in CONDITION_GRADE_MAP:
        if keyword in text:
            return cls
    return None

# 券種（データスキーマ仕様§1 bets[].type／単勝・複勝は使わない）
BET_TYPES = ["ワイド", "馬連", "3連複"]

# キャラID（買い目生成仕様§5.1）
CHARACTER_IDS = ["kei", "tetsu", "gen", "otori"]

# 使えるJRA中央10場のみ（スピード指数仕様§2：地方・海外は基準タイム表がないため除外）
JRA_VENUES = set(VENUE_ROMAJI.keys())


def race_id(date_str: str, venue_jp: str, race_no: int) -> str:
    """
    内部主キー生成。 YYYYMMDD-venue-RR （R番号2桁ゼロ埋め）
    データスキーマ仕様 v1.2 §1 / 取得項目仕様§2.1 のルールに従う。

    date_str: "2026-07-05" のようなISO8601日付文字列
    venue_jp: "小倉" のような日本語場名
    race_no: 11 のような整数
    """
    date_compact = date_str.replace("-", "")
    romaji = VENUE_ROMAJI.get(venue_jp)
    if romaji is None:
        raise ValueError(f"未対応の開催場です（JRA中央10場以外）: {venue_jp}")
    return f"{date_compact}-{romaji}-{race_no:02d}"
