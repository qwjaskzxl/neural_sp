#! /usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2018 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Train the ASR model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import cProfile
import numpy as np
import os
# from setproctitle import setproctitle
import shutil
import time
import torch
from tqdm import tqdm

from neural_sp.bin.asr.train_utils import Controller
from neural_sp.bin.asr.train_utils import Reporter
from neural_sp.bin.asr.train_utils import load_config
from neural_sp.bin.asr.train_utils import save_config
from neural_sp.bin.asr.train_utils import set_logger
from neural_sp.datasets.loader_asr import Dataset
from neural_sp.evaluators.character import eval_char
from neural_sp.evaluators.loss import eval_loss
from neural_sp.evaluators.phone import eval_phone
from neural_sp.evaluators.word import eval_word
from neural_sp.evaluators.wordpiece import eval_wordpiece
from neural_sp.models.data_parallel import CustomDataParallel
from neural_sp.models.seq2seq.seq2seq import Seq2seq
from neural_sp.models.seq2seq.transformer import Transformer
from neural_sp.models.rnnlm.rnnlm import RNNLM
from neural_sp.utils.general import mkdir_join

parser = argparse.ArgumentParser()
# general
parser.add_argument('--ngpus', type=int, default=0,
                    help='number of GPUs (0 indicates CPU)')
parser.add_argument('--model', type=str, default=None,
                    help='directory to save a model')
parser.add_argument('--resume', type=str, default=None,
                    help='path to the model to resume training')
parser.add_argument('--job_name', type=str, default='',
                    help='name of job')
# dataset
parser.add_argument('--train_set', type=str,
                    help='path to a csv file for the training set')
parser.add_argument('--train_set_sub', type=str, default=None,
                    help='path to a csv file for the training set for th sub tsk')
parser.add_argument('--dev_set', type=str,
                    help='path to a csv file for the development set')
parser.add_argument('--dev_set_sub', type=str, default=None,
                    help='path to a csv file for the development set for the sub task')
parser.add_argument('--eval_sets', type=str, default=[], nargs='+',
                    help='path to csv files for the evaluation sets')
parser.add_argument('--dict', type=str,
                    help='path to a dictionary file')
parser.add_argument('--dict_sub', type=str, default=None,
                    help='path to a dictionary file for the sub task')
parser.add_argument('--unit', type=str, default='word',
                    choices=['word', 'wp', 'char', 'phone', 'word_char'],
                    help='')
parser.add_argument('--unit_sub', type=str, default='char',
                    choices=['wp', 'char', 'phone'],
                    help='')
parser.add_argument('--wp_model', type=str, default=False, nargs='?',
                    help='path to of the wordpiece model')
# features
parser.add_argument('--input_type', type=str, default='speech',
                    choices=['speech', 'text'],
                    help='')
parser.add_argument('--nsplices', type=int, default=1,
                    help='')
parser.add_argument('--nstacks', type=int, default=1,
                    help='')
parser.add_argument('--nskips', type=int, default=1,
                    help='')
parser.add_argument('--max_nframes', type=int, default=2000,
                    help='')
parser.add_argument('--min_nframes', type=int, default=40,
                    help='')
parser.add_argument('--dynamic_batching', type=bool, default=True,
                    help='')
# topology (encoder)
parser.add_argument('--conv_in_channel', type=int, default=1, nargs='?',
                    help='')
parser.add_argument('--conv_channels', type=str, default="", nargs='?',
                    help='Delimited list input.')
parser.add_argument('--conv_kernel_sizes', type=str, default="", nargs='?',
                    help='Delimited list input.')
parser.add_argument('--conv_strides', type=str, default="", nargs='?',
                    help='Delimited list input.')
parser.add_argument('--conv_poolings', type=str, default="", nargs='?',
                    help='Delimited list input.')
parser.add_argument('--conv_batch_norm', type=bool, default=False, nargs='?',
                    help='')
parser.add_argument('--enc_type', type=str, default='blstm',
                    choices=['blstm', 'lstm', 'bgru', 'gru'],
                    help='')
parser.add_argument('--enc_nunits', type=int, default=320,
                    help='The number of units in each encoder RNN layer.')
parser.add_argument('--enc_nprojs', type=int, default=0,
                    help='The number of units in each projection layer after the RNN layer.')
parser.add_argument('--enc_nlayers', type=int, default=5,
                    help='The number of encoder RNN layers')
parser.add_argument('--enc_nlayers_sub', type=int, default=0,
                    help='')
parser.add_argument('--enc_residual', type=bool, default=False, nargs='?',
                    help='')
parser.add_argument('--subsample', type=str, default="",
                    help='Delimited list input.')
parser.add_argument('--subsample_type', type=str, default='drop',
                    choices=['drop', 'concat', 'max_pool'],
                    help='')
# topology (decoder)
parser.add_argument('--attn_type', type=str, default='location',
                    choices=['location', 'add', 'dot',
                             'luong_dot', 'luong_general', 'luong_concat'],
                    help='')
parser.add_argument('--attn_dim', type=int, default=128,
                    help='')
parser.add_argument('--attn_conv_nchannels', type=int, default=10,
                    help='')
parser.add_argument('--attn_conv_width', type=int, default=100,
                    help='')
parser.add_argument('--attn_nheads', type=int, default=1,
                    help='')
parser.add_argument('--attn_nheads_sub', type=int, default=1,
                    help='')
parser.add_argument('--attn_sharpening', type=float, default=1.0,
                    help='')
parser.add_argument('--attn_sigmoid', type=bool, default=False,
                    help='')
parser.add_argument('--bridge_layer', type=bool, default=False,
                    help='')
parser.add_argument('--dec_type', type=str, default='lstm',
                    choices=['lstm', 'gru'],
                    help='')
parser.add_argument('--dec_nunits', type=int, default=320,
                    help='')
parser.add_argument('--dec_nprojs', type=int, default=0,
                    help='')
parser.add_argument('--dec_nlayers', type=int, default=1,
                    help='')
parser.add_argument('--dec_nlayers_sub', type=int, default=1,
                    help='')
parser.add_argument('--dec_residual', type=bool, default=False, nargs='?',
                    help='')
parser.add_argument('--init_with_enc', type=bool, default=False,
                    help='')
parser.add_argument('--emb_dim', type=int, default=320,
                    help='')
parser.add_argument('--input_feeding', type=bool, default=False,
                    help='')
parser.add_argument('--ctc_fc_list', type=str, default="", nargs='?',
                    help='')
parser.add_argument('--ctc_fc_list_sub', type=str, default="", nargs='?',
                    help='')
# optimization
parser.add_argument('--batch_size', type=int, default=50,
                    help='')
parser.add_argument('--optimizer', type=str, default='adam',
                    choices=['adam', 'adadelta', 'sgd'],
                    help='')
parser.add_argument('--learning_rate', type=float, default=1e-3,
                    help='')
parser.add_argument('--nepochs', type=int, default=25,
                    help='')
parser.add_argument('--convert_to_sgd_epoch', type=int, default=20,
                    help='')
parser.add_argument('--print_step', type=int, default=200,
                    help='')
parser.add_argument('--metric', type=str, default='edit_distance',
                    choices=['edit_distance', 'loss', 'acc', 'ppl', 'bleu'],
                    help='')
parser.add_argument('--decay_type', type=str, default='per_epoch',
                    choices=['per_epoch', 'compare_metric', 'warmup'],
                    help='')
parser.add_argument('--decay_start_epoch', type=int, default=10,
                    help='')
parser.add_argument('--decay_rate', type=float, default=0.9,
                    help='')
parser.add_argument('--decay_patient_epoch', type=int, default=0,
                    help='')
parser.add_argument('--sort_stop_epoch', type=int, default=10000,
                    help='')
parser.add_argument('--not_improved_patient_epoch', type=int, default=5,
                    help='')
parser.add_argument('--eval_start_epoch', type=int, default=1,
                    help='')
parser.add_argument('--warmup_start_learning_rate', type=float, default=1e-4,
                    help='')
parser.add_argument('--warmup_step', type=int, default=0,
                    help='')
parser.add_argument('--warmup_epoch', type=int, default=0,
                    help='')
# initialization
parser.add_argument('--param_init', type=float, default=0.1,
                    help='')
parser.add_argument('--param_init_dist', type=str, default='uniform',
                    choices=['uniform', 'he', 'glorot', 'chainer'],
                    help='')
parser.add_argument('--rec_weight_orthogonal', type=bool, default=False,
                    help='')
parser.add_argument('--pretrained_model', default=False, nargs='?',
                    help='')
# regularization
parser.add_argument('--clip_grad_norm', type=float, default=5.0,
                    help='')
parser.add_argument('--dropout_in', type=float, default=0.0,
                    help='')
parser.add_argument('--dropout_enc', type=float, default=0.0,
                    help='')
parser.add_argument('--dropout_dec', type=float, default=0.0,
                    help='')
parser.add_argument('--dropout_emb', type=float, default=0.0,
                    help='')
parser.add_argument('--dropout_att', type=float, default=0.0,
                    help='')
parser.add_argument('--weight_decay', type=float, default=0,
                    help='')
parser.add_argument('--logits_temp', type=float, default=1.0,
                    help='')
parser.add_argument('--ss_prob', type=float, default=0.0,
                    help='')
parser.add_argument('--ss_type', type=str, default='constant',
                    choices=['constant'],
                    help='')
parser.add_argument('--lsm_prob', type=float, default=0.0,
                    help='')
parser.add_argument('--layer_norm', default=False,
                    help='If true, apply layer normalization (see https://arxiv.org/abs/1607.06450).')
# MTL
parser.add_argument('--ctc_weight', type=float, default=0.0,
                    help='')
parser.add_argument('--ctc_weight_sub', type=float, default=0.0,
                    help='')
parser.add_argument('--main_task_weight', type=float, default=1.0,
                    help='')
parser.add_argument('--mtl_per_batch', type=bool, default=False, nargs='?',
                    help='If True, change mini-batch per task')
# foroward-backward
parser.add_argument('--bwd_weight', type=float, default=0.0,
                    help='')
parser.add_argument('--bwd_weight_sub', type=float, default=0.0,
                    help='')
# cold fusion
parser.add_argument('--cold_fusion', type=str, default='hidden',
                    choices=['hidden', 'prob'],
                    help='')
parser.add_argument('--rnnlm_cold_fusion', type=str, default=False,
                    help='RNNLM parameters for cold fusion.')
# RNNLM init. and MTL
parser.add_argument('--internal_lm', type=bool, default=False,
                    help='')
parser.add_argument('--rnnlm_init', type=str, default=False,
                    help='')
parser.add_argument('--rnnlm_task_weight', type=float, default=0.0,
                    help='')
parser.add_argument('--share_lm_softmax', type=bool, default=False,
                    help='')
# transformer
parser.add_argument('--transformer', type=bool, default=False,
                    help='')
parser.add_argument('--d_model', type=int, default=512,
                    help='')
parser.add_argument('--d_ff', type=int, default=2048,
                    help='')
parser.add_argument('--pre_process', type=str, default=None,
                    help='')
parser.add_argument('--post_process', type=str, default='dal',
                    help='')
# parser.add_argument('--share_embedding', type=bool, default=True,
#                     help='')
args = parser.parse_args()

torch.manual_seed(1)
torch.cuda.manual_seed_all(1)

decode_params = {
    'batch_size': 1,
    'beam_width': 1,
    'min_len_ratio': 0.0,
    'max_len_ratio': 1.0,
    'length_penalty': 0.0,
    'coverage_penalty': 0.0,
    'coverage_threshold': 0.0,
    'rnnlm_weight': 0.0,
    'resolving_unk': False,
    'fwd_bwd_attention': False
}


def main():

    # Load a config file
    if args.resume is not None:
        config = load_config(os.path.join(args.resume, 'config.yml'))
        for k, v in config.items():
            setattr(args, k, v)

    # Automatically reduce batch size in multi-GPU setting
    if args.ngpus > 1:
        args.batch_size -= 10
        args.print_step //= args.ngpus

    subsample_factor = 1
    subsample_factor_sub = 1
    subsample = [int(s) for s in args.subsample.split('_')]
    if args.conv_poolings:
        for p in args.conv_poolings.split('_'):
            p = int(p.split(',')[0].replace('(', ''))
            if p > 1:
                subsample_factor *= p
    if args.train_set_sub is not None:
        subsample_factor_sub = subsample_factor * np.prod(subsample[:args.enc_nlayers_sub - 1])
    subsample_factor *= np.prod(subsample)

    # Load dataset
    train_set = Dataset(csv_path=args.train_set,
                        dict_path=args.dict,
                        unit=args.unit,
                        wp_model=args.wp_model,
                        batch_size=args.batch_size * args.ngpus,
                        nepochs=args.nepochs,
                        min_nframes=args.min_nframes,
                        max_nframes=args.max_nframes,
                        sort_by_input_length=True,
                        short2long=True,
                        sort_stop_epoch=args.sort_stop_epoch,
                        dynamic_batching=args.dynamic_batching,
                        ctc=args.ctc_weight > 0,
                        subsample_factor=subsample_factor,
                        csv_path_sub=args.train_set_sub,
                        dict_path_sub=args.dict_sub,
                        unit_sub=args.unit_sub,
                        ctc_sub=args.ctc_weight_sub > 0,
                        subsample_factor_sub=subsample_factor_sub,
                        skip_speech=(args.input_type != 'speech'))
    dev_set = Dataset(csv_path=args.dev_set,
                      dict_path=args.dict,
                      unit=args.unit,
                      wp_model=args.wp_model,
                      batch_size=args.batch_size * args.ngpus,
                      min_nframes=args.min_nframes,
                      max_nframes=args.max_nframes,
                      shuffle=True,
                      ctc=args.ctc_weight > 0,
                      subsample_factor=subsample_factor,
                      csv_path_sub=args.dev_set_sub,
                      dict_path_sub=args.dict_sub,
                      unit_sub=args.unit_sub,
                      ctc_sub=args.ctc_weight_sub > 0,
                      subsample_factor_sub=subsample_factor_sub,
                      skip_speech=(args.input_type != 'speech'))
    eval_sets = []
    for set in args.eval_sets:
        eval_sets += [Dataset(csv_path=set,
                              dict_path=args.dict,
                              unit=args.unit,
                              wp_model=args.wp_model,
                              batch_size=1,
                              is_test=True,
                              skip_speech=(args.input_type != 'speech'))]

    args.vocab = train_set.vocab
    args.vocab_sub = train_set.vocab_sub
    args.input_dim = train_set.input_dim

    # Load a RNNLM config file for cold fusion & RNNLM initialization
    # if config['rnnlm_cold_fusion']:
    #     if args.model is not None:
    #         config['rnnlm_config_cold_fusion'] = load_config(
    #             os.path.join(config['rnnlm_cold_fusion'], 'config.yml'), is_eval=True)
    #     elif args.resume is not None:
    #         config = load_config(os.path.join(
    #             args.resume, 'config_rnnlm_cf.yml'))
    #     assert args.unit == config['rnnlm_config_cold_fusion']['unit']
    #     config['rnnlm_config_cold_fusion']['vocab'] = train_set.vocab
    args.rnnlm_cold_fusion = None

    # Model setting
    if args.transformer:
        model = Transformer(args)
        dir_name = 'transformer'
        if len(args.conv_channels) > 0:
            tmp = dir_name
            dir_name = 'conv' + str(len(args.conv_channels.split('_'))) + 'L'
            if args.conv_batch_norm:
                dir_name += 'bn'
            dir_name += tmp
        dir_name += str(args.d_model) + 'H'
        dir_name += str(args.enc_nlayers) + 'L'
        dir_name += str(args.dec_nlayers) + 'L'
        dir_name += '_head' + str(args.attn_nheads)
        dir_name += '_' + args.optimizer
        dir_name += '_lr' + str(args.learning_rate)
        dir_name += '_bs' + str(args.batch_size)
        dir_name += '_ls' + str(args.lsm_prob)
        dir_name += '_' + str(args.pre_process) + 't' + str(args.post_process)
        if args.nstacks > 1:
            dir_name += '_stack' + str(args.nstacks)
        if args.bwd_weight > 0:
            dir_name += '_bwd' + str(args.bwd_weight)
    else:
        model = Seq2seq(args)
        dir_name = args.enc_type
        if args.conv_channels and len(args.conv_channels.split('_')) > 0:
            tmp = dir_name
            dir_name = 'conv' + str(len(args.conv_channels.split('_'))) + 'L'
            if args.conv_batch_norm:
                dir_name += 'bn'
            dir_name += tmp
        dir_name += str(args.enc_nunits) + 'H'
        dir_name += str(args.enc_nprojs) + 'P'
        dir_name += str(args.enc_nlayers) + 'L'
        dir_name += '_' + args.subsample_type + str(subsample_factor)
        dir_name += '_' + args.dec_type
        dir_name += str(args.dec_nunits) + 'H'
        # dir_name += str(args.dec_nprojs) + 'P'
        dir_name += str(args.dec_nlayers) + 'L'
        dir_name += '_' + args.attn_type
        if args.attn_nheads > 1:
            dir_name += '_head' + str(args.attn_nheads)
        dir_name += '_' + args.optimizer
        dir_name += '_lr' + str(args.learning_rate)
        dir_name += '_bs' + str(args.batch_size)
        dir_name += '_ss' + str(args.ss_prob)
        dir_name += '_ls' + str(args.lsm_prob)
        if args.layer_norm:
            dir_name += '_layernorm'
        if args.ctc_weight > 0:
            dir_name += '_ctc' + str(args.ctc_weight)
        if args.bwd_weight > 0:
            dir_name += '_bwd' + str(args.bwd_weight)
        if args.main_task_weight < 1:
            dir_name += '_main' + str(args.main_task_weight)
            if args.ctc_weight_sub > 0:
                dir_name += '_ctcsub' + str(args.ctc_weight_sub * (1 - args.main_task_weight))
            else:
                dir_name += '_attsub' + str(1 - args.main_task_weight)
        if args.mtl_per_batch:
            dir_name += '_mtlpertask'

    if args.resume is None:
        # Load pre-trained RNNLM
        # if config['rnnlm_cold_fusion']:
        #     rnnlm = RNNLM(args)
        #     rnnlm.load_checkpoint(save_path=config['rnnlm_cold_fusion'], epoch=-1)
        #     rnnlm.flatten_parameters()
        #
        #     # Fix RNNLM parameters
        #     for param in rnnlm.parameters():
        #         param.requires_grad = False
        #
        #     # Set pre-trained parameters
        #     if config['rnnlm_config_cold_fusion']['backward']:
        #         model.dec_0_bwd.rnnlm = rnnlm
        #     else:
        #         model.dec_0_fwd.rnnlm = rnnlm
        # TODO(hirofumi): 最初にRNNLMのモデルをコピー

        # Set save path
        save_path = mkdir_join(args.model, '_'.join(os.path.basename(args.train_set).split('.')[:-1]), dir_name)
        model.set_save_path(save_path)  # avoid overwriting

        # Save the config file as a yaml file
        save_config(vars(args), model.save_path)

        # Save the dictionary & wp_model
        shutil.copy(args.dict, os.path.join(model.save_path, 'dict.txt'))
        if args.dict_sub is not None:
            shutil.copy(args.dict_sub, os.path.join(model.save_path, 'dict_sub.txt'))
        if args.unit == 'wp':
            shutil.copy(args.wp_model, os.path.join(model.save_path, 'wp.model'))

        # Setting for logging
        logger = set_logger(os.path.join(model.save_path, 'train.log'), key='training')

        for k, v in sorted(vars(args).items(), key=lambda x: x[0]):
            logger.info('%s: %s' % (k, str(v)))

        # Count total parameters
        for n in sorted(list(model.num_params_dict.keys())):
            nparams = model.num_params_dict[n]
            logger.info("%s %d" % (n, nparams))
        logger.info("Total %.2f M parameters" % (model.total_parameters / 1000000))
        logger.info(model)

        # if os.path.isdir(args.pretrained_model):
        #     # NOTE: Start training from the pre-trained model
        #     # This is defferent from resuming training
        #     model.load_checkpoint(args.pretrained_model, epoch=-1,
        #                           load_pretrained_model=True)

        # Set optimizer
        model.set_optimizer(optimizer=args.optimizer,
                            learning_rate_init=float(args.learning_rate),
                            weight_decay=float(args.weight_decay),
                            clip_grad_norm=args.clip_grad_norm,
                            lr_schedule=False,
                            factor=args.decay_rate,
                            patience_epoch=args.decay_patient_epoch)

        epoch, step = 1, 1
        learning_rate = float(args.learning_rate)
        metric_dev_best = 10000

    # NOTE: Restart from the last checkpoint
    # elif args.resume is not None:
    #     # Set save path
    #     model.save_path = args.resume
    #
    #     # Setting for logging
    #     logger = set_logger(os.path.join(model.save_path, 'train.log'), key='training')
    #
    #     # Set optimizer
    #     model.set_optimizer(
    #         optimizer=config['optimizer'],
    #         learning_rate_init=float(config['learning_rate']),  # on-the-fly
    #         weight_decay=float(config['weight_decay']),
    #         clip_grad_norm=config['clip_grad_norm'],
    #         lr_schedule=False,
    #         factor=config['decay_rate'],
    #         patience_epoch=config['decay_patient_epoch'])
    #
    #     # Restore the last saved model
    #     epoch, step, learning_rate, metric_dev_best = model.load_checkpoint(
    #         save_path=args.resume, epoch=-1, restart=True)
    #
    #     if epoch >= config['convert_to_sgd_epoch']:
    #         model.set_optimizer(
    #             optimizer='sgd',
    #             learning_rate_init=float(config['learning_rate']),  # on-the-fly
    #             weight_decay=float(config['weight_decay']),
    #             clip_grad_norm=config['clip_grad_norm'],
    #             lr_schedule=False,
    #             factor=config['decay_rate'],
    #             patience_epoch=config['decay_patient_epoch'])
    #
    #     if config['rnnlm_cold_fusion']:
    #         if config['rnnlm_config_cold_fusion']['backward']:
    #             model.rnnlm_0_bwd.flatten_parameters()
    #         else:
    #             model.rnnlm_0_fwd.flatten_parameters()

    train_set.epoch = epoch - 1  # start from index:0

    # GPU setting
    if args.ngpus >= 1:
        model = CustomDataParallel(model,
                                   device_ids=list(range(0, args.ngpus, 1)),
                                   deterministic=False,
                                   benchmark=True)
        model.cuda()

    logger.info('PID: %s' % os.getpid())
    logger.info('USERNAME: %s' % os.uname()[1])

    # Set process name
    # setproctitle(args.job_name)

    # Set learning rate controller
    lr_controller = Controller(learning_rate_init=learning_rate,
                               decay_type=args.decay_type,
                               decay_start_epoch=args.decay_start_epoch,
                               decay_rate=args.decay_rate,
                               decay_patient_epoch=args.decay_patient_epoch,
                               lower_better=True,
                               best_value=metric_dev_best,
                               model_size=args.d_model,
                               warmup_step=args.warmup_step,
                               factor=1)

    # Set reporter
    reporter = Reporter(model.module.save_path, tensorboard=True)

    start_time_train = time.time()
    start_time_epoch = time.time()
    start_time_step = time.time()
    not_improved_epoch = 0
    pbar_epoch = tqdm(total=len(train_set))
    while True:
        # Compute loss in the training set
        batch_train, is_new_epoch = train_set.next()
        if args.mtl_per_batch:
            # Change tasks depending on task
            # NOTE: from easier to harder tasks
            for task in ['ys_sub', 'ys']:
                model.module.optimizer.zero_grad()
                loss, reporter = model(batch_train['xs'], batch_train[task],
                                       reporter=reporter, task=task)
                if len(model.device_ids) > 1:
                    loss.backward(torch.ones(len(model.device_ids)))
                else:
                    loss.backward()
                loss.detach()  # Trancate the graph
                if args.clip_grad_norm > 0:
                    torch.nn.utils.clip_grad_norm_(model.module.parameters(), args.clip_grad_norm)
                model.module.optimizer.step()
                loss_train = loss.item()
                del loss
        else:
            model.module.optimizer.zero_grad()
            loss, reporter = model(batch_train['xs'], batch_train['ys'], batch_train['ys_sub'],
                                   reporter=reporter)
            if len(model.device_ids) > 1:
                loss.backward(torch.ones(len(model.device_ids)))
            else:
                loss.backward()
            loss.detach()  # Trancate the graph
            if args.clip_grad_norm > 0:
                torch.nn.utils.clip_grad_norm_(model.module.parameters(), args.clip_grad_norm)
            model.module.optimizer.step()
            loss_train = loss.item()
            del loss
        reporter.step(is_eval=False)

        # Update learning rate
        if args.decay_type == 'warmup':
            model.module.optimizer, learning_rate = lr_controller.warmup_lr(
                optimizer=model.module.optimizer,
                learning_rate=learning_rate,
                step=step)

        if step % args.print_step == 0:
            # Compute loss in the dev set
            batch_dev = dev_set.next()[0]
            if args.mtl_per_batch:
                # Change tasks depending on task
                for task in ['ys_sub', 'ys']:
                    loss, reporter = model(batch_dev['xs'], batch_dev[task],
                                           reporter=reporter, task=task, is_eval=True)
                    loss_dev = loss.item()
                    del loss
            else:
                loss, reporter = model(batch_dev['xs'], batch_dev['ys'], batch_dev['ys_sub'],
                                       reporter=reporter, is_eval=True)
                loss_dev = loss.item()
                del loss
            reporter.step(is_eval=True)

            duration_step = time.time() - start_time_step
            if args.input_type == 'speech':
                x_len = max(len(x) for x in batch_train['xs'])
            elif args.input_type == 'text':
                x_len = max(len(x) for x in batch_train['ys'])
            logger.info("step:%d(ep:%.2f) loss:%.3f(%.3f)/lr:%.5f/bs:%d/x_len:%d (%.2f min)" %
                        (step, train_set.epoch_detail,
                         loss_train, loss_dev,
                         learning_rate, len(batch_train['utt_ids']),
                         x_len, duration_step / 60))
            start_time_step = time.time()
        step += args.ngpus
        pbar_epoch.update(len(batch_train['utt_ids']))

        # Save fugures of loss and accuracy
        if step % (args.print_step * 10) == 0:
            reporter.snapshot()

        # Save checkpoint and evaluate model per epoch
        if is_new_epoch:
            duration_epoch = time.time() - start_time_epoch
            logger.info('========== EPOCH:%d (%.2f min) ==========' % (epoch, duration_epoch / 60))

            if epoch < args.eval_start_epoch:
                # Save the model
                model.module.save_checkpoint(model.module.save_path, epoch, step - 1,
                                             learning_rate, metric_dev_best)
            else:
                start_time_eval = time.time()
                # dev
                if args.metric == 'edit_distance':
                    if args.unit in ['word', 'word_char']:
                        metric_dev = eval_word([model.module], dev_set, decode_params,
                                               epoch=epoch)[0]
                        logger.info('WER (%s): %.3f %%' % (dev_set.set, metric_dev))
                    elif args.unit == 'wp':
                        metric_dev = eval_wordpiece([model.module], dev_set, decode_params,
                                                    epoch=epoch)[0]
                        logger.info('WER (%s): %.3f %%' % (dev_set.set, metric_dev))
                    elif 'char' in args.unit:
                        dev_results = eval_char([model.module], dev_set, decode_params,
                                                epoch=epoch)
                        metric_dev = dev_results[1][0]
                        wer_dev = dev_results[0][0]
                        logger.info('CER (%s): %.3f %%' % (dev_set.set, metric_dev))
                        logger.info('WER (%s): %.3f %%' % (dev_set.set, wer_dev))
                    elif 'phone' in args.unit:
                        metric_dev = eval_phone([model.module], dev_set, decode_params,
                                                epoch=epoch)[0]
                        logger.info('PER (%s): %.3f %%' % (dev_set.set, metric_dev))
                elif args.metric == 'loss':
                    metric_dev = eval_loss([model.module], dev_set, decode_params)
                    logger.info('Loss (%s): %.3f %%' % (dev_set.set, metric_dev))
                else:
                    raise NotImplementedError()

                # Update learning rate
                if args.decay_type != 'warmup':
                    model.module.optimizer, learning_rate = lr_controller.decay_lr(
                        optimizer=model.module.optimizer,
                        learning_rate=learning_rate,
                        epoch=epoch,
                        value=metric_dev)

                if metric_dev < metric_dev_best:
                    metric_dev_best = metric_dev
                    not_improved_epoch = 0
                    logger.info('||||| Best Score |||||')

                    # Save the model
                    model.module.save_checkpoint(model.module.save_path, epoch, step - 1,
                                                 learning_rate, metric_dev_best)

                    # test
                    for eval_set in eval_sets:
                        if args.metric == 'edit_distance':
                            if args.unit in ['word', 'word_char']:
                                wer_test = eval_word([model.module], eval_set, decode_params,
                                                     epoch=epoch)[0]
                                logger.info('WER (%s): %.3f %%' % (eval_set.set, wer_test))
                            elif args.unit == 'wp':
                                wer_test = eval_wordpiece([model.module], eval_set, decode_params,
                                                          epoch=epoch)[0]
                                logger.info('WER (%s): %.3f %%' % (eval_set.set, wer_test))
                            elif 'char' in args.unit:
                                test_results = eval_char([model.module], eval_set, decode_params,
                                                         epoch=epoch)
                                cer_test = test_results[1][0]
                                wer_test = test_results[0][0]
                                logger.info('CER (%s): %.3f %%' % (eval_set.set, cer_test))
                                logger.info('WER (%s): %.3f %%' % (eval_set.set, wer_test))
                            elif 'phone' in args.unit:
                                per_test = eval_phone([model.module], eval_set, decode_params,
                                                      epoch=epoch)[0]
                                logger.info('PER (%s): %.3f %%' % (eval_set.set, per_test))
                        elif args.metric == 'loss':
                            loss_test = eval_loss([model.module], eval_set, decode_params)
                            logger.info('Loss (%s): %.3f %%' % (eval_set.set, loss_test))
                        else:
                            raise NotImplementedError()
                else:
                    not_improved_epoch += 1

                duration_eval = time.time() - start_time_eval
                logger.info('Evaluation time: %.2f min' % (duration_eval / 60))

                # Early stopping
                if not_improved_epoch == args.not_improved_patient_epoch:
                    break

                if epoch == args.convert_to_sgd_epoch:
                    # Convert to fine-tuning stage
                    model.module.set_optimizer(
                        'sgd',
                        learning_rate_init=float(args.learning_rate),  # TODO: ?
                        weight_decay=float(args.weight_decay),
                        clip_grad_norm=args.clip_grad_norm,
                        lr_schedule=False,
                        factor=args.decay_rate,
                        patience_epoch=args.decay_patient_epoch)
                    logger.info('========== Convert to SGD ==========')

            pbar_epoch = tqdm(total=len(train_set))

            if epoch == args.nepochs:
                break

            start_time_step = time.time()
            start_time_epoch = time.time()
            epoch += 1

    duration_train = time.time() - start_time_train
    logger.info('Total time: %.2f hour' % (duration_train / 3600))

    if reporter.tensorboard:
        reporter.tf_writer.close()
    pbar_epoch.close()

    return model.module.save_path


if __name__ == '__main__':
    # Setting for profiling
    pr = cProfile.Profile()
    save_path = pr.runcall(main)
    pr.dump_stats(os.path.join(save_path, 'train.profile'))
