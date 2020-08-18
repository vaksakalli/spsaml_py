# spFtSel: Feature Selection and Ranking via SPSA
# V. Aksakalli & Z. D. Yenice
# GPL-3.0, 2020
# Please refer to below for more information:
# https://arxiv.org/abs/1804.05589

import logging
import numpy as np
from sklearn.model_selection import KFold, RepeatedKFold, StratifiedKFold, RepeatedStratifiedKFold, cross_val_score
from sklearn.utils import shuffle
from joblib import parallel_backend
import time


class SpFtSelLog:
    # if is_debug is set to True, DEBUG information will also be printed
    # so, change the following line as needed:
    is_debug = False
    # create logger and set level
    logger = logging.getLogger('spFtSel')
    logger.setLevel(logging.INFO)
    # create console handler and set level
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ###
    if is_debug:
        logger.setLevel(logging.DEBUG)
        ch.setLevel(logging.DEBUG)
    ###
    # create formatter
    formatter = logging.Formatter(fmt='{name}-{levelname}: {message}', style='{')
    # add formatter to console handler
    ch.setFormatter(formatter)
    # add console handler to logger
    logger.addHandler(ch)

#######################################


class SpFtSelKernel:

    def __init__(self, params):
        """
        algorithm parameters initialization
        """
        self._perturb_amount = 0.05
        #####
        self._gain_min = 0.01
        self._gain_max = 1.0
        #####
        self._change_min = 0.0
        self._change_max = 0.2
        #####
        self._stall_tolerance = 1e-5
        self._bb_bottom_threshold = 1e-5
        self._decimals = 5  # for display rounding, must be > 3
        #####
        self._gain_type = params['gain_type']
        self._features_to_keep_indices = params['features_to_keep_indices']
        self._iter_max = params['iter_max']
        self._stall_limit = params['stall_limit']
        self._same_count_max = params['stall_limit']
        self._num_grad_avg = params['num_grad_avg']
        self._num_gain_smoothing = params['num_gain_smoothing']
        self._stratified_cv = params['stratified_cv']
        self._num_cv_reps_grad = params['cv_reps_grad']
        self._num_cv_reps_eval = params['cv_reps_eval']
        self._num_cv_folds = params['cv_folds']
        self._n_jobs = params['n_jobs']
        self._num_features_selected = params['num_features']
        self._starting_imps = params.get('starting_imps')
        self._print_freq = params.get('print_freq')
        #####
        self._mon_gain_A = params.get('mon_gain_A') if params.get('mon_gain_A') else 100
        self._mon_gain_a = params.get('mon_gain_a') if params.get('mon_gain_a') else 0.75
        self._mon_gain_alpha = params.get('mon_gain_alpha') if params.get('mon_gain_alpha') else 0.6
        #####
        self._input_x = None
        self._output_y = None
        self._wrapper = None
        self._scoring = None
        self._curr_imp_prev = None
        self._imp = None
        self._ghat = None
        self._cv_feat_eval = None
        self._cv_grad_avg = None
        self._curr_imp = None
        self._p = None
        self._best_value = -1 * np.inf
        self._best_std = -1
        self._stall_counter = 1
        self._run_time = -1
        self._best_iter = -1
        self._gain = -1
        self._selected_features = list()
        self._selected_features_prev = list()
        self._best_features = list()
        self._best_imps = list()
        self._raw_gain_seq = list()
        self._iter_results = self.prepare_results_dict()

    def set_inputs(self, x, y, wrapper, scoring):
        self._input_x = x
        self._output_y = y
        self._wrapper = wrapper
        self._scoring = scoring

    def shuffle_data(self):
        if any([self._input_x is None, self._output_y is None]):
            raise ValueError('There is no data inside shuffle_data()')
        else:
            self._input_x, self._output_y = shuffle(self._input_x, self._output_y)

    @staticmethod
    def prepare_results_dict():
        iter_results = dict()
        iter_results['values'] = list()
        iter_results['st_devs'] = list()
        iter_results['gains'] = list()
        iter_results['gains_raw'] = list()
        iter_results['importance'] = list()
        iter_results['feature_indices'] = list()
        return iter_results

    def init_parameters(self):
        self._p = self._input_x.shape[1]
        if self._starting_imps:
            self._curr_imp = self._starting_imps
            SpFtSelLog.logger.info(f'Starting importance range: ({self._curr_imp.min()}, {self._curr_imp.max()})')
        else:
            self._curr_imp = np.repeat(0.0, self._p)
        self._ghat = np.repeat(0.0, self._p)
        self._curr_imp_prev = self._curr_imp

    def print_algo_info(self):
        SpFtSelLog.logger.info(f'Wrapper: {self._wrapper}')
        SpFtSelLog.logger.info(f'Scoring metric: {self._scoring}')
        SpFtSelLog.logger.info(f"Number of observations: {self._input_x.shape[0]}")
        SpFtSelLog.logger.info(f"Number of features available: {self._p}")
        SpFtSelLog.logger.info(f"Number of features to select: {self._num_features_selected}")

    def get_selected_features(self, imp):
        """
        given the importance array, determine which features to select (as indices)
        :param imp: importance array
        :return: indices of selected features
        """
        selected_features = imp.copy()  # init_parameters
        if self._features_to_keep_indices is not None:
            selected_features[self._features_to_keep_indices] = 1.0  # keep these for sure by setting their imp to 1

        if self._num_features_selected == 0:  # automated feature selection
            num_features_to_select = np.sum(selected_features >= 0.0)
            if num_features_to_select == 0:
                num_features_to_select = 1  # select at least one!
        else:  # user-supplied _num_features_selected
            if self._features_to_keep_indices is None:
                num_features_to_keep = 0
            else:
                num_features_to_keep = len(self._features_to_keep_indices)

            num_features_to_select = np.minimum(self._p, (num_features_to_keep + self._num_features_selected))

        return selected_features.argsort()[::-1][:num_features_to_select]

    def gen_cv_task(self):
        if self._stratified_cv:
            if self._num_cv_reps_grad > 1:
                self._cv_grad_avg = RepeatedStratifiedKFold(n_splits=self._num_cv_folds,
                                                            n_repeats=self._num_cv_reps_grad)
            else:
                self._cv_grad_avg = StratifiedKFold(n_splits=self._num_cv_folds)

            if self._num_cv_reps_eval > 1:
                self._cv_feat_eval = RepeatedStratifiedKFold(n_splits=self._num_cv_folds,
                                                             n_repeats=self._num_cv_reps_eval)
            else:
                self._cv_feat_eval = StratifiedKFold(n_splits=self._num_cv_folds)

        else:
            if self._num_cv_reps_grad > 1:
                self._cv_grad_avg = RepeatedKFold(n_splits=self._num_cv_folds, n_repeats=self._num_cv_reps_grad)
            else:
                self._cv_grad_avg = KFold(n_splits=self._num_cv_folds)

            if self._num_cv_reps_eval > 1:
                self._cv_feat_eval = RepeatedKFold(n_splits=self._num_cv_folds, n_repeats=self._num_cv_reps_eval)
            else:
                self._cv_feat_eval = KFold(n_splits=self._num_cv_folds)

    def eval_feature_set(self, cv_task, curr_imp):
        selected_features = self.get_selected_features(curr_imp)
        x_fs = self._input_x[:, selected_features]
        scores = cross_val_score(self._wrapper,
                                 x_fs,
                                 self._output_y,
                                 cv=cv_task,
                                 scoring=self._scoring,
                                 n_jobs=self._n_jobs)
        best_value_mean = scores.mean().round(self._decimals - 2)
        best_value_std = scores.std().round(self._decimals - 2)
        del scores
        # sklearn metrics convention is that higher is always better
        # and SPSA here maximizes the obj. function
        return [best_value_mean, best_value_std]

    def clip_change(self, raw_change):
        change_sign = np.where(raw_change > 0.0, +1, -1)
        change_abs_clipped = np.abs(raw_change).clip(min=self._change_min, max=self._change_max)
        change_clipped = change_sign * change_abs_clipped
        return change_clipped

    def run_kernel(self):
        start_time = time.time()

        curr_iter_no = -1
        while curr_iter_no < self._iter_max:
            curr_iter_no += 1

            g_matrix = np.array([]).reshape(0, self._p)

            # gradient averaging
            for g in range(self._num_grad_avg):
                delta = np.where(np.random.sample(self._p) >= 0.5, 1, -1)

                imp_plus = self._curr_imp + self._perturb_amount * delta
                imp_minus = self._curr_imp - self._perturb_amount * delta

                y_plus = self.eval_feature_set(self._cv_grad_avg, imp_plus)[0]
                y_minus = self.eval_feature_set(self._cv_grad_avg, imp_minus)[0]

                g_curr = (y_plus - y_minus) / (2 * self._perturb_amount * delta)

                g_matrix = np.vstack([g_matrix, g_curr])

            ghat_prev = self._ghat.copy()
            self._ghat = g_matrix.mean(axis=0)

            if np.count_nonzero(self._ghat) == 0:
                self._ghat = ghat_prev

            # gain calculation
            if self._gain_type == 'bb':
                if curr_iter_no == 0:
                    self._gain = self._gain_min
                    self._raw_gain_seq.append(self._gain)
                else:
                    imp_diff = self._curr_imp - self._curr_imp_prev
                    ghat_diff = self._ghat - ghat_prev
                    bb_bottom = -1 * np.sum(imp_diff * ghat_diff)  # -1 due to maximization in SPSA
                    # make sure we don't end up with division by zero
                    # or negative gains:
                    if bb_bottom < self._bb_bottom_threshold:
                        self._gain = self._gain_min
                    else:
                        self._gain = np.sum(imp_diff * imp_diff) / bb_bottom
                        self._gain = np.maximum(self._gain_min, (np.minimum(self._gain_max, self._gain)))
                    self._raw_gain_seq.append(self._gain)
                    if curr_iter_no >= self._num_gain_smoothing:
                        raw_gain_seq_recent = self._raw_gain_seq[-self._num_gain_smoothing:]
                        self._gain = np.mean(raw_gain_seq_recent)
            elif self._gain_type == 'mon':
                self._gain = self._mon_gain_a / ((curr_iter_no + self._mon_gain_A) ** self._mon_gain_alpha)
                self._raw_gain_seq.append(self._gain)
            else:
                raise ValueError('Error: unknown gain type')

            SpFtSelLog.logger.debug(f'iteration no = {curr_iter_no}')
            SpFtSelLog.logger.debug(f'iteration gain raw = {np.round(self._raw_gain_seq[-1], self._decimals)}')
            SpFtSelLog.logger.debug(f'iteration gain smooth = {np.round(self._gain, self._decimals)}')

            self._curr_imp_prev = self._curr_imp.copy()

            # make sure change is not too much
            curr_change_raw = self._gain * self._ghat
            SpFtSelLog.logger.debug(f"curr_change_raw = {np.round(curr_change_raw, self._decimals)}")
            curr_change_clipped = self.clip_change(curr_change_raw)
            SpFtSelLog.logger.debug(f"curr_change_clipped = {np.round(curr_change_clipped, self._decimals)}")

            # we use "+" below so that SPSA maximizes
            self._curr_imp = self._curr_imp + curr_change_clipped

            self._selected_features_prev = self.get_selected_features(self._curr_imp_prev)
            self._selected_features = self.get_selected_features(self._curr_imp)

            # make sure we move to a new solution
            same_feature_counter = 0
            curr_imp_orig = self._curr_imp.copy()
            same_feature_step_size = (self._gain_max - self._gain_min)/self._stall_limit
            while np.array_equal(self._selected_features_prev, self._selected_features):
                same_feature_counter = same_feature_counter + 1
                curr_step_size = (self._gain_min + same_feature_counter*same_feature_step_size)
                curr_change_raw = curr_step_size * self._ghat
                curr_change_clipped = self.clip_change(curr_change_raw)
                self._curr_imp = curr_imp_orig + curr_change_clipped
                self._selected_features = self.get_selected_features(self._curr_imp)
                if same_feature_counter >= self._same_count_max:
                    break

            if same_feature_counter > 1:
                SpFtSelLog.logger.debug(f"same_feature_counter = {same_feature_counter}")

            fs_perf_output = self.eval_feature_set(self._cv_feat_eval, self._curr_imp)

            self._iter_results['values'].append(np.round(fs_perf_output[0], self._decimals))
            self._iter_results['st_devs'].append(np.round(fs_perf_output[1], self._decimals))
            self._iter_results['gains'].append(np.round(self._gain, self._decimals))
            self._iter_results['gains_raw'].append(np.round(self._raw_gain_seq[-1], self._decimals))
            self._iter_results['importance'].append(np.round(self._curr_imp, self._decimals))
            self._iter_results['feature_indices'].append(self._selected_features)

            if self._iter_results['values'][curr_iter_no] >= self._best_value + self._stall_tolerance:
                self._stall_counter = 1
                self._best_iter = curr_iter_no
                self._best_value = self._iter_results['values'][curr_iter_no]
                self._best_std = self._iter_results['st_devs'][curr_iter_no]
                self._best_features = self._selected_features
                self._best_imps = self._curr_imp[self._best_features]
            else:
                self._stall_counter = self._stall_counter + 1

            if curr_iter_no % self._print_freq == 0:
                SpFtSelLog.logger.info(f"iter_no: {curr_iter_no}, "
                                       f"num_ft: {len(self._selected_features)}, "
                                       f"value: {self._iter_results['values'][curr_iter_no]}, "
                                       f"st_dev: {self._iter_results['st_devs'][curr_iter_no]}, "
                                       f"best: {self._best_value} @ iter_no {self._best_iter}")

            if self._stall_counter > self._stall_limit:
                # search stalled, start from scratch!
                SpFtSelLog.logger.info(f"iteration stall limit reached, initializing search...")
                self._stall_counter = 1  # reset the stall counter
                self.init_parameters()  # set _curr_imp and _g_hat to vectors of zeros

            if same_feature_counter >= self._same_count_max:
                # search stalled, start from scratch!
                SpFtSelLog.logger.info(f"same feature counter limit reached, initializing search...")
                self._stall_counter = 1  # reset the stall counter
                self.init_parameters()

        self._run_time = round((time.time() - start_time) / 60, 2)  # in minutes
        SpFtSelLog.logger.info(f"spFtSel completed in {self._run_time} minutes.")
        SpFtSelLog.logger.info(
            f"Best value = {np.round(self._best_value, self._decimals)} with " +
            f"{len(self._best_features )} features and {len(self._iter_results.get('values')) - 1} total iterations. ")

    def parse_results(self):
        selected_data = self._input_x[:, self._best_features]
        results_values = np.array(self._iter_results.get('values'))
        total_iter_for_opt = np.argmax(results_values)

        return {'wrapper': self._wrapper,
                'scoring': self._scoring,
                'selected_data': selected_data,
                'iter_results': self._iter_results,
                'features': self._best_features,
                'importance': self._best_imps,
                'num_features': len(self._best_features),
                'total_iter_overall': len(self._iter_results.get('values')),
                'total_iter_for_opt': total_iter_for_opt,
                'best_value': self._best_value,
                'best_std': self._best_std,
                'run_time': self._run_time,
                }


class SpFtSel:
    def __init__(self, x, y, wrapper, scoring='accuracy'):
        self._x = x
        self._y = y
        self._wrapper = wrapper
        self._scoring = scoring
        self.results = None

    def run(self,
            num_features=0,
            stratified_cv=True,  # *** MUST be set to False for regression problems ***
            n_jobs=1,
            print_freq=5,
            starting_imps=None,
            features_to_keep_indices=None):

        sp_params = dict()

        # define a dictionary to initialize the SpFtSel kernel
        # two gain types are available: bb (Barzilai & Borwein) (default) or mon (monotone)
        sp_params['gain_type'] = 'bb'
        ####
        sp_params['num_features'] = num_features
        sp_params['stratified_cv'] = stratified_cv
        sp_params['n_jobs'] = n_jobs
        sp_params['print_freq'] = print_freq
        sp_params['starting_imps'] = starting_imps
        sp_params['features_to_keep_indices'] = features_to_keep_indices

        ######################################
        # change below as needed:
        sp_params['cv_folds'] = 5
        sp_params['cv_reps_eval'] = 2
        sp_params['cv_reps_grad'] = 1
        sp_params['iter_max'] = 300
        sp_params['stall_limit'] = 100
        sp_params['num_grad_avg'] = 4
        sp_params['num_gain_smoothing'] = 1
        ######################################

        kernel = SpFtSelKernel(sp_params)

        kernel.set_inputs(x=self._x,
                          y=self._y,
                          wrapper=self._wrapper,
                          scoring=self._scoring)

        kernel.shuffle_data()
        kernel.init_parameters()
        kernel.print_algo_info()
        kernel.gen_cv_task()

        with parallel_backend('multiprocessing'):
            kernel.run_kernel()

        self.results = kernel.parse_results()

        return self
