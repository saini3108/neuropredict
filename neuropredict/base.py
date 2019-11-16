from __future__ import print_function

import argparse
import os
import sys
import textwrap
import traceback
import warnings
import random
import numpy as np
from os.path import join as pjoin, exists as pexists
from warnings import catch_warnings, filterwarnings, simplefilter
from multiprocessing import Pool, Manager
from functools import partial
from os.path import abspath, exists as pexists
from abc import abstractmethod
from neuropredict import config_neuropredict as cfg
from neuropredict import __version__
from neuropredict.utils import not_unspecified, check_paths, impute_missing_data
from neuropredict.algorithms import make_pipeline
from neuropredict.results import ClassifyCVResults, RegressCVResults
from sklearn.model_selection import GridSearchCV, ShuffleSplit
from sklearn.base import is_classifier

class BaseWorkflow(object):
    """Class defining a structure for the neuropredict workflow"""


    def __init__(self,
                 datasets,
                 pred_model=cfg.default_classifier,
                 impute_strategy=cfg.default_imputation_strategy,
                 dim_red_method=cfg.default_feat_select_method,
                 reduced_dim=cfg.default_num_features_to_select,
                 train_perc=cfg.default_train_perc,
                 num_rep_cv=cfg.default_num_repetitions,
                 scoring=cfg.default_scoring_metric,
                 grid_search_level=cfg.GRIDSEARCH_LEVEL_DEFAULT,
                 out_dir=None,
                 num_procs=cfg.DEFAULT_NUM_PROCS,
                 user_options=None,
                 checkpointing=False
                 ):
        """Constructor"""

        self.datasets = datasets
        self.pred_model = pred_model
        self.impute_strategy = impute_strategy
        self.dim_red_method = dim_red_method
        self.reduced_dim = reduced_dim
        self.train_perc = train_perc
        self.num_rep_cv = num_rep_cv
        self._scoring = scoring
        self.grid_search_level = grid_search_level
        self.out_dir = out_dir
        self.num_procs = num_procs
        self.user_options = user_options
        self._checkpointing = checkpointing

        if is_classifier(self.pred_model):
            self.results = ClassifyCVResults(self.pred_model, self._scoring)
        else:
            self.results = RegressCVResults(self.pred_model, self._scoring)


    def _prepare(self):
        """Checks in inputs, parameters and their combinations"""

        if self.train_perc <= 0.0 or self.train_perc >= 1.0:
            raise ValueError('Train perc > 0.0 and < 1.0')

        self._id_list = list(self.datasets.samplet_ids)
        self._num_samples = len(self._id_list)
        self._train_set_size = np.int64(np.floor(self._num_samples * self.train_perc))
        self._train_set_size = max(1, min(self._num_samples, self._train_set_size))


    def run(self):
        """Full run of workflow"""

        self._prepare()
        self._run_cv()
        self.save()
        self.summarize()


    def _run_cv(self):
        """Actual CV"""

        if self.num_procs > 1:
            raise NotImplementedError('parallel runs not implemented yet!'
                                      'Use num_procs=1 for now')
            print('Parallelizing the repetitions of CV with {} processes ...'
                  ''.format(self.num_procs))
            with Manager() as proxy_manager:
                # TODO these inputs may not need to be shared, as the method is a
                #  class member and has direct access to the inputs passed here
                shared_inputs = proxy_manager.list(
                        [self.datasets, self.impute_strategy, self.reduced_dim,
                         self.train_perc, self.user_options.out_dir,
                         self.grid_search_level, self.pred_model,
                         self.dim_red_method])
                partial_func_holdout = partial(self._single_run_cv, *shared_inputs)

                with Pool(processes=self.num_procs) as pool:
                    cv_results = pool.map(partial_func_holdout,
                                          range(self.num_rep_cv))
        else:
            # switching to regular sequential for loop to avoid any parallel drama
            for rep in range(self.num_rep_cv):
                self._single_run_cv(rep)


    def _single_run_cv(self, run_id=None):
        """Implements a single run of train, optimize and predict"""

        random.shuffle(self._id_list)
        train_set = self._id_list[:self._train_set_size]
        test_set = list(set(self._id_list) - set(train_set))

        for ds_id, (train_data, train_targets), (test_data, test_targets) \
                in self.datasets.get_subsets((train_set, test_set)):
            print('Dataset {}'.format(ds_id))

            missing = self.datasets.get_attr(ds_id, cfg.missing_data_flag_name)
            if missing:
                train_data, test_data = impute_missing_data(
                        train_data, train_targets, self.impute_strategy, test_data)

            best_pipeline, best_params, feat_importance = \
                self._optimize_pipeline_on_train_set(train_data, train_targets)

            self.results.add_attr(run_id, ds_id, 'feat_importance', feat_importance)

            self._eval_predictions(best_pipeline, test_data, test_targets,
                                   run_id, ds_id)

        # dump results if self._checkpointing = True


    def _optimize_pipeline_on_train_set(self, train_data, train_targets):
        """Optimize model on training set and return predictions on test set."""

        pipeline, param_grid = make_pipeline(pred_model=self.pred_model,
                                             dim_red_method=self.dim_red_method,
                                             reduced_dim=self.reduced_dim,
                                             train_set_size=self._train_set_size,
                                             gs_level=self.grid_search_level)

        best_pipeline, best_params = self._optimize_pipeline(
                pipeline, train_data, train_targets, param_grid, self.train_perc)

        feat_importance = self._get_feature_importance(
                self.pred_model, best_pipeline, train_data.shape[1])

        return best_pipeline, best_params, feat_importance


    @staticmethod
    def _get_feature_importance(est_name, pipeline, num_features, fill_value=np.nan):
        "Extracts the feature importance of input features, if available."

        # assuming order in pipeline construction :
        #   - step 0 : preprocessign (robust scaling)
        #   - step 1 : feature selector / dim reducer
        dim_red = pipeline.steps[1]
        est = pipeline.steps[-1]  # the final step in an sklearn pipeline
                                  #   is always an estimator/classifier

        feat_importance = None
        if hasattr(dim_red, 'get_support'):  # nonlinear dim red won't have this
            index_selected_features = dim_red.get_support(indices=True)

            if hasattr(est, cfg.importance_attr[est_name]):
                feat_importance = np.full(num_features, fill_value)
                feat_importance[index_selected_features] = \
                    getattr(est, cfg.importance_attr[est_name])

        return feat_importance


    def _optimize_pipeline(self, pipeline, train_data, train_targets,
                           param_grid, train_perc_inner_cv):
        """Optimizes a given pipeline on the given dataset"""

        # TODO perhaps k-fold is a better inner CV,
        #   which guarantees full use of training set with fewer repeats?
        inner_cv = ShuffleSplit(n_splits=cfg.INNER_CV_NUM_SPLITS,
                                train_size=train_perc_inner_cv,
                                test_size=1.0 - train_perc_inner_cv)
        # inner_cv = RepeatedKFold(n_splits=cfg.INNER_CV_NUM_FOLDS,
        #   n_repeats=cfg.INNER_CV_NUM_REPEATS)

        # gs = GridSearchCV(estimator=pipeline, param_grid=param_grid, cv=inner_cv,
        #                   n_jobs=cfg.GRIDSEARCH_NUM_JOBS,
        #                   pre_dispatch=cfg.GRIDSEARCH_PRE_DISPATCH)

        # not specifying n_jobs to avoid any kind of parallelism (joblib) from within
        # sklearn to avoid potentially bad interactions with outer parallelization
        # with builtin multiprocessing library
        gs = GridSearchCV(estimator=pipeline,
                          param_grid=param_grid,
                          cv=inner_cv,
                          scoring=self._scoring,
                          refit=cfg.refit_best_model_on_ALL_training_set)

        # ignoring some not-so-critical warnings
        with catch_warnings():
            filterwarnings(action='once', category=UserWarning, module='joblib',
                           message='Multiprocessing-backed parallel loops cannot be '
                                   'nested, setting n_jobs=1')
            filterwarnings(action='once', category=UserWarning,
                           message='Some inputs do not have OOB scores')
            np.seterr(divide='ignore', invalid='ignore')
            filterwarnings(action='once', category=RuntimeWarning,
                           message='invalid value encountered in true_divide')
            simplefilter(action='once', category=DeprecationWarning)

            gs.fit(train_data, train_targets)

        return gs.best_estimator_, gs.best_params_


    @abstractmethod
    def _eval_predictions(self, pipeline, test_data, true_targets, run_id, ds_id):
        """
        Evaluate predictions and perf estimates to results class.
        Prints a quick summary too, as an indication of progress.

        Making it abstract to let the child classes decide on what types of
        predictions to make (probabilistic or not), and how to evaluate them
        """


    @abstractmethod
    def save(self):
        """Saves the results and state to disk."""


    @abstractmethod
    def load(self):
        """Mechanism to reload results.

        Useful for check-pointing, and restore upon crash etc
        """


    @abstractmethod
    def summarize(self):
        """Simple summary of the results produced, for logging and user info"""


    @abstractmethod
    def visualize(self):
        """Method to produce all the relevant visualizations based on the results
        from this workflow."""


def get_parser_base():
    "Parser to specify arguments and their defaults."

    help_text_pyradigm_paths = textwrap.dedent("""
    Path(s) to pyradigm datasets.

    Each path is self-contained dataset identifying each sample, its class and 
    features.
    \n \n """)

    help_text_user_defined_folder = textwrap.dedent("""
        List of absolute paths to user's own features.

        Format: Each of these folders contains a separate folder for each subject 
        ( named after its ID in the metadata file) containing a file called 
        features.txt with one number per line. All the subjects (in a given 
        folder) must have the number of features ( #lines in file). Different 
        parent folders (describing one feature set) can have different number of 
        features for each subject, but they must all have the same number of 
        subjects (folders) within them.

        Names of each folder is used to annotate the results in visualizations. 
        Hence name them uniquely and meaningfully, keeping in mind these figures 
        will be included in your papers. For example,

        .. parsed-literal::

            --user_feature_paths /project/fmri/ /project/dti/ /project/t1_volumes/

        Only one of ``--pyradigm_paths``, ``user_feature_paths``, 
        ``data_matrix_path`` 
        or ``arff_paths`` options can be specified.
        \n \n """)

    help_text_data_matrix = textwrap.dedent("""
    List of absolute paths to text files containing one matrix of size N x p  (
    num_samples x num_features).

    Each row in the data matrix file must represent data corresponding to sample 
    in the same row of the meta data file (meta data file and data matrix must be 
    in row-wise correspondence).

    Name of this file will be used to annotate the results and visualizations.

    E.g. ``--data_matrix_paths /project/fmri.csv /project/dti.csv 
    /project/t1_volumes.csv ``

    Only one of ``--pyradigm_paths``, ``user_feature_paths``, ``data_matrix_path`` 
    or ``arff_paths`` options can be specified.

    File format could be
     - a simple comma-separated text file (with extension .csv or .txt): which can 
     easily be read back with
        numpy.loadtxt(filepath, delimiter=',')
        or
     - a numpy array saved to disk (with extension .npy or .numpy) that can read 
     in with numpy.load(filepath).

     One could use ``numpy.savetxt(data_array, delimiter=',')`` or ``numpy.save(
     data_array)`` to save features.

     File format is inferred from its extension.
     \n \n """)

    help_text_train_perc = textwrap.dedent("""
    Percentage of the smallest class to be reserved for training.

    Must be in the interval [0.01 0.99].

    If sample size is sufficiently big, we recommend 0.5.
    If sample size is small, or class imbalance is high, choose 0.8.
    \n \n """)

    help_text_num_rep_cv = textwrap.dedent("""
    Number of repetitions of the repeated-holdout cross-validation.

    The larger the number, more stable the estimates will be.
    \n \n """)

    help_text_metadata_file = textwrap.dedent("""
    Abs path to file containing metadata for subjects to be included for analysis.

    At the minimum, each subject should have an id per row followed by the class 
    it belongs to.

    E.g.
    .. parsed-literal::

        sub001,control
        sub002,control
        sub003,disease
        sub004,disease

    \n \n """)

    help_text_dimensionality_red_size = textwrap.dedent("""
    Number of features to select as part of feature selection. Options:

         - 'tenth'
         - 'sqrt'
         - 'log2'
         - 'all'

    Default: \'tenth\' of the number of samples in the training set.

    For example, if your dataset has 90 samples, you chose 50 percent for training 
    (default), then Y will have 90*.5=45 samples in training set, leading to 5 
    features to be selected for taining. If you choose a fixed integer, ensure all 
    the feature sets under evaluation have atleast that many features.
    \n \n """)

    help_text_gs_level = textwrap.dedent("""
    Flag to specify the level of grid search during hyper-parameter optimization 
    on the training set.
    
    Allowed options are : 'none', 'light' and 'exhaustive', in the order of how 
    many values/values will be optimized. More parameters and more values demand 
    more resources and much longer time for optimization.

    The 'light' option tries to "folk wisdom" to try least number of values (no 
    more than one or two), for the parameters for the given classifier. (e.g. a 
    lage number say 500 trees for a random forest optimization). The 'light' will 
    be the fastest and should give a "rough idea" of predictive performance. The 
    'exhaustive' option will try to most parameter values for the most parameters 
    that can be optimized.
    """)

    help_text_make_vis = textwrap.dedent("""
    Option to make visualizations from existing results in the given path. 
    This is helpful when neuropredict failed to generate result figures 
    automatically 
    e.g. on a HPC cluster, or another environment when DISPLAY is either not 
    available.

    """)

    help_text_num_cpus = textwrap.dedent("""
    Number of CPUs to use to parallelize CV repetitions.

    Default : 4.

    Number of CPUs will be capped at the number available on the machine if higher 
    is requested.
    \n \n """)

    help_text_out_dir = textwrap.dedent("""
    Output folder to store gathered features & results.
    \n \n """)

    help_dim_red_method = textwrap.dedent("""
    Feature selection, or dimensionality reduction method to apply prior to 
    training the classifier.

    **NOTE**: when feature 'selection' methods are used, we are able to keep track 
    of which features in the original input space were slected and hence visualize 
    their feature importance after the repetitions of CV. When the more generic 
    'dimensionality reduction' methods are used, features often get transformed to 
    new subspaces, wherein the link to original features is lost, and hence 
    importance values for original input features can not be computed and hence 
    are not visualized.

    Default: 'VarianceThreshold', removing features with 0.001 percent of lowest 
    variance (zeros etc).

    """)

    help_imputation_strategy = textwrap.dedent("""
    Strategy to impute any missing data (as encoded by NaNs).

    Default: 'raise', which raises an error if there is any missing data anywhere.
    Currently available imputation strategies are: {}

    """.format(cfg.avail_imputation_strategies))

    help_text_print_options = textwrap.dedent("""
    Prints the options used in the run in an output folder.

    """)

    parser = argparse.ArgumentParser(prog="neuropredict",
                                     formatter_class=argparse.RawTextHelpFormatter,
                                     description='Easy, standardized and '
                                                 'comprehensive predictive '
                                                 'analysis.')

    parser.add_argument("-m", "--meta_file", action="store", dest="meta_file",
                        default=None, required=False, help=help_text_metadata_file)

    parser.add_argument("-o", "--out_dir", action="store", dest="out_dir",
                        required=False, help=help_text_out_dir,
                        default=None)

    user_feat_args = parser.add_argument_group(title='Input data and formats',
                                               description='Only one of the '
                                                           'following types can be '
                                                           'specified.')

    user_feat_args.add_argument("-y", "--pyradigm_paths", action="store",
                                dest="pyradigm_paths",
                                nargs='+',  # to allow for multiple features
                                default=None,
                                help=help_text_pyradigm_paths)

    user_feat_args.add_argument("-u", "--user_feature_paths", action="store",
                                dest="user_feature_paths",
                                nargs='+',  # to allow for multiple features
                                default=None,
                                help=help_text_user_defined_folder)

    user_feat_args.add_argument("-d", "--data_matrix_paths", action="store",
                                dest="data_matrix_paths",
                                nargs='+',
                                default=None,
                                help=help_text_data_matrix)

    cv_args = parser.add_argument_group(title='Cross-validation',
                                        description='Parameters related to '
                                                    'training and '
                                                    'optimization during '
                                                    'cross-validation')

    cv_args.add_argument("-t", "--train_perc", action="store",
                         dest="train_perc",
                         default=cfg.default_train_perc,
                         help=help_text_train_perc)

    cv_args.add_argument("-n", "--num_rep_cv", action="store",
                         dest="num_rep_cv",
                         default=cfg.default_num_repetitions,
                         help=help_text_num_rep_cv)

    cv_args.add_argument("-k", "--reduced_dim_size",
                         dest="reduced_dim_size",
                         action="store",
                         default=cfg.default_reduced_dim_size,
                         help=help_text_dimensionality_red_size)

    cv_args.add_argument("-g", "--gs_level", action="store", dest="gs_level",
                         default="light", help=help_text_gs_level,
                         choices=cfg.GRIDSEARCH_LEVELS, type=str.lower)

    pipeline_args = parser.add_argument_group(
            title='Predictive Model',
            description='Parameters of pipeline comprising the predictive model')

    pipeline_args.add_argument("-is", "--impute_strategy", action="store",
                               dest="impute_strategy",
                               default=cfg.default_imputation_strategy,
                               help=help_imputation_strategy,
                               choices=cfg.avail_imputation_strategies_with_raise,
                               type=str.lower)

    pipeline_args.add_argument("-dr", "--dim_red_method", action="store",
                               dest="dim_red_method",
                               default=cfg.default_dim_red_method,
                               help=help_dim_red_method,
                               choices=cfg.all_dim_red_methods,
                               type=str.lower)

    vis_args = parser.add_argument_group(
            title='Visualization',
            description='Parameters related to generating visualizations')

    vis_args.add_argument("-z", "--make_vis", action="store", dest="make_vis",
                          default=None, help=help_text_make_vis)

    comp_args = parser.add_argument_group(
            title='Computing',
            description='Parameters related to computations/debugging')

    comp_args.add_argument("-c", "--num_procs", action="store", dest="num_procs",
                           default=cfg.DEFAULT_NUM_PROCS, help=help_text_num_cpus)

    comp_args.add_argument("--po", "--print_options", action="store",
                           dest="print_opt_dir",
                           default=False, help=help_text_print_options)

    comp_args.add_argument('-v', '--version', action='version',
                           version='%(prog)s {version}'.format(version=__version__))

    return parser, user_feat_args, cv_args, pipeline_args, vis_args, comp_args


def organize_inputs(user_args):
    """
    Validates the input features specified and
    returns organized list of paths and readers.

    Parameters
    ----------
    user_args : ArgParse object
        Various options specified by the user.

    Returns
    -------
    user_feature_paths : list
        List of paths to specified input features
    user_feature_type : str
        String identifying the type of user-defined input
    fs_subject_dir : str
        Path to freesurfer subject directory, if supplied.

    """

    atleast_one_feature_specified = False
    # specifying pyradigm avoids the need for separate meta data file
    meta_data_supplied = False
    meta_data_format = None

    if hasattr(user_args, 'fs_subject_dir') and \
        not_unspecified(user_args.fs_subject_dir):
        fs_subject_dir = abspath(user_args.fs_subject_dir)
        if not pexists(fs_subject_dir):
            raise IOError("Given Freesurfer directory doesn't exist.")
        atleast_one_feature_specified = True
    else:
        fs_subject_dir = None

    # ensuring only one type is specified
    mutually_excl_formats = ['user_feature_paths',
                             'data_matrix_paths',
                             'pyradigm_paths',
                             'arff_paths']
    not_none_count = 0
    for format in mutually_excl_formats:
        if  hasattr(user_args, format) and \
                not_unspecified(getattr(user_args, format)):
            not_none_count = not_none_count + 1
    if not_none_count > 1:
        raise ValueError('Only one of the following formats can be specified:\n'
                         '{}'.format(mutually_excl_formats))

    if hasattr(user_args, 'user_feature_paths') and \
        not_unspecified(user_args.user_feature_paths):
        user_feature_paths = check_paths(user_args.user_feature_paths,
                                         path_type='user defined (dir_of_dirs)')
        atleast_one_feature_specified = True
        user_feature_type = 'dir_of_dirs'

    elif hasattr(user_args, 'data_matrix_paths') and \
        not_unspecified(user_args.data_matrix_paths):
        user_feature_paths = check_paths(user_args.data_matrix_paths,
                                         path_type='data matrix')
        atleast_one_feature_specified = True
        user_feature_type = 'data_matrix'

    elif hasattr(user_args, 'pyradigm_paths') and \
        not_unspecified(user_args.pyradigm_paths):
        user_feature_paths = check_paths(user_args.pyradigm_paths,
                                         path_type='pyradigm')
        atleast_one_feature_specified = True
        meta_data_supplied = user_feature_paths[0]
        meta_data_format = 'pyradigm'
        user_feature_type = 'pyradigm'

    elif hasattr(user_args, 'arff_paths') and \
        not_unspecified(user_args.arff_paths):
        user_feature_paths = check_paths(user_args.arff_paths, path_type='ARFF')
        atleast_one_feature_specified = True
        user_feature_type = 'arff'
        meta_data_supplied = user_feature_paths[0]
        meta_data_format = 'arff'
    else:
        user_feature_paths = None
        user_feature_type = None

    # map in python 3 returns a generator, not a list, so len() wouldnt work
    if not isinstance(user_feature_paths, list):
        user_feature_paths = list(user_feature_paths)

    if not atleast_one_feature_specified:
        raise ValueError('Atleast one method specifying features must be specified. '
                         'It can be a path(s) to pyradigm dataset, matrix file, '
                         'user-defined folder or a Freesurfer subject directory.')

    return user_feature_paths, user_feature_type, fs_subject_dir, \
           meta_data_supplied, meta_data_format


class NeuroPredictException(Exception):
    """Custom exception to distinguish neuropredict related errors (usage etc)
    from the usual."""
    pass


class MissingDataException(NeuroPredictException):
    """Custom exception to uniquely identify this error. Helpful for testing etc"""
    pass
