"""前日版の決めごとと、式の保存・読み込み。

前日版：前の晩には分からない材料（当日の馬体重、その日の前のレースの傾向、その日の騎手の成績）を式から外す。
        SAMEDAY=1 を付けると外さない（旧来の当日版）。
FAST=1：式を作り直さず、保存してある式（models/）で見立てだけ出す。学習年は2011–2022で固定なので、
        週ごとに作り直しても同じ式になる。作り直すのは材料や作りを変えたときだけ（FAST を付けずに回す）。
"""
import json
import os

MODELS = '/home/claude/keiba_lab/models'
os.makedirs(MODELS, exist_ok=True)
FAST = os.environ.get('FAST') == '1'
SAMEDAY = os.environ.get('SAMEDAY') == '1'
_PRE = ['horse_weight', 'weight_change', 'horse_weight_rk', 'day_style_adv', 'day_gate_adv', 'day_n',
        'jky_today_n', 'jky_today_edge', 'bw_best_gap', 'bw_best_gap_abs', 'bw_win_gap', 'bw_vs_mean', 'bw_max_gap',
        'bw_trend3', 'bw_rel_field']
PRE = set() if SAMEDAY else set(_PRE) | {c + '_rel' for c in _PRE} | {c + '_rk' for c in _PRE}


def keep(feats):
    return [c for c in feats if c not in PRE]


def save_lgb(b, name, feats):
    b.save_model(f'{MODELS}/{name}.txt')
    json.dump(list(feats), open(f'{MODELS}/{name}.feats.json', 'w'))


def load_lgb(name, feats):
    import lightgbm as lgb
    saved = json.load(open(f'{MODELS}/{name}.feats.json'))
    assert saved == list(feats), f'{name}: 保存した式と材料の並びが違う（作り直しが必要）'
    return lgb.Booster(model_file=f'{MODELS}/{name}.txt')
