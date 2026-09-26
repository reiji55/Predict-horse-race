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

  var CHAR_ORDER = ["kei", "tetsu", "gen", "chappy", "otori"];
  var CHAR_LABEL = {
    kei: "ケイ", tetsu: "哲さん", gen: "源さん",
    chatgpt: "チャット予想", chappy: "チャッピー", otori: "鳳",
  };

  function yen(value) {
    return Math.round(value).toLocaleString("ja-JP") + "円";
  }

  function signedYen(value) {
    return (value >= 0 ? "+" : "−") + Math.abs(Math.round(value)).toLocaleString("ja-JP") + "円";
  }

  function percent(value) {
    return Math.round(value) + "%";
  }

  var RACE_LIST_RETENTION_DAYS = 31;
  var STATS_RETENTION_DAYS = 365;

  function raceDateMs(raceId) {
    var raw = String(raceId || "").slice(0, 8);
    if (!/^\d{8}$/.test(raw)) return null;
    var y = Number(raw.slice(0, 4));
    var m = Number(raw.slice(4, 6));
    var d = Number(raw.slice(6, 8));
    return Date.UTC(y, m - 1, d);
  }

  function retainRecentById(rows, days, idOf) {
    var dated = rows.map(function (row) {
      return { row: row, ms: raceDateMs(idOf(row)) };
    });
    var usable = dated.filter(function (x) { return x.ms != null; });
    if (!usable.length) return rows.slice();

    var latest = Math.max.apply(null, usable.map(function (x) { return x.ms; }));
    var cutoff = latest - days * 24 * 60 * 60 * 1000;
    return dated.filter(function (x) {
      return x.ms == null || x.ms >= cutoff;
    }).map(function (x) { return x.row; });
  }

  function fallbackRaceLabel(raceId) {
    var parts = String(raceId || "").split("-");
    var venueMap = {
      nakayama: "中山", hanshin: "阪神", tokyo: "東京", kyoto: "京都",
      kokura: "小倉", chukyo: "中京", niigata: "新潟", fukushima: "福島",
      sapporo: "札幌", hakodate: "函館",
    };
    if (parts.length >= 3) {
      var venue = venueMap[parts[1]] || parts[1] || "";
      var no = Number(parts[2]);
      if (venue && Number.isFinite(no)) return venue + no + "R";
    }
    return "過去レース";
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

  /** 買い目1点を一意に表す鍵。券種＋馬番（順不同）で、予想側と結果側を突き合わせる。 */
  function betKey(bet) {
    return bet.type + ":" + (bet.horses || []).slice().sort(function (a, b) {
      return a - b;
    }).join("-");
  }

  /**
   * results.json を {race_id: {char: {betKey: 確定した1点}}} に畳む。
   *
   * 予想（predictions.json）には「いくら買ったか」しか無く、的中したか・いくら返ってきたかは
   * 結果（results.json）側にある。レース画面で買い目ごとに的中を出すには、ここで結び直す。
   */
  function settlementIndex(results) {
    var index = {};
    ((results || {}).results || []).forEach(function (race) {
      var byChar = index[race.race_id] = {};
      (race.cards || []).forEach(function (card) {
        var bets = {};
        (card.bets || []).forEach(function (bet) {
          bets[betKey(bet)] = bet;
        });
        byChar[card.char] = {
          hit: !!card.hit, spent: card.spent, payout: card.payout, bets: bets,
        };
      });
    });
    return index;
  }

  /** レース一覧用の確定結果サマリ。結果未取得レースは index に存在しない。 */
  function resultSummaryIndex(results) {
    var index = {};
    ((results || {}).results || []).forEach(function (race) {
      var spent = 0, payout = 0, hitChars = [];
      (race.cards || []).forEach(function (card) {
        if (card.action === "pass") return;
        spent += Number(card.spent || 0);
        payout += Number(card.payout || 0);
        if (card.hit) hitChars.push(CHAR_LABEL[card.char] || card.char);
      });
      index[race.race_id] = {
        settled: true,
        hit: payout > 0,
        spent: spent,
        payout: payout,
        balance: payout - spent,
        hit_chars: hitChars,
        finish: (race.finish || []).slice(0, 3),
      };
    });
    return index;
  }

  function settledResultToRace(result) {
    var meta = result.meta || {};
    var marks = meta.marks || [];
    var waku = wakuByNum(marks);
    var spent = 0, payout = 0, hitChars = [];
    (result.cards || []).forEach(function (card) {
      if (card.action === "pass") return;
      spent += Number(card.spent || 0);
      payout += Number(card.payout || 0);
      if (card.hit) hitChars.push(CHAR_LABEL[card.char] || card.char);
    });

    return {
      id: result.race_id,
      day: meta.day || "",
      venue: meta.venue || "",
      no: meta.race_no ? meta.race_no + "R" : "",
      name: meta.name || fallbackRaceLabel(result.race_id),
      grade: meta.grade || null,
      time: meta.post_time || "",
      cond: conditionText(meta),
      distort: meta.myomi == null ? null : Math.round(meta.myomi),
      myomi: meta.myomi == null ? null : meta.myomi,
      myomi_parts: meta.myomi_parts || null,
      legendary: !!meta.legendary,
      status: meta.status || (result.evaluation_scope === "manual_chat" ? "manual_record" : "settled"),
      status_note: meta.status_note || result.record_note || "",
      result_status: payout > 0 ? "hit" : "miss",
      result_payout: payout,
      result_spent: spent,
      result_balance: payout - spent,
      result_hit_chars: hitChars,
      result_finish: (result.finish || []).slice(0, 3).map(function (num) {
        return [waku[num] || 0, num];
      }),
      marks: marks.filter(function (mark) {
        return mark.mk;
      }).map(function (mark) {
        return { mk: mark.mk, hon: !!mark.hon, waku: mark.waku, num: mark.num, name: mark.name };
      }),
      cards: (result.cards || []).map(function (card) {
        return {
          char: card.char,
          hit: "—",
          range: "—",
          total: Number(card.spent || 0),
          bets: (card.bets || []).map(function (bet) {
            return {
              type: bet.type,
              h: (bet.horses || []).map(function (num) { return [waku[num] || 0, num]; }),
              amt: bet.amt,
              won: !!bet.hit,
              payout: Number(bet.payout || 0),
            };
          }),
          settled: card.action === "pass" ? null : {
            hit: !!card.hit,
            spent: Number(card.spent || 0),
            payout: Number(card.payout || 0),
            balance: Number(card.payout || 0) - Number(card.spent || 0),
          },
          say: card.say || "",
          conviction: card.conviction == null ? null : card.conviction,
          portfolio_style: card.portfolio_style || null,
          source: card.source || null,
          manual: (card.source || card.model_role) === "manual_chat",
          model_version: card.model_version || null,
          decision_log: card.decision_log || null,
        };
      }),
    };
  }

  /**
   * predictions.json（+ comments.json + results.json）を、モックの RACES 配列に変換する。
   * comments は {race_id: {char_id: セリフ}}。無ければ say は空文字（UI側でフォールバック表示）。
   * results を渡すと、各買い目に確定した hit / payout が付く（結果が出ていなければ付かない）。
   */
  function toRaces(predictions, comments, results) {
    comments = comments || {};
    var settled = settlementIndex(results);
    var resultSummaries = resultSummaryIndex(results);
    var races = (predictions.races || []).map(function (race) {
      var waku = wakuByNum(race.marks);
      var say = comments[race.id] || {};
      var raceSettled = settled[race.id] || {};
      var resultSummary = resultSummaries[race.id] || null;

      return {
        id: race.id,
        day: race.day,
        venue: race.venue,
        no: race.race_no + "R",
        name: race.name,
        grade: race.grade,
        time: race.post_time,
        cond: conditionText(race),
        distort: race.myomi == null ? null : Math.round(race.myomi),
        myomi: race.myomi,                       // 生値も渡す（将来の2軸メーター用）
        myomi_parts: race.myomi_parts || null,
        legendary: !!race.legendary,
        status: race.status || null,
        status_note: race.status_note || "",
        result_status: resultSummary ? (resultSummary.hit ? "hit" : "miss") : null,
        result_payout: resultSummary ? resultSummary.payout : null,
        result_spent: resultSummary ? resultSummary.spent : null,
        result_balance: resultSummary ? resultSummary.balance : null,
        result_hit_chars: resultSummary ? resultSummary.hit_chars : [],
        result_finish: resultSummary ? resultSummary.finish.map(function (num) {
          return [waku[num] || 0, num];
        }) : [],
        // 無印（mk:""）の馬は表示しない。marks には後方検証のため全馬入っている（OPEN_QUESTIONS B-5）
        marks: (race.marks || []).filter(function (mark) {
          return mark.mk;
        }).map(function (mark) {
          return { mk: mark.mk, hon: !!mark.hon, waku: mark.waku, num: mark.num, name: mark.name };
        }),
        cards: (race.cards || []).map(function (card) {
          var cardSettled = raceSettled[card.char] || null;
          return {
            char: card.char,
            hit: card.hit_pct == null ? "—" : percent(card.hit_pct),
            range: (card.payout_range && card.payout_range.length >= 2)
              ? yen(card.payout_range[0]).replace("円", "") + "〜" + yen(card.payout_range[1])
              : "—",
            total: card.total,
            bets: (card.bets || []).map(function (bet) {
              var done = cardSettled ? cardSettled.bets[betKey(bet)] : null;
              return {
                type: bet.type,
                h: bet.horses.map(function (num) { return [waku[num] || 0, num]; }),
                amt: bet.amt,
                // 結果が未確定なら null のまま（UIは「的中！」を出さない）
                won: done ? !!done.hit : null,
                payout: done ? done.payout : null,
              };
            }),
            // カード単位の確定収支。レースが終わっていなければ null
            settled: cardSettled ? {
              hit: cardSettled.hit, spent: cardSettled.spent, payout: cardSettled.payout,
              balance: cardSettled.payout - cardSettled.spent,
            } : null,
            say: say[card.char] || card.say || "",
            conviction: card.conviction == null ? null : card.conviction,
            portfolio_style: card.portfolio_style || null,
            source: card.source || null,
            // 手動（ChatGPTがチャットで組んだ）か自動（アプリが組んだ）かの区別。
            // 表示ラベルはUI側が決める（本線モデル / 検証モデル）。
            manual: (card.source || card.model_role) === "manual_chat",
            model_version: card.model_version || null,
            decision_log: card.decision_log || null,
          };
        }),
      };
    });

    var seen = {};
    races.forEach(function (race) { seen[race.id] = true; });
    ((results || {}).results || []).forEach(function (result) {
      if (!result.race_id || seen[result.race_id]) return;
      races.push(settledResultToRace(result));
      seen[result.race_id] = true;
    });

    return retainRecentById(races, RACE_LIST_RETENTION_DAYS, function (race) {
      return race.id;
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
    var retainedResults = retainRecentById(
      (results.results || []), STATS_RETENTION_DAYS,
      function (race) { return race.race_id; }
    );

    retainedResults.forEach(function (race) {
      var isManualChat = race.evaluation_scope === "manual_chat";
      if (!isManualChat) overall.races += 1;
      var raceSpent = 0;
      var racePayout = 0;
      var hitChars = [];

      (race.cards || []).forEach(function (card) {
        // 見送り(PASS)は0円・不的中扱いにしない。的中率の分母にも入れない。
        if (card.action === "pass") {
          if (isManualChat) return;
          var passStats = byChar[card.char] || (byChar[card.char] = {
            cards: 0, hits: 0, spent: 0, payout: 0, hitPayout: 0, passes: 0,
          });
          passStats.passes = (passStats.passes || 0) + 1;
          return;
        }
        raceSpent += card.spent;
        racePayout += card.payout;
        if (card.hit) hitChars.push(CHAR_LABEL[card.char] || card.char);

        // ChatGPT会話で作った本線カード(manual_chat)は、累計収支（自動パイプラインの
        // 全カード購入時）には混ぜない。ただしチャッピー/鳳の本線はまさにこの手動カード
        // なので、予想師別の成績には載せる（載せないとチャッピーの行が永遠に出ない）。
        var chappyLayer = card.char === "chappy" || card.char === "otori";
        if (isManualChat && !chappyLayer) return;

        if (!isManualChat) {
          overall.spent += card.spent;
          overall.payout += card.payout;
        }
        var stats = byChar[card.char] || (byChar[card.char] = {
          cards: 0, hits: 0, spent: 0, payout: 0, hitPayout: 0, passes: 0,
        });
        if (isManualChat) stats.manual = (stats.manual || 0) + 1;
        stats.cards += 1;
        stats.spent += card.spent;
        stats.payout += card.payout;
        if (card.hit) {
          stats.hits += 1;
          stats.hitPayout += card.payout;
        }
      });

      var meta = nameById[race.race_id] || race.meta || {};
      history.push({
        hit: racePayout > 0,
        name: (meta.venue || "") + (meta.race_no ? meta.race_no + "R" : "") +
              (meta.name ? "(" + meta.name + ")" : fallbackRaceLabel(race.race_id)),
        // results.json の finish は着順どおりの馬番配列。
        // 予想カードと同じ枠色チップで表示できるよう [枠番, 馬番] にして渡す。
        top3: (function () {
          var waku = wakuByNum(meta.marks || []);
          return (race.finish || []).slice(0, 3).map(function (num) {
            return [waku[num] || 0, num];
          });
        })(),
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
        rec: stats.hits + "/" + stats.cards + "的中" +
             (stats.passes ? "・見送り" + stats.passes : ""),
        avgPay: stats.hits ? yen(stats.hitPayout / stats.hits) : "—",
        // 1カードあたりの購入額（ケイ・哲さん・源さんは500円、チャッピー/鳳は1,000円）
        stake: stats.cards ? yen(stats.spent / stats.cards) : "—",
        manual: stats.manual || 0,
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
    betKey: betKey,
    toStats: toStats,
    conditionText: conditionText,
    GRADE_LABEL: GRADE_LABEL,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;       // Node（テスト）から
  }
  root.W3CAdapter = api;        // ブラウザから
})(typeof globalThis !== "undefined" ? globalThis : this);
