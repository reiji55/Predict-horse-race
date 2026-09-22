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
  assert.deepStrictEqual(card.bets[0], { type: "ワイド", h: [[3, 4], [6, 9]], amt: 200 });
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

let passed = 0;
for (const [name, fn] of Object.entries(tests)) {
  fn();
  console.log(name + ": OK");
  passed += 1;
}
console.log(`\nすべてのテストが通りました（${passed}件）。`);
