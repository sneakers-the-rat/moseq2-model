import click
import os
import sys
import shutil
import random
import warnings
import numpy as np
from pathlib import Path
from copy import deepcopy
from collections import OrderedDict
from moseq2_model.train.models import ARHMM
from moseq2_model.train.util import train_model, whiten_all, whiten_each, run_e_step
from moseq2_model.util import (save_dict, load_pcs, get_parameters_from_model, copy_model,
                               load_arhmm_checkpoint)

orig_init = click.core.Option.__init__


def new_init(self, *args, **kwargs):
    orig_init(self, *args, **kwargs)
    self.show_default = True


click.core.Option.__init__ = new_init

@click.group()
def cli():
    pass


@cli.command(name='count-frames')
@click.argument("input_file", type=click.Path(exists=True))
@click.option("--var-name", type=str, default='scores', help="Variable name in input file with PCs")
def count_frames(input_file, var_name):

    data_dict, _ = load_pcs(filename=input_file, var_name=var_name,
                                        npcs=10, load_groups=False)
    total_frames = 0
    for v in data_dict.values():
        idx = (~np.isnan(v)).all(axis=1)
        total_frames += np.sum(idx)

    print('Total frames: {}'.format(total_frames))


# this is the entry point for learning models over Kubernetes, expose all
# parameters we could/would possibly scan over
@cli.command(name="learn-model")
@click.argument("input_file", type=click.Path(exists=True))
@click.argument("dest_file", type=click.Path(file_okay=True, writable=True, resolve_path=True))
@click.option("--hold-out", "-h", type=bool, default=False, is_flag=True,
              help="Hold out one fold (set by nfolds) for computing heldout likelihood")
@click.option("--hold-out-seed", type=int, default=-1,
              help="Random seed for holding out data (set for reproducibility)")
@click.option("--nfolds", type=int, default=5, help="Number of folds for split")
@click.option("--ncpus", "-c", type=int, default=0, help="Number of cores to use for resampling")
@click.option("--num-iter", "-n", type=int, default=100, help="Number of times to resample model")
@click.option("--var-name", type=str, default='scores', help="Variable name in input file with PCs")
@click.option("--save-every", "-s", type=int, default=-1,
              help="Increment to save labels and model object (-1 for just last)")
@click.option("--save-model", is_flag=True, help="Save model object at the end of training")
@click.option("--max-states", "-m", type=int, default=100, help="Maximum number of states")
@click.option("--progressbar", "-p", type=bool, default=True, help="Show model progress")
@click.option("--checkpoint-freq", type=int, default=None, help='checkpoint the training after N iterations')
@click.option("--npcs", type=int, default=10, help="Number of PCs to use")
@click.option("--whiten", "-w", type=click.Choice(['each', 'all', 'none']), default='all', help="Whiten (e)each (a)ll or (n)o whitening")
@click.option("--kappa", "-k", type=float, default=None, help="Kappa")
@click.option("--gamma", "-g", type=float, default=1e3, help="Gamma")
@click.option("--alpha", "-g", type=float, default=5.7, help="Alpha")
@click.option("--noise-level", type=float, default=0, help="Additive white gaussian noise for regularization" )
@click.option("--nu", type=float, default=4, help="Nu (only applicable if robust set to true)")
@click.option("--nlags", type=int, default=3, help="Number of lags to use")
@click.option("--separate-trans", is_flag=True, help="Use separate transition matrix per group")
@click.option("--robust", is_flag=True, help="Use tAR model")
@click.option('--e-step', default=True, type=bool, help="Compute the expected states for each animal")
def learn_model(input_file, dest_file, hold_out, hold_out_seed, nfolds, ncpus,
                num_iter, var_name, e_step, save_every, save_model, max_states,
                progressbar, npcs, whiten, kappa, gamma, alpha, noise_level, nu,
                nlags, separate_trans, robust, checkpoint_freq):

    # TODO: graceful handling of extra parameters:  orchestrating this fails catastrophically if we pass
    # an extra option, just flag it to the user and ignore
    dest_file = Path(dest_file).resolve()

    # if not(os.path.dirname(dest_file)):
    #     dest_file = os.path.join('./', dest_file)
    if not os.access(dest_file.parent, os.W_OK):
        raise IOError('Output directory is not writable.')

    if save_every < 0:
        click.echo("Will only save the last iteration of the model")
        save_every = num_iter + 1

    click.echo("Entering modeling training")

    run_parameters = deepcopy(locals())
    data_dict, data_metadata = load_pcs(filename=input_file,
                                        var_name=var_name,
                                        npcs=npcs,
                                        load_groups=separate_trans)
    all_keys = list(data_dict.keys())
    nkeys = len(all_keys)

    if kappa is None:
        total_frames = 0
        for v in data_dict.values():
            idx = (~np.isnan(v)).all(axis=1)
            total_frames += np.sum(idx)
        print(f'Setting kappa to the number of frames: {total_frames}')
        kappa = total_frames

    if hold_out and nkeys >= nfolds:
        click.echo(f"Will hold out 1 fold of {nfolds}")

        if hold_out_seed >= 0:
            click.echo(f"Settings random seed to {hold_out_seed}")
            splits = np.array_split(random.Random(hold_out_seed).sample(list(range(nkeys)), nkeys), nfolds)
        else:
            warnings.warn("Random seed not set, will choose a different test set each time this is run...")
            splits = np.array_split(random.sample(list(range(nkeys)), nkeys), nfolds)

        hold_out_list = [all_keys[k] for k in splits[0].astype('int').tolist()]
        train_list = [k for k in all_keys if k not in hold_out_list]
        click.echo(f"Holding out {hold_out_list}")
        click.echo(f"Training on {train_list}")
    else:
        hold_out = False
        hold_out_list = None
        train_list = all_keys

    if ncpus > len(train_list):
        warnings.warn('Setting ncpus to {}, ncpus must be <= nkeys in dataset, {}'.format(nkeys, len(train_list)))
        ncpus = len(train_list)

    # use a list of dicts, with everything formatted ready to go
    model_parameters = {
        'gamma': gamma,
        'alpha': alpha,
        'kappa': kappa,
        'nlags': nlags,
        'separate_trans': separate_trans,
        'robust': robust,
        'max_states': max_states,
        'nu': nu
    }

    if separate_trans:
        model_parameters['groups'] = data_metadata['groups']
    else:
        model_parameters['groups'] = None

    if whiten[0].lower() == 'a':
        click.echo('Whitening the training data using the whiten_all function')
        data_dict = whiten_all(data_dict)
    elif whiten[0].lower() == 'e':
        click.echo('Whitening the training data using the whiten_each function')
        data_dict = whiten_each(data_dict)
    else:
        click.echo('Not whitening the data')

    if noise_level > 0:
        click.echo('Using {} STD AWGN'.format(noise_level))
        for k, v in data_dict.items():
            data_dict[k] = v + np.random.randn(*v.shape) * noise_level

    if hold_out:
        train_data = OrderedDict((i, data_dict[i]) for i in all_keys if i in train_list)
        test_data = OrderedDict((i, data_dict[i]) for i in all_keys if i in hold_out_list)
        train_list = list(train_data.keys())
        hold_out_list = list(test_data.keys())
    else:
        train_data = data_dict
        test_data = None
        train_list = list(data_dict.keys())
        test_list = None

    loglikes = []
    labels = []
    heldout_ll = []
    save_parameters = []
    # _tmp = os.path.splitext(dest_file)[0]
    # checkpoint_file = _tmp + '-checkpoint.arhmm'
    # checkpoint_file2 = checkpoint_file + '.1'
    checkpoint_file = dest_file.parent / dest_file.stem + '-checkpoint.arhmm'
    checkpoint_file_backup = checkpoint_file + '.1'

    # look for model checkpoint
    if checkpoint_file.exists() or checkpoint_file_backup.exists():
        print('Loading checkpoint')
        try:
            checkpoint = load_arhmm_checkpoint(checkpoint_file, train_data)
        except (FileNotFoundError, ValueError):
            print('Loading original checkpoint failed, checking backup')
            if checkpoint_file.exists():
                checkpoint_file.unlink()
            checkpoint = load_arhmm_checkpoint(checkpoint_file_backup, train_data)
        arhmm = checkpoint.pop('model')
        itr = checkpoint.pop('iter')
        print('On iteration', itr)
    else:
        arhmm = ARHMM(data_dict=train_data, **model_parameters)
        itr = 0

    progressbar_kws = {
        'total': num_iter,
        'cli': True,
        'file': sys.stdout,
        'leave': False,
        'disable': not progressbar,
        'initial': itr
    }
        
    arhmm, loglikes_sample, labels_sample = train_model(
        model=arhmm,
        save_every=save_every,
        num_iter=num_iter,
        ncpus=ncpus,
        checkpoint_freq=checkpoint_freq,
        e_step=e_step,
        chkpt_file=checkpoint_file,
        start=itr,
        progress_kws=progressbar_kws,
    )

    if test_data and separate_trans:
        click.echo("Computing held out likelihoods with separate transition matrix...")
        [heldout_ll.append(arhmm.log_likelihood(v, group_id=data_metadata['groups'][i]))
            for i, (k, v) in enumerate(test_data.items())]
    elif test_data:
        click.echo("Computing held out likelihoods...")
        [heldout_ll.append(arhmm.log_likelihood(v)) for k, v in test_data.items()]

    loglikes.append(loglikes_sample)
    labels.append(labels_sample)
    save_parameters.append(get_parameters_from_model(arhmm))

    # if we save the model, don't use copy_model which strips out the data and potentially
    # leaves useless certain functions we'll want to use in the future (e.g. cross-likes)
    if e_step:
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
        'model': copy_model(arhmm) if save_model else None,
        'hold_out_list': hold_out_list,
        'train_list': train_list
        }

    if e_step:
        export_dict['expected_states'] = expected_states

    save_dict(filename=dest_file, obj_to_save=export_dict)


if __name__ == '__main__':
    cli()
