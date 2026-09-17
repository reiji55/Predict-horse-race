/*
 * predictions.json / comments.json / results.json  →  UIモックのデータ構造へ変換するアダプタ。
 *
 * 引き継ぎ書v1 §7 は「スクレイパーが生成する predictions.json を RACES 形式に合わせれば
 * 差し替えるだけで動く」と書いていたが、その後スキーマ側が確定して差分が生まれた：
 *
 *   モック(v7)              データスキーマ v1.2
 *   ----------------------  ----------------------------------------
 *   distort                 myomi（+ myomi_parts）
 *   no:"11R" / cond:"芝…"   race_no / course{surface,dist,heads,note}
 *   hit:"52%"               hit_pct: 52
 *   range:"700〜1,400円"     payout_range: [700, 1400]
 *   bets[].h:[[枠,馬番],…]   bets[].horses:[馬番,…]（枠はUIが marks から引く）
 *   cards[].say             comments.json 側に分離
 *
 * その差を吸収するのがこのファイル。**表示の整形（%・円・カンマ区切り）はUIの責務**という
 * データスキーマ仕様§1 marks[] の原則に従い、整形はここで行う（JSONには生値を持つ）。
 *
 * ブラウザからは <script src="adapter.js"> で読み込み、window.W3CAdapter として使う。
 * Node からは require() でき、tests/test_adapter.js がサンプルJSONで検証している。
 */
(function (root) {
  "use strict";

  // grade コード → 表示ラベル（データスキーマ仕様§1 races[].grade）
  var GRADE_LABEL = {
    g1: "GⅠ", g2: "GⅡ", g3: "GⅢ", op: "OP",
    "3win": "3勝クラス", "2win": "2勝クラス", "1win": "1勝クラス", mi: "未勝利",
  };

  var CHAR_ORDER = ["kei", "tetsu", "gen", "otori"];

  function yen(value) {
    return Math.round(value).toLocaleString("ja-JP") + "円";
  }

  function signedYen(value) {
    return (value >= 0 ? "+" : "−") + Math.abs(Math.round(value)).toLocaleString("ja-JP") + "円";
  }

  function percent(value) {
    return Math.round(value) + "%";
  }

  /** レース条件の1行テキスト（例："芝1200m・GⅢ・ハンデ・18頭"）を組み立てる。 */
  function conditionText(race) {
    var course = race.course || {};
    var parts = [];
    if (course.surface && course.dist) parts.push(course.surface + course.dist + "m");
    if (GRADE_LABEL[race.grade]) parts.push(GRADE_LABEL[race.grade]);
    if (course.note) parts.push(course.note);
    if (course.heads) parts.push(course.heads + "頭");
    return parts.join("・");
  }

  /** 馬番 → 枠番 の対応表。買い目は馬番だけを持つので、枠色はここから引く（スキーマ§1 cards[]）。 */
  function wakuByNum(marks) {
    var map = {};
    (marks || []).forEach(function (mark) {
      map[mark.num] = mark.waku;
    });
    return map;
  }

  /**
   * predictions.json（+ comments.json）を、モックの RACES 配列に変換する。
   * comments は {race_id: {char_id: セリフ}}。無ければ say は空文字（UI側でフォールバック表示）。
   */
  function toRaces(predictions, comments) {
    comments = comments || {};
    return (predictions.races || []).map(function (race) {
      var waku = wakuByNum(race.marks);
      var say = comments[race.id] || {};

      return {
        id: race.id,
        day: race.day,
        venue: race.venue,
        no: race.race_no + "R",
        name: race.name,
        grade: race.grade,
        time: race.post_time,
        cond: conditionText(race),
        distort: Math.round(race.myomi),        // モックのメーターは distort という名前のまま
        myomi: race.myomi,                       // 生値も渡す（将来の2軸メーター用）
        myomi_parts: race.myomi_parts || null,
        legendary: !!race.legendary,
        // 無印（mk:""）の馬は表示しない。marks には後方検証のため全馬入っている（OPEN_QUESTIONS B-5）
        marks: (race.marks || []).filter(function (mark) {
          return mark.mk;
        }).map(function (mark) {
          return { mk: mark.mk, hon: !!mark.hon, waku: mark.waku, num: mark.num, name: mark.name };
        }),
        cards: (race.cards || []).map(function (card) {
          return {
            char: card.char,
            hit: percent(card.hit_pct),
            range: yen(card.payout_range[0]).replace("円", "") + "〜" + yen(card.payout_range[1]),
            total: card.total,
            bets: (card.bets || []).map(function (bet) {
              return {
                type: bet.type,
                h: bet.horses.map(function (num) { return [waku[num] || 0, num]; }),
                amt: bet.amt,
              };
            }),
            say: say[card.char] || "",
          };
        }),
      };
    });
  }

  /**
   * results.json を、モックの STATS に変換する。
   *
   * 集計トップは「全予想師のカードを毎回すべて買った場合の累計収支」に一本化する
   * （引き継ぎ書v1 §2・v3 §4.1）。合算の的中率のような曖昧な数値は出さず、マイナスも隠さない。
   * predictions はレース名を引くためだけに使う（results.json は race_id しか持たないため）。
   */
  function toStats(results, predictions) {
    var nameById = {};
    ((predictions || {}).races || []).forEach(function (race) {
      nameById[race.id] = race;
    });

    var overall = { spent: 0, payout: 0, races: 0 };
    var byChar = {};
    var history = [];

    (results.results || []).forEach(function (race) {
      overall.races += 1;
      var raceSpent = 0;
      var racePayout = 0;
      var hitChars = [];

      (race.cards || []).forEach(function (card) {
        overall.spent += card.spent;
        overall.payout += card.payout;
        raceSpent += card.spent;
        racePayout += card.payout;
        if (card.hit) hitChars.push(card.char);

        var stats = byChar[card.char] || (byChar[card.char] = {
          cards: 0, hits: 0, spent: 0, payout: 0, hitPayout: 0,
        });
        stats.cards += 1;
        stats.spent += card.spent;
        stats.payout += card.payout;
        if (card.hit) {
          stats.hits += 1;
          stats.hitPayout += card.payout;
        }
      });

      var meta = nameById[race.race_id] || {};
      history.push({
        hit: racePayout > 0,
        name: (meta.venue || "") + (meta.race_no ? meta.race_no + "R" : "") +
              (meta.name ? "(" + meta.name + ")" : race.race_id),
        meta: (meta.day ? meta.day + "曜・" : "") +
              (hitChars.length ? hitChars.join("・") + " が的中" : "全カード不的中"),
        pay: signedYen(racePayout - raceSpent),
        legendary: !!meta.legendary,
      });
    });

    var chars = CHAR_ORDER.filter(function (id) { return byChar[id]; }).map(function (id) {
      var stats = byChar[id];
      return {
        id: id,
        hit: stats.cards ? Math.round((stats.hits / stats.cards) * 100) : 0,
        roi: stats.spent ? Math.round((stats.payout / stats.spent) * 100) : 0,
        rec: stats.hits + "/" + stats.cards + "的中",
        avgPay: stats.hits ? yen(stats.hitPayout / stats.hits) : "—",
      };
    });

    return {
      balance: signedYen(overall.payout - overall.spent),
      bought: yen(overall.spent),
      paid: yen(overall.payout),
      roi: overall.spent ? percent((overall.payout / overall.spent) * 100) : "—",
      races: overall.races,
      chars: chars,
      history: history.reverse(),   // 新しいレースを上に
    };
  }

  var api = {
    toRaces: toRaces,
    toStats: toStats,
    conditionText: conditionText,
    GRADE_LABEL: GRADE_LABEL,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;       // Node（テスト）から
  }
  root.W3CAdapter = api;        // ブラウザから
})(typeof globalThis !== "undefined" ? globalThis : this);
