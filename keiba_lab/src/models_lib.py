"""Shared model-training utilities (conditional-logit GBM with per-race softmax objective)."""
import lightgbm as lgb
import numpy as np
import pandas as pd

from common import D, race_index, group_softmax, race_logloss

PARAMS_FUND = dict(learning_rate=0.03, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
                   bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1)
PARAMS_RES = dict(learning_rate=0.02, num_leaves=15, min_data_in_leaf=2000, feature_fraction=0.6,
                  bagging_fraction=0.8, bagging_freq=1, lambda_l2=50.0, max_bin=63, num_threads=2, seed=1)

DROP = {'race_id', 'horse_number', 'date', 'year', 'start_min', 'venue_name', 'surf', 'sex', 'jockey_id', 'trainer_id',
        'finish', 'completed', 'odds', 'popularity', 'mkt_p', 'dead_heat_win', 'horse_id', 'y', 'log_pi', 'pi_rank'}


def load_features(path=f'{D}/features.parquet', min_year=2011):
    F = pd.read_parquet(path)
    F = F[~F.dead_heat_win & (F.year >= min_year)].copy()
    F = F.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
    F['log_pi'] = np.log(F.mkt_p)
    F['pi_rank'] = F.groupby('race_id').mkt_p.rank(ascending=False, pct=True)
    for c in ('handicap', 'female_only', 'listed'):
        F[c] = F[c].astype(int)
    fund = [c for c in F.columns if c not in DROP and F[c].dtype != object]
    return F, fund


CTX = ['field', 'cls', 'surf_code', 'distance', 'cond', 'venue_code', 'course_code', 'month', 'race_num', 'handicap',
       'female_only', 'listed', 'age', 'sex_code', 'gate_pct', 'gate_number', 'impost', 'impost_rel', 'impost_rk']
GROUPS = {
    'MKT': ['log_pi', 'pi_rank'],
    'CTX': CTX,
    'RATING': ['rating', 'rating_rel', 'rating_rk', 'rating_sd', 'rating_fill', 'rating_other', 'n_runs', 'n_surf'],
    'FORM': ['days_off', 'last_finish_pct', 'last_y', 'last_resid', 'dist_change', 'surf_change', 'cls_change',
             'jockey_same', 'impost_change', 'last_cond', 'last_venue_same', 'y_mean5', 'y_best5', 'resid_mean5',
             'fin_pct_mean5', 'career_win_rate', 'career_top3_rate', 'prize_per_run', 'max_cls', 'y_mean5_rel',
             'y_mean5_rk', 'resid_mean5_rel', 'resid_mean5_rk', 'prize_per_run_rel', 'prize_per_run_rk',
             'career_top3_rate_rel', 'career_top3_rate_rk'],
    'PASTMKT': ['last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
                'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk'],
    'CONN': [p + s for p in ('jky_', 'trn_', 'own_', 'jky90_') for s in ('n', 'win', 'top3', 'mratio')]
            + ['jky_win_rel', 'jky_win_rk', 'trn_win_rel', 'trn_win_rk'],
    'BODY': ['horse_weight', 'weight_change', 'horse_weight_rel', 'horse_weight_rk'],
    'PACE': ['last_style', 'last_kick', 'style_mean5', 'style_min5', 'kick_mean5', 'kick_mean5_rel', 'kick_mean5_rk',
             'style_mean5_rel', 'style_mean5_rk', 'front_runners', 'day_style_adv', 'day_gate_adv', 'day_n'],
    'APT': ['apt_dist', 'n_dist', 'apt_dist_gap', 'apt_venue', 'apt_wet', 'best_dist_fin', 'apt_dist_rel', 'apt_dist_rk'],
}


def make_obj(offset, starts, inv):
    def obj(preds, data):
        y = data.get_label()
        p = group_softmax(preds + offset, starts, inv)
        return p - y, np.maximum(p * (1 - p), 1e-6)
    return obj


def train_gbm(Xtr, ytr, rtr, offtr, Xva, yva, rva, offva, params, rounds=3000, es=150):
    s_tr, i_tr = race_index(rtr)
    s_va, i_va = race_index(rva)
    dtr = lgb.Dataset(Xtr, label=ytr, free_raw_data=False)
    dva = lgb.Dataset(Xva, label=yva, reference=dtr, free_raw_data=False)
    p = dict(params)
    p['objective'] = make_obj(offtr, s_tr, i_tr)
    p['verbosity'] = -1

    def feval(preds, data):
        pr = group_softmax(preds + offva, s_va, i_va)
        return 'race_ll', float(race_logloss(pr, data.get_label(), s_va).mean()), False

    return lgb.train(p, dtr, num_boost_round=rounds, valid_sets=[dva], feval=feval,
                     callbacks=[lgb.early_stopping(es, verbose=False)])
