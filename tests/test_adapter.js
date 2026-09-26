/*
 * docs/ui/adapter.js のテスト（Node で実行する）。
 *
 * サンプルJSON（docs/samples/）を入力に、UIモックが期待する形へ正しく変換できるかを検証する。
 *
 * 実行： node tests/test_adapter.js
 */
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const adapter = require(path.join(ROOT, "docs/ui/adapter.js"));

const read = (p) => JSON.parse(fs.readFileSync(path.join(ROOT, p), "utf-8"));
const predictions = read("docs/samples/predictions.sample.json");
const comments = read("docs/samples/comments.sample.json");
const results = read("docs/samples/results.sample.json");

const tests = {};

tests.test_races_basic_shape = () => {
  const races = adapter.toRaces(predictions, comments);
  assert.strictEqual(races.length, 2);

  const race = races[0];
  assert.strictEqual(race.id, "20260705-kokura-01");
  assert.strictEqual(race.no, "1R");
  assert.strictEqual(race.time, "9:50");
  assert.strictEqual(race.cond, "ダ1000m・未勝利・14頭");
  assert.strictEqual(race.distort, 92);          // myomi → distort（モックのメーター名）
  assert.strictEqual(race.legendary, true);
  assert.deepStrictEqual(race.myomi_parts, { umami: 0.92, conf: 1.0 });
};

tests.test_condition_text_includes_handicap = () => {
  const race = adapter.toRaces(predictions, comments)[1];
  assert.strictEqual(race.cond, "芝1200m・GⅢ・ハンデ・18頭");   // note（ハンデ）が入る
  assert.strictEqual(race.legendary, false);
};

tests.test_marks_drop_unmarked_horses = () => {
  const withUnmarked = JSON.parse(JSON.stringify(predictions));
  withUnmarked.races[0].marks.push({ mk: "", num: 13, waku: 8, name: "無印馬", odds: 80.0, score: 40.1 });

  const race = adapter.toRaces(withUnmarked, comments)[0];
  assert.strictEqual(race.marks.length, 5);                    // 無印は表示しない
  assert.ok(race.marks.every((m) => m.mk !== ""));
  assert.strictEqual(race.marks[0].hon, true);
};

tests.test_bets_carry_waku_for_color = () => {
  const race = adapter.toRaces(predictions, comments)[0];
  const card = race.cards.find((c) => c.char === "otori");

  // 買い目は馬番だけ持つので、枠番は marks から引いて [枠, 馬番] にする
  // won/payout は results を渡したときだけ埋まる（ここでは未確定なので null）
  assert.deepStrictEqual(card.bets[0],
    { type: "ワイド", h: [[3, 4], [6, 9]], amt: 200, won: null, payout: null });
  assert.deepStrictEqual(card.bets[4].h, [[3, 4], [6, 9], [1, 1]]);   // 3連複
  assert.strictEqual(card.total, 1000);
};

tests.test_bets_resolve_waku_even_for_unmarked_horses = () => {
  // 無印の馬が買い目に入っていても枠色が引けること（marksには全馬入っている前提）
  const data = JSON.parse(JSON.stringify(predictions));
  data.races[0].marks.push({ mk: "", num: 13, waku: 8, name: "無印馬", odds: 80.0, score: 40.1 });
  data.races[0].cards[0].bets[0] = { type: "ワイド", horses: [4, 13], amt: 200 };

  const card = adapter.toRaces(data, comments)[0].cards[0];
  assert.deepStrictEqual(card.bets[0].h, [[3, 4], [8, 13]]);
};

tests.test_display_formatting = () => {
  const race = adapter.toRaces(predictions, comments)[0];
  const otori = race.cards.find((c) => c.char === "otori");

  assert.strictEqual(otori.hit, "38%");                 // hit_pct: 38 → "38%"
  assert.strictEqual(otori.range, "3,000〜48,000円");    // [3000, 48000] → カンマ区切り
};

tests.test_comments_are_merged = () => {
  const races = adapter.toRaces(predictions, comments);
  const kei = races[0].cards.find((c) => c.char === "kei");
  assert.ok(kei.say.startsWith("未勝利戦は母集団が小さく"));

  // セリフが無いレース・キャラは空文字（UI側で「準備中」等にフォールバックする）
  const noComments = adapter.toRaces(predictions, {});
  assert.strictEqual(noComments[0].cards[0].say, "");
};

tests.test_chappy_card_uses_embedded_say_and_1000_yen = () => {
  const data = JSON.parse(JSON.stringify(predictions));
  data.races[0].cards.push({
    char:"chappy", hit_pct:31, payout_range:[1400,12000], total:1000,
    say:"勝ち軸は12番。Top3妙味は14番を最重視。",
    source:"signal_engine", conviction:0.81, portfolio_style:"balanced_edge_1000",
    decision_log:{roles:{win_anchor:{num:12},top3_edge:{num:14}}},
    bets:[
      {type:"ワイド",horses:[4,9],amt:200},
      {type:"ワイド",horses:[4,1],amt:100},
      {type:"ワイド",horses:[9,1],amt:100},
      {type:"ワイド",horses:[4,11],amt:100},
      {type:"馬連",horses:[4,9],amt:100},
      {type:"3連複",horses:[4,9,1],amt:100},
      {type:"3連複",horses:[4,1,11],amt:100},
      {type:"3連複",horses:[9,1,11],amt:100},
      {type:"3連複",horses:[4,9,11],amt:100},
    ],
  });

  const race = adapter.toRaces(data, {});
  const chappy = race[0].cards.find((x)=>x.char==="chappy");
  assert.strictEqual(chappy.total, 1000);
  assert.strictEqual(chappy.say, "勝ち軸は12番。Top3妙味は14番を最重視。");
  assert.strictEqual(chappy.source, "signal_engine");
  assert.strictEqual(chappy.conviction, 0.81);
};


tests.test_stats_from_results = () => {
  const stats = adapter.toStats(results, predictions);

  assert.strictEqual(stats.races, 1);
  assert.strictEqual(stats.bought, "2,500円");      // 鳳1000 + 3人×500
  assert.strictEqual(stats.paid, "12,955円");
  assert.strictEqual(stats.balance, "+10,455円");
  assert.strictEqual(stats.roi, "518%");

  const gen = stats.chars.find((c) => c.id === "gen");
  assert.strictEqual(gen.hit, 0);
  assert.strictEqual(gen.rec, "0/1的中");
  assert.strictEqual(gen.avgPay, "—");              // 的中ゼロなら平均払戻は出さない

  const kei = stats.chars.find((c) => c.id === "kei");
  assert.strictEqual(kei.roi, 588);                 // 2940 / 500

  // 直近結果のレース名エリアに1〜3着の馬番を表示するため、finish先頭3頭を渡す
  const firstRace = predictions.races.find((r) => r.id === results.results[0].race_id);
  const waku = Object.fromEntries(firstRace.marks.map((m) => [m.num, m.waku]));
  assert.deepStrictEqual(
    stats.history[0].top3,
    results.results[0].finish.slice(0, 3).map((num) => [waku[num] || 0, num])
  );
};

tests.test_stats_show_losses_honestly = () => {
  // 全カード不的中のレースを足すと、収支がマイナスに振れることを確認する
  const losing = JSON.parse(JSON.stringify(results));
  losing.results.push({
    race_id: "20260705-kokura-11",
    finish: [1, 2, 3],
    dividends: {},
    cards: [
      { char: "kei", hit: false, spent: 500, payout: 0, bets: [] },
      { char: "tetsu", hit: false, spent: 500, payout: 0, bets: [] },
      { char: "gen", hit: false, spent: 500, payout: 0, bets: [] },
    ],
  });

  const stats = adapter.toStats(losing, predictions);
  assert.strictEqual(stats.races, 2);
  assert.strictEqual(stats.bought, "4,000円");
  assert.strictEqual(stats.history[0].hit, false);            // 新しいレースが先頭
  assert.strictEqual(stats.history[0].pay, "−1,500円");
  assert.ok(stats.history[0].name.includes("CBC賞"));
  assert.strictEqual(stats.history[0].meta, "日曜・全カード不的中");
};

tests.test_empty_results_do_not_crash = () => {
  const stats = adapter.toStats({ results: [] }, predictions);
  assert.strictEqual(stats.races, 0);
  assert.strictEqual(stats.roi, "—");
  assert.deepStrictEqual(stats.chars, []);
};

// --- 的中買い目の表示（2026-09-23 のUI改修）-------------------------------

tests.test_settled_bets_carry_hit_and_payout_into_the_race_view = () => {
  // レース画面は predictions.json しか見ていなかったので、
  // 「どの買い目が当たっていくら返ったか」を出せなかった。results を結び直す。
  const races = adapter.toRaces(predictions, comments, results);
  const race = races.find((r) => r.id === "20260705-kokura-01");
  const kei = race.cards.find((c) => c.char === "kei");

  const won = kei.bets.filter((b) => b.won === true);
  assert.ok(won.length > 0, "的中した買い目が1点も拾えていない");
  won.forEach((b) => assert.ok(b.payout > 0, "的中なのに払戻が0"));
  kei.bets.filter((b) => b.won === false).forEach((b) => {
    assert.strictEqual(b.payout, 0);
  });

  // カード単位の確定収支も出せること
  assert.strictEqual(kei.settled.spent, kei.total);
  assert.strictEqual(kei.settled.balance, kei.settled.payout - kei.settled.spent);
};

tests.test_race_list_gets_settled_hit_status_and_payout = () => {
  const races = adapter.toRaces(predictions, comments, results);
  const race = races.find((r) => r.id === results.results[0].race_id);
  const source = results.results[0];
  const expectedPayout = source.cards.reduce((sum, card) =>
    sum + (card.action === "pass" ? 0 : Number(card.payout || 0)), 0);
  const expectedSpent = source.cards.reduce((sum, card) =>
    sum + (card.action === "pass" ? 0 : Number(card.spent || 0)), 0);
  assert.strictEqual(race.result_status, expectedPayout > 0 ? "hit" : "miss");
  assert.strictEqual(race.result_payout, expectedPayout);
  assert.strictEqual(race.result_spent, expectedSpent);
  assert.strictEqual(race.result_balance, expectedPayout - expectedSpent);
  const predRace = predictions.races.find((r) => r.id === source.race_id);
  const waku = Object.fromEntries(predRace.marks.map((m) => [m.num, m.waku]));
  assert.deepStrictEqual(
    race.result_finish,
    source.finish.slice(0, 3).map((num) => [waku[num] || 0, num])
  );
};

tests.test_race_list_has_no_result_status_before_settlement = () => {
  const races = adapter.toRaces(predictions, comments, { results: [] });
  races.forEach((race) => {
    assert.strictEqual(race.result_status, null);
    assert.strictEqual(race.result_payout, null);
    assert.deepStrictEqual(race.result_hit_chars, []);
  });
};

tests.test_race_list_keeps_recent_settled_races_missing_from_latest_predictions = () => {
  const historical = JSON.parse(JSON.stringify(results));
  historical.results.push({
    race_id: "20260620-hanshin-11",
    finish: [11, 9, 12],
    dividends: {},
    evaluation_scope: "manual_chat",
    meta: {
      day: "土", venue: "阪神", race_no: 11, name: "履歴テストS", grade: "g3",
      post_time: "15:30", course: {surface:"ダ", dist:1800, heads:16},
      status: "manual_record", marks: [
        {mk:"◎", hon:true, num:11, waku:6, name:"A"},
        {mk:"○", num:9, waku:5, name:"B"},
        {mk:"▲", num:12, waku:6, name:"C"},
      ],
    },
    cards: [{
      char:"chappy", source:"manual_chat", model_role:"manual_chat",
      hit:true, spent:1000, payout:5000,
      bets:[{type:"3連複",horses:[9,11,12],amt:100,hit:true,payout:5000}],
    }],
  });
  historical.results.push({
    race_id: "20260501-tokyo-11",
    finish: [1,2,3], dividends:{},
    meta:{venue:"東京",race_no:11,name:"古すぎるレース",marks:[]},
    cards:[],
  });

  const races = adapter.toRaces(predictions, comments, historical);
  const kept = races.find((r) => r.id === "20260620-hanshin-11");
  assert.ok(kept, "直近1か月の確定レースが一覧に復元される");
  assert.strictEqual(kept.name, "履歴テストS");
  assert.strictEqual(kept.result_status, "hit");
  assert.strictEqual(kept.cards[0].manual, true);
  assert.ok(!races.find((r) => r.id === "20260501-tokyo-11"),
            "1か月より古いレースは一覧から畳む");
};

tests.test_stats_keep_one_year_even_when_predictions_no_longer_contain_race = () => {
  const data = {
    results: [
      {
        race_id:"20250701-hanshin-11", finish:[1,2,3], dividends:{},
        meta:{venue:"阪神",race_no:11,name:"一年超前",marks:[]},
        cards:[{char:"kei",hit:true,spent:500,payout:999999,bets:[]}],
      },
      {
        race_id:"20260705-kokura-01", finish:[1,2,3], dividends:{},
        meta:{venue:"小倉",race_no:1,name:"最新",marks:[]},
        cards:[{char:"kei",hit:false,spent:500,payout:0,bets:[]}],
      },
    ],
  };
  const stats = adapter.toStats(data, {races:[]});
  assert.strictEqual(stats.races, 1);
  assert.strictEqual(stats.bought, "500円");
  assert.strictEqual(stats.paid, "0円");
  assert.ok(stats.history[0].name.includes("最新"));
  assert.ok(!stats.history.some((h) => h.name.includes("一年超前")));
};

tests.test_stats_never_fall_back_to_raw_race_id_as_the_display_name = () => {
  const data = {
    results: [{
      race_id:"20260705-hanshin-11", finish:[1,2,3], dividends:{},
      cards:[{char:"kei",hit:false,spent:500,payout:0,bets:[]}],
    }],
  };
  const stats = adapter.toStats(data, {races:[]});
  assert.strictEqual(stats.history[0].name, "阪神11R");
};

tests.test_bets_stay_unsettled_when_the_race_has_no_result = () => {
  // 結果がまだ無いレースで「的中！」を出さない（won は null のまま）。
  const races = adapter.toRaces(predictions, comments, { results: [] });
  races.forEach((race) => {
    race.cards.forEach((card) => {
      assert.strictEqual(card.settled, null);
      card.bets.forEach((b) => {
        assert.strictEqual(b.won, null);
        assert.strictEqual(b.payout, null);
      });
    });
  });
};

tests.test_bet_key_matches_regardless_of_horse_order = () => {
  // 予想側と結果側で馬番の並びが違っても同じ1点として結びつくこと。
  assert.strictEqual(
    adapter.betKey({ type: "3連複", horses: [14, 3, 12] }),
    adapter.betKey({ type: "3連複", horses: [3, 12, 14] }),
  );
  assert.notStrictEqual(
    adapter.betKey({ type: "ワイド", horses: [3, 12] }),
    adapter.betKey({ type: "馬連", horses: [3, 12] }),
  );
};

tests.test_manual_and_auto_chappy_are_distinguishable = () => {
  // UIの「本線モデル / 検証モデル」ラベルはこのフラグで出し分ける。
  const doctored = JSON.parse(JSON.stringify(predictions));
  doctored.races[0].cards = [
    { char: "chappy", source: "manual_chat", model_role: "manual_chat",
      total: 1000, bets: [], hit_pct: null, payout_range: null },
    { char: "chappy", source: "signal_engine", model_role: "champion",
      total: 1000, bets: [], hit_pct: 28, payout_range: [1200, 48000] },
  ];
  const cards = adapter.toRaces(doctored, comments, { results: [] })[0].cards;
  assert.strictEqual(cards[0].manual, true);
  assert.strictEqual(cards[1].manual, false);
};

tests.test_pass_card_is_not_counted_as_a_miss = () => {
  // race-regime challenger の見送りカードは0円。的中率の分母や「不的中」に入れない。
  const doctored = {
    results: [{
      race_id: "r-pass", finish: [1, 2, 3], dividends: {},
      cards: [
        { char: "kei", hit: true, spent: 500, payout: 900, bets: [] },
        { char: "gen", action: "pass", hit: false, spent: 0, payout: 0, bets: [] },
      ],
    }],
  };
  const stats = adapter.toStats(doctored, predictions);
  const gen = stats.chars.find((c) => c.id === "gen");
  assert.strictEqual(gen.rec, "0/0的中・見送り1");
  assert.strictEqual(gen.hit, 0);
  assert.strictEqual(stats.bought, "500円");
  assert.strictEqual(stats.history[0].hit, true);
};

tests.test_manual_chappy_appears_in_character_stats_but_not_overall = () => {
  // 9/22のチャッピーはChatGPT会話で作った本線(manual_chat)。予想師別には出すが累計収支には混ぜない。
  const data = {
    results: [
      { race_id: "auto", finish: [1, 2, 3], dividends: {},
        cards: [{ char: "kei", hit: false, spent: 500, payout: 0, bets: [] }] },
      { race_id: "20260922-nakayama-11", evaluation_scope: "manual_chat",
        finish: [1, 2, 3], dividends: {},
        cards: [{ char: "chappy", source: "manual_chat", hit: true,
                  spent: 1000, payout: 11280, bets: [] }] },
      { race_id: "20260921-hanshin-11", evaluation_scope: "manual_chat",
        finish: [1, 2, 3], dividends: {},
        cards: [{ char: "chatgpt", hit: true, spent: 500, payout: 1150, bets: [] }] },
    ],
  };
  const stats = adapter.toStats(data, predictions);
  const chappy = stats.chars.find((c) => c.id === "chappy");
  assert.ok(chappy, "チャッピーの行がある");
  assert.strictEqual(chappy.rec, "1/1的中");
  assert.strictEqual(chappy.roi, 1128);
  assert.strictEqual(chappy.stake, "1,000円");
  assert.strictEqual(chappy.manual, 1);
  assert.strictEqual(stats.chars.find((c) => c.id === "kei").stake, "500円");
  assert.ok(!stats.chars.find((c) => c.id === "chatgpt"));
  assert.strictEqual(stats.bought, "500円");      // 累計はautoのケイだけ
  assert.strictEqual(stats.races, 1);
};

let passed = 0;
for (const [name, fn] of Object.entries(tests)) {
  fn();
  console.log(name + ": OK");
  passed += 1;
}
console.log(`\nすべてのテストが通りました（${passed}件）。`);
