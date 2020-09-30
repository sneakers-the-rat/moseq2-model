'''
Utility functions for handling loading and saving models and their respective metadata.
'''

import os
import h5py
import click
import joblib
import pickle
import scipy.io
import numpy as np
from copy import deepcopy
from cytoolz import first
from collections import OrderedDict
from os.path import basename, getctime
from autoregressive.util import AR_striding
from moseq2_model.train.models import ARHMM
from moseq2_model.helpers.data import count_frames

def load_pcs(filename, var_name="features", load_groups=False, npcs=10, h5_key_is_uuid=True):
    '''
    Load the Principal Component Scores for modeling.

    Parameters
    ----------
    filename (str): path to the file that contains PC scores
    var_name (str): key where the pc scores are stored within ``filename``
    load_groups (bool): Load metadata group variable
    npcs (int): Number of PCs to load
    h5_key_is_uuid (bool): use h5 key as uuid.

    Returns
    -------
    data_dict (OrderedDict): key-value pairs for keys being uuids and values being PC scores.
    metadata (OrderedDict): dictionary containing lists of index-aligned uuids and groups.
    '''

    metadata = {
        'uuids': None,
        'groups': [],
    }

    if filename.endswith('.mat'):
        print('Loading data from matlab file')
        data_dict = load_data_from_matlab(filename, var_name, npcs)

        # convert the uuid list to something that will export easily...
        metadata['uuids'] = load_cell_string_from_matlab(filename, "uuids")
        if load_groups:
            metadata['groups'] = load_cell_string_from_matlab(filename, "groups")
        else:
            metadata['groups'] = None

    elif filename.endswith(('.z', '.pkl', '.p')):
        print('Loading data from pickle file')
        data_dict = joblib.load(filename)

        # Reading in PCs and associated groups
        if isinstance(first(data_dict.values()), tuple):
            print('Detected tuple')
            for k, v in data_dict.items():
                data_dict[k] = v[0][:, :npcs]
                metadata['groups'].append(v[1])
        else:
            for k, v in data_dict.items():
                data_dict[k] = v[:, :npcs]

    elif filename.endswith('.h5'):
        # Reading PCs from h5 file
        with h5py.File(filename, 'r') as f:
            if var_name in f:
                print('Found pcs in {}'.format(var_name))
                tmp = f[var_name]

                # Reading in PCs into training dict
                if isinstance(tmp, h5py.Dataset):
                    data_dict = OrderedDict([(1, tmp[:, :npcs])])

                elif isinstance(tmp, h5py.Group):
                    # Reading in PCs
                    data_dict = OrderedDict([(k, v[:, :npcs]) for k, v in tmp.items()])
                    # Optionally loading groups
                    if load_groups:
                        metadata['groups'] = list(range(len(tmp)))
                    elif 'groups' in f:
                        metadata['groups'] = [f[f'groups/{key}'][()] for key in tmp.keys()]
                else:
                    raise IOError('Could not load data from h5 file')
            else:
                raise IOError(f'Could not find dataset name {var_name} in {filename}')

            if 'uuids' in f:
                # TODO: make sure uuids is in f, and not uuid
                metadata['uuids'] = f['uuid'][()]
            elif h5_key_is_uuid:
                metadata['uuids'] = list(data_dict.keys())
    else:
        raise ValueError('Did not understand filetype')

    return data_dict, metadata

def get_current_model(use_checkpoint, all_checkpoints, train_data, model_parameters):
    '''
    Checks to see whether user is loading a checkpointed model, if so, loads the latest iteration.
    Otherwise, will instantiate a new model.

    Parameters
    ----------
    use_checkpoint (bool): CLI input parameter indicating user is loading a checkpointed model
    all_checkpoints (list): list of all found checkpoint paths
    train_data (OrderedDict): dictionary of uuid-PC score key-value pairs
    model_parameters (dict): dictionary of required modeling hyperparameters.

    Returns
    -------
    arhmm (ARHMM): instantiated model object including loaded data
    itr (int): starting iteration number for the model to begin training from.
    '''

    # Check for available previous modeling checkpoints
    itr = 0
    if use_checkpoint:
        if len(all_checkpoints) > 0:
            # Get latest checkpoint (with respect to save date)
            latest_checkpoint = max(all_checkpoints, key=getctime)
            click.echo(f'Loading Checkpoint: {basename(latest_checkpoint)}')
            try:
                checkpoint = load_arhmm_checkpoint(latest_checkpoint, train_data)
                # Get model object
                arhmm = checkpoint.pop('model')
                itr = checkpoint.pop('iter')
                click.echo(f'On iteration {itr}')
            except (FileNotFoundError, ValueError):
                click.echo('Loading original checkpoint failed, creating new ARHMM')
                arhmm = ARHMM(data_dict=train_data, **model_parameters)
        else:
            click.echo('No matching checkpoints found, creating new ARHMM')
            arhmm = ARHMM(data_dict=train_data, **model_parameters)
    else:
        arhmm = ARHMM(data_dict=train_data, **model_parameters)

    return arhmm, itr

def get_loglikelihoods(arhmm, data, groups, separate_trans):
    '''
    Computes the log-likelihoods of the trained ARHMM states.

    Parameters
    ----------
    arhmm (ARHMM): Trained ARHMM model.
    data (dict): dict object containing training data keyed by their corresponding UUIDs
    groups (list): list of assigned groups for all corresponding session uuids. (Only used if
        separate_trans == True.
    separate_trans (bool): boolean that determines whether to compute separate log-likelihoods
    for each modeled group.

    Returns
    -------
    ll (list): list of log-likelihoods for the trained model, len(ll) > 1 if separate_trans==True
    '''

    if separate_trans:
        ll = [arhmm.log_likelihood(v, group_id=g) for g, v in zip(groups, data.values())]
    else:
        ll = [arhmm.log_likelihood(v) for v in data.values()]

    return ll

def get_session_groupings(data_metadata, groups, all_keys, hold_out_list):
    '''
    Creates a list or tuple of assigned groups for training and (optionally)
    held out data.

    Parameters
    ----------
    data_metadata (dict): dict containing session group information
    groups (list): list of all session groups
    all_keys (list): list of all corresponding included session UUIDs
    hold_out_list (list): list of held-out uuids

    Returns
    -------
    groupings (list or tuple): 1/2-tuple containing lists of train groups
    and held-out groups (if held_out_list exists)
    '''

    groupings = None

    if hold_out_list != None:
        # Get held out groups
        if groups == None:
            train_g, hold_g = [], []
        else:
            hold_g = []
            train_g = []
            # remove held out group
            for i in range(len(all_keys)):
                if all_keys[i] in hold_out_list:
                    hold_g.append(data_metadata['groups'][i])
                else:
                    train_g.append(data_metadata['groups'][i])

        # Ensure training groups were found before setting grouping
        if len(train_g) != 0:
            groupings = (train_g, hold_g)

    else:
        # set default group
        if groups == None:
            groupings = []
        else:
            groupings = list(groups)

    return groupings

def save_dict(filename, obj_to_save=None):
    '''
    Save dictionary to file.

    Parameters
    ----------
    filename (str): path to file where dict is being saved.
    obj_to_save (dict): dict to save.

    Returns
    -------
    None
    '''

    # Parsing given file extension and saving model accordingly
    if filename.endswith('.mat'):
        print('Saving MAT file', filename)
        scipy.io.savemat(filename, mdict=obj_to_save)
    elif filename.endswith('.z'):
        print('Saving compressed pickle', filename)
        joblib.dump(obj_to_save, filename, compress=('zlib', 4))
    elif filename.endswith('.pkl') | filename.endswith('.p'):
        print('Saving pickle', filename)
        joblib.dump(obj_to_save, filename, compress=0)
    elif filename.endswith('.h5'):
        print('Saving h5 file', filename)
        with h5py.File(filename, 'w') as f:
            dict_to_h5(f, obj_to_save)
    else:
        raise ValueError('Did not understand filetype')



def dict_to_h5(h5file, export_dict, path='/'):
    '''
    Recursively save dicts to h5 file groups.
    # https://codereview.stackexchange.com/questions/120802/recursively-save-python-dictionaries-to-hdf5-files-using-h5py

    Parameters
    ----------
    h5file (h5py.File): opened h5py File object.
    export_dict (dict): dictionary to save
    path (str): path within h5 to save to.

    Returns
    -------
    None
    '''

    for key, item in export_dict.items():
        # Parse key and value types, and load them accordingly
        if isinstance(key, (tuple, int)):
            key = str(key)
        if isinstance(item, str):
            item = item.encode('utf8')

        # Write dict item to h5 based on its data-type
        if isinstance(item, np.ndarray) and item.dtype == np.object:
            dt = h5py.special_dtype(vlen=item.flat[0].dtype)
            h5file.create_dataset(path+key, item.shape, dtype=dt, compression='gzip')
            for tup, _ in np.ndenumerate(item):
                if item[tup] is not None:
                    h5file[path+key][tup] = np.array(item[tup]).ravel()
        elif isinstance(item, (np.ndarray, list)):
            h5file.create_dataset(path+key, data=item, compression='gzip')
        elif isinstance(item, (np.int, np.float, str, bytes)):
            h5file.create_dataset(path+key, data=item)
        elif isinstance(item, dict):
            dict_to_h5(h5file, item, path + key + '/')
        else:
            raise ValueError(f'Cannot save {type(item)} type')


def load_arhmm_checkpoint(filename: str, train_data: dict) -> dict:
    '''
    Load an arhmm checkpoint and re-add data into the arhmm model checkpoint.

    Parameters
    ----------
    filename (str): path that specifies the checkpoint.
    train_data (OrderedDict): an OrderedDict that contains the training data

    Returns
    -------
    mdl_dict (dict): a dict containing the model with reloaded data, and associated training data
    '''

    # Loading model and its respective number of lags
    mdl_dict = joblib.load(filename)
    nlags = mdl_dict['model'].nlags

    for s, t in zip(mdl_dict['model'].states_list, train_data.values()):
        # Loading model AR-strided data
        s.data = AR_striding(t.astype('float32'), nlags)

    return mdl_dict

def save_arhmm_checkpoint(filename: str, arhmm: dict):
    '''
    Save an arhmm checkpoint and strip out data used to train the model.

    Parameters
    ----------
    filename (str): path that specifies the checkpoint
    arhmm (dict): a dictionary containing the model obj, training iteration number,
               log-likelihoods of each training step, and labels for each step.

    Returns
    -------
    None
    '''

    # Getting model object
    mdl = arhmm.pop('model')
    arhmm['model'] = copy_model(mdl)

    # Save model
    print(f'Saving Checkpoint {filename}')
    joblib.dump(arhmm, filename, compress=('zlib', 5))


def append_resample(filename, label_dict: dict):
    '''
    Adds the labels from a resampling iteration to a pickle file.

    Parameters
    ----------
    filename (str): file (containing modeling results) to append new label dict to.
    label_dict (dict): a dictionary with a single key/value pair, where the
            key is the sampling iteration and the value contains a dict of:
            (labels, a log likelihood val, and expected states if the flag is set)
            from each mouse.

    Returns
    -------
    None
    '''

    with open(filename, 'ab+') as f:
        pickle.dump(label_dict, f)


def _load_h5_to_dict(file: h5py.File, path: str) -> dict:
    '''
    A convenience function to load the contents of an h5 file
    at a user-specified path into a dictionary.

    Parameters
    ----------
    filename (str): path to h5 file.
    path (str): path within the h5 file to load data from.

    Returns
    -------
    (dict): dict containing all of the h5 file contents.
    '''

    ans = {}
    if isinstance(file[path], h5py._hl.dataset.Dataset):
        # only use the final path key to add to `ans`
        ans[path.split('/')[-1]] = file[path][()]
    else:
        # Reading in h5 value into dict key-value pair
        for key, item in file[path].items():
            if isinstance(item, h5py.Dataset):
                ans[key] = item[()]
            elif isinstance(item, h5py.Group):
                ans[key] = _load_h5_to_dict(file, '/'.join([path, key]))
    return ans


def h5_to_dict(h5file, path: str = '/') -> dict:
    '''
    Load h5 data to dictionary from a user specified path.

    Parameters
    ----------
    h5file (str or h5py.File): file path to the given h5 file or the h5 file handle
    path (str): path to the base dataset within the h5 file

    Returns
    -------
    out (dict): a dict with h5 file contents with the same path structure
    '''

    # Load h5 file according to whether it is separated by Groups
    if isinstance(h5file, str):
        with h5py.File(h5file, 'r') as f:
            out = _load_h5_to_dict(f, path)
    elif isinstance(h5file, (h5py.File, h5py.Group)):
        out = _load_h5_to_dict(h5file, path)
    else:
        raise Exception('file input not understood - need h5 file path or file object')
    return out


def load_data_from_matlab(filename, var_name="features", npcs=10):
    '''
    Load PC Scores from a specified variable column in a MATLAB file.

    Parameters
    ----------
    filename (str): path to MATLAB (.mat) file
    var_name (str): variable to load
    npcs (int): number of PCs to load.

    Returns
    -------
    data_dict (OrderedDict): loaded dictionary of uuid and PC-score pairings.
    '''

    data_dict = OrderedDict()

    with h5py.File(filename, 'r') as f:
        # Loading PCs scores into training data dict
        if var_name in f.keys():
            score_tmp = f[var_name]
            for i in range(len(score_tmp)):
                tmp = f[score_tmp[i][0]]
                score_to_add = tmp[()]
                data_dict[i] = score_to_add[:npcs, :].T

    return data_dict


def load_cell_string_from_matlab(filename, var_name="uuids"):
    '''
    Load cell strings from MATLAB file.

    Parameters
    ----------
    filename (str): path to .mat file
    var_name (str): cell name to read

    Returns
    -------
    return_list (list): list of selected loaded variables
    '''

    f = h5py.File(filename, 'r')
    return_list = []

    if var_name in f.keys():

        tmp = f[var_name]

        # change unichr to chr for python 3

        for i in range(len(tmp)):
            tmp2 = f[tmp[i][0]]
            uni_list = [''.join(chr(c)) for c in tmp2]
            return_list.append(''.join(uni_list))

    return return_list


# per Scott's suggestion
def copy_model(model_obj):
    '''
    Return a new copy of a model using deepcopy().

    Parameters
    ----------
    model_obj (ARHMM): model to copy.

    Returns
    -------
    cp (ARHMM): copy of the model
    '''

    tmp = []

    # make a deep copy of the data-less version
    for s in model_obj.states_list:
        tmp.append(s.data)
        s.data = None

    cp = deepcopy(model_obj)

    # now put the data back in

    for s, t in zip(model_obj.states_list, tmp):
        s.data = t

    return cp


def get_parameters_from_model(model):
    '''
    Get parameter dictionary from model.

    Parameters
    ----------
    model (ARHMM): model to get parameters from.

    Returns
    -------
    parameters (dict): dictionary containing all modeling parameters
    '''

    init_obs_dist = model.init_emission_distn.hypparams

    # Loading transition graph(s)
    if hasattr(model, 'trans_distns'):
        trans_dist = model.trans_distns[0]
    else:
        trans_dist = model.trans_distn

    ls_obj = dir(model.obs_distns[0])

    # Packing object parameters into a single dict
    parameters = {
        'kappa': trans_dist.kappa,
        'gamma': trans_dist.gamma,
        'alpha': trans_dist.alpha,
        'nu': np.nan,
        'max_states': trans_dist.N,
        'nu_0': init_obs_dist['nu_0'],
        'sigma_0': init_obs_dist['sigma_0'],
        'kappa_0': init_obs_dist['kappa_0'],
        'nlags': model.nlags,
        'mu_0': init_obs_dist['mu_0'],
        'model_class': model.__class__.__name__,
        'ar_mat': [obs.A for obs in model.obs_distns],
        'sig': [obs.sigma for obs in model.obs_distns]
        }

    if 'nu' in ls_obj:
        parameters['nu'] = [obs.nu for obs in model.obs_distns]

    return parameters

def get_parameter_strings(index_file, config_data):
    '''
    Creates the CLI learn-model parameters string using the given config_data dict contents.
     Function checks for the following paramters: [npcs, num_iter, separate_trans, robust, e_step,
      hold_out, max_states, converge, tolerance].

    Parameters
    ----------
    index_file (str): Path to index file.
    config_data (dict): Configuration parameters dict.

    Returns
    -------
    parameters (str): String containing all the requested CLI command parameter flags.
    prefix (str): Prefix string for the learn-model command, used for Slurm functionality.
    '''

    parameters = f'-i {index_file} --npcs {config_data["npcs"]} -n {config_data["num_iter"]} '

    if config_data['separate_trans']:
        parameters += '--separate-trans '

    if config_data['robust']:
        parameters += '--robust '

    if config_data['e_step']:
        parameters += '--e-step '

    if config_data['hold_out']:
        parameters += f'-h {str(config_data["nfolds"])} '

    if config_data['max_states']:
        parameters += f'-m {config_data["max_states"]} '

    if config_data['converge']:
        parameters += '--converge '

        parameters += f'-t {config_data["tolerance"]} '

    # Handle possible Slurm batch functionality
    prefix = ''
    if config_data['cluster_type'] == 'slurm':
        prefix = f'sbatch -c {config_data["ncpus"]} --mem={config_data["memory"]} '
        prefix += f'-p {config_data["partition"]} -t {config_data["wall_time"]} --wrap "'

    return parameters, prefix

def create_command_strings(input_file, index_file, output_dir, config_data, kappas, model_name_format='model-{}-{}.p'):
    '''
    Creates the CLI learn-model N command strings with parameter flags based on the contents of the configuration
     dict. Each model will a different kappa value within a given range (for N models to train).

    Parameters
    ----------
    input_file (str): Path to PCA Scores
    index_file (str): Path to index file
    output_dir (str): Path to directory to save models in.
    config_data (dict): Configuration parameters dict.
    kappas (list): List of kappa values to assign to model training commands.
    model_name_format (str): Filename string format string.

    Returns
    -------
    command_string (str): CLI learn-model command strings with the requested parameters separated by newline characters
    '''

    # Get base command and parameter flags
    base_command = f'moseq2-model learn-model {input_file} '
    parameters, prefix = get_parameter_strings(index_file, config_data)

    commands = []
    for i, k in enumerate(kappas):
        # Create CLI command
        cmd = base_command + os.path.join(output_dir, model_name_format.format(str(k), str(i))) + parameters + f'-k {k}'

        # Add possible batch fitting prefix string
        if config_data['cluster_type'] == 'slurm':
            cmd = prefix + cmd + '"'
        commands.append(cmd)

    # Create and return the command string
    command_string = '\n'.join(commands)
    return command_string

def get_kappa_within_range(min_kappa, max_kappa, n_models):
    '''
    Creates a list of kappa values incremented by the average difference between the
     inputted min and max kappa values. The values in the outputted list will be >=min_kappa && <=max_kappa.

    Parameters
    ----------
    min_kappa (int): Minimum Kappa value
    max_kappa (int): Maximum Kappa value
    n_models (int): Number of kappa values to compute within min-max range.

    Returns
    -------
    kappa (list): list of int kappa values of len == n_models.
    '''

    # Get average difference
    diff_kappa = min_kappa - max_kappa
    kappa_iter = int(diff_kappa / n_models)

    # Get kappa list
    kappas = list(range(min_kappa, max_kappa, kappa_iter))

    return kappas

def get_scan_range_kappas(data_dict, config_data):
    '''
    Helper function that checks if the user has inputted min and/or max kappa values to scan between,
     and returns a list of kappa values corresponding to their selected ranges. If no ranges are given,
     the kappa values will start at nframes/100 and increment by a factor of 10 times for each model.

    Parameters
    ----------
    data_dict (OrderedDict): Loaded PCA score dictionary.
    config_data (dict): Configuration parameters dict.

    Returns
    -------
    kappas (list): list of ints corresponding to the kappa value for each model. len(kappas) == config_data['n_models']
    '''

    if config_data['min_kappa'] == None or config_data['max_kappa'] == None:
        # Handle either of the missing parameters
        if config_data['min_kappa'] == None:
            # Choosing a minimum kappa value (AKA value to begin the scan from)
            # less than the counted number of frames
            min_kappa = count_frames(data_dict) / 100
            config_data['min_kappa'] = min_kappa # default initial kappa value

        # get kappa values for each model to train
        if config_data['max_kappa'] == None:
            # If no max is specified, kappa values will be incremented by factors of 10.
            kappas = [(config_data['min_kappa'] * (10 ** i)) for i in range(config_data['n_models'])]
        else:
            kappas = get_kappa_within_range(config_data['min_kappa'], config_data['max_kappa', config_data['n_models']])
    else:
        kappas = get_kappa_within_range(config_data['min_kappa'], config_data['max_kappa', config_data['n_models']])

    return kappas