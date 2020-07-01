import os
import sys
import glob
import click
from copy import deepcopy
from collections import OrderedDict
from moseq2_model.train.models import ARHMM
from moseq2_model.train.util import train_model, run_e_step
from os.path import join, basename, getctime, realpath, dirname, exists
from moseq2_model.util import (save_dict, load_pcs, get_parameters_from_model, copy_model, load_arhmm_checkpoint)
from moseq2_model.helpers.data import (process_indexfile, select_data_to_model, \
                                            prepare_model_metadata, graph_modeling_loglikelihoods, \
                                            get_heldout_data_splits, get_training_data_splits)

def learn_model_wrapper(input_file, dest_file, config_data, index=None, gui=False):
    '''
    Wrapper function to train ARHMM, shared between CLI and GUI.

    Parameters
    ----------
    input_file (str): path to pca scores file.
    dest_file (str): path to save model to.
    config_data (dict): dictionary containing necessary modeling parameters.
    index (str): path to index file.
    gui (bool): indicates whether Jupyter notebook is being used.
    Returns
    -------
    None
    '''

    # TODO: graceful handling of extra parameters:  orchestraconfig_data['ting'] this fails catastrophically if we pass
    # an extra option, just flag it to the user and ignore
    dest_file = realpath(dest_file)

    if not os.access(dirname(dest_file), os.W_OK):
        raise IOError('Output directory is not writable.')

    checkpoint_path = join(dirname(dest_file), 'checkpoints/')
    checkpoint_freq = config_data.get('checkpoint_freq', -1)

    if checkpoint_freq < 0:
        checkpoint_freq = config_data.get('num_iter', 100) + 1
    else:
        if not exists(checkpoint_path):
            os.makedirs(checkpoint_path)

    click.echo("Entering modeling training")

    run_parameters = deepcopy(config_data)
    data_dict, data_metadata = load_pcs(filename=input_file,
                                        var_name=config_data.get('var_name', 'scores'),
                                        npcs=config_data['npcs'],
                                        load_groups=True)

    index_data, data_metadata = process_indexfile(index, config_data, data_metadata)

    all_keys = list(data_dict.keys())
    groups = list(data_metadata['groups'])

    select_groups = config_data.get('select_groups', True)
    if (index_data != None):
        all_keys, groups = select_data_to_model(index_data, select_groups)
        data_metadata['groups'] = groups
        data_metadata['uuids'] = all_keys

    data_dict = OrderedDict((i, data_dict[i]) for i in all_keys)
    nkeys = len(all_keys)

    config_data, data_dict, model_parameters, train_list, hold_out_list = \
        prepare_model_metadata(data_dict, data_metadata, config_data, nkeys, all_keys)

    if config_data['hold_out']:
        train_data, hold_out_list, test_data, nt_frames = \
            get_heldout_data_splits(all_keys, data_dict, train_list, hold_out_list)
    else:
        train_data, training_data, validation_data, nt_frames = get_training_data_splits(config_data, data_dict)

    # check for available previous modeling checkpoints
    checkpoint_file = join(checkpoint_path, basename(dest_file).replace('.p', '') + '-checkpoint.arhmm')
    all_checkpoints = [f for f in glob.glob(f'{checkpoint_path}*.arhmm') if basename(dest_file).replace('.p', '') in f]

    itr = 0
    if config_data.get('use_checkpoint', False):
        if len(all_checkpoints) > 0:
            latest_checkpoint = max(all_checkpoints, key=getctime) # get latest checkpoint
            click.echo(f'Loading Checkpoint: {basename(latest_checkpoint)}')
            try:
                checkpoint = load_arhmm_checkpoint(latest_checkpoint, train_data)

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

    progressbar_kwargs = {
        'total': config_data['num_iter'],
        'cli': True,
        'file': sys.stdout,
        'leave': False,
        'disable': not config_data['progressbar'],
        'initial': itr
    }

    groupings = None
    if config_data['hold_out']:
        if model_parameters['groups'] == None:
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

        if len(train_g) != 0:
            groupings = (train_g, hold_g)

    else:
        if model_parameters['groups'] == None:
            groupings = []
        else:
            groupings = list(model_parameters['groups'])

        test_data = validation_data

    arhmm, loglikes_sample, labels_sample, iter_lls, iter_holls, group_idx = train_model\
    (
        model=arhmm,
        num_iter=config_data['num_iter'],
        ncpus=config_data['ncpus'],
        checkpoint_freq=checkpoint_freq,
        checkpoint_file=checkpoint_file,
        start=itr,
        progress_kwargs=progressbar_kwargs,
        num_frames=nt_frames,
        train_data=train_data,
        val_data=test_data,
        separate_trans=config_data['separate_trans'],
        groups=groupings,
        verbose=config_data['verbose']
    )

    ## Graph training summary
    img_path = graph_modeling_loglikelihoods(config_data, iter_lls, iter_holls, group_idx, dest_file)

    click.echo('Computing likelihoods on each training dataset...')

    if config_data['separate_trans']:
        train_ll = [arhmm.log_likelihood(v, group_id=g) for g, v in zip(data_metadata['groups'], train_data.values())]
    else:
        train_ll = [arhmm.log_likelihood(v) for v in train_data.values()]
    heldout_ll = []

    if config_data['hold_out'] and config_data['separate_trans']:
        click.echo('Computing held out likelihoods with separate transition matrix...')
        heldout_ll += [arhmm.log_likelihood(v, group_id=g) for g, v in
                       zip(data_metadata['groups'], test_data.values())]
    elif config_data['hold_out']:
        click.echo('Computing held out likelihoods...')
        heldout_ll += [arhmm.log_likelihood(v) for v in test_data.values()]

    loglikes = [loglikes_sample]
    labels = [labels_sample]
    save_parameters = [get_parameters_from_model(arhmm)]

    # if we save the model, don't use copy_model which strips out the data and potentially
    # leaves useless certain functions we'll want to use in the future (e.g. cross-likes)
    if config_data['e_step']:
        click.echo('Running E step...')
        expected_states = run_e_step(arhmm)

    # TODO:  just compute cross-likes at the end and potentially dump the model (what else
    # would we want the model for hm?), though hard drive space is cheap, recomputing models is not...

    export_dict = {
        'loglikes': loglikes,
        'labels': labels,
        'keys': all_keys,
        'heldout_ll': heldout_ll,
        'model_parameters': save_parameters,
        'run_parameters': run_parameters,
        'metadata': data_metadata,
        'model': copy_model(arhmm) if config_data.get('save_model', True) else None,
        'hold_out_list': hold_out_list,
        'train_list': train_list,
        'train_ll': train_ll,
        'expected_states': expected_states if config_data['e_step'] else None
    }

    save_dict(filename=str(dest_file), obj_to_save=export_dict)

    if config_data['verbose'] and gui:
        return img_path