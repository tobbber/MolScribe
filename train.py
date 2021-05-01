import os
import sys
import re
import time
import json
import random
import argparse
import collections
import numpy as np
import pandas as pd
from tqdm.auto import tqdm

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from torch.optim import Adam, AdamW, SGD
from torch.utils.data import DataLoader, Dataset, RandomSampler
from torch.nn.utils.rnn import pad_sequence, pack_padded_sequence
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts, CosineAnnealingLR, ReduceLROnPlateau
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data.distributed import DistributedSampler

from apex import amp

from bms.dataset import TrainDataset, TestDataset, bms_collate
from bms.model import Encoder, DecoderWithAttention
from bms.utils import init_logger, init_summary_writer, seed_torch, get_score, AverageMeter, asMinutes, timeSince, \
                      batch_convert_smiles_to_inchi, FORMAT_INFO, print_rank_0


import warnings 
warnings.filterwarnings('ignore')


class CFG:
    max_len=120
    print_freq=200
    num_workers=4
    scheduler='CosineAnnealingLR' # ['ReduceLROnPlateau', 'CosineAnnealingLR', 'CosineAnnealingWarmRestarts']
    encoder_lr=1e-4
    decoder_lr=4e-4
    min_lr=1e-6
    weight_decay=1e-6
    max_grad_norm=5
    attention_dim=256
    embed_dim=256
    decoder_dim=512
    dropout=0.5
    seed=42


def get_args():
    parser = argparse.ArgumentParser()
    # Model
    parser.add_argument('--encoder', type=str, default='resnet34')
    parser.add_argument('--decoder', type=str, default='lstm')
    parser.add_argument('--decoder_scale', type=int, default=1)
    parser.add_argument('--decoder_layer', type=int, default=1)
    # Data
    parser.add_argument('--format', type=str, choices=['inchi','atomtok','spe'], default='atomtok')
    parser.add_argument('--input_size', type=int, default=224)
    # Training
    parser.add_argument('--epochs', type=int, default=8)
    parser.add_argument('--batch_size', type=int, default=256)
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1)
    parser.add_argument('--load_path', type=str, default=None)
    parser.add_argument('--save_path', type=str, default='output/')
    parser.add_argument('--do_train', action='store_true')
    parser.add_argument('--do_valid', action='store_true')
    parser.add_argument('--do_test', action='store_true')
    parser.add_argument('--concat_valid', action='store_true', help='Concatenate valid data for training.')
    parser.add_argument('--local_rank', type=int, default=-1, metavar='N', help='Local process rank.')
    parser.add_argument('--fp16', action='store_true')
    parser.add_argument('--init_scheduler', action='store_true')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()
    return args


def get_model(args, tokenizer, device, load_path=None, ddp=True):
    encoder = Encoder(args.encoder, pretrained=True)
    decoder = DecoderWithAttention(attention_dim=CFG.attention_dim * args.decoder_scale,
                                   embed_dim=CFG.embed_dim * args.decoder_scale,
                                   encoder_dim=encoder.n_features,
                                   decoder_dim=CFG.decoder_dim * args.decoder_scale,
                                   max_len=CFG.max_len,
                                   dropout=CFG.dropout,
                                   n_layer=args.decoder_layer,
                                   vocab_size=len(tokenizer),
                                   tokenizer=tokenizer)
    if load_path:
        states = torch.load(
            os.path.join(load_path, f'{args.encoder}_{args.decoder}_best.pth'),
            map_location=torch.device('cpu')
        )
        def remove_prefix(state_dict):
            return {k.replace('module.', ''): v for k,v in state_dict.items()}
        encoder.load_state_dict(remove_prefix(states['encoder']))
        decoder.load_state_dict(remove_prefix(states['decoder']))
        print_rank_0(f"Model loaded from {load_path}")
    
    encoder.to(device)
    decoder.to(device)

    return encoder, decoder


def get_optimizer_and_scheduler(args, encoder, decoder, load_path=None):
    
    def get_scheduler(optimizer):
        if CFG.scheduler=='ReduceLROnPlateau':
            scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=CFG.factor, patience=CFG.patience, verbose=True, eps=CFG.eps)
        elif CFG.scheduler=='CosineAnnealingLR':
            scheduler = CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=CFG.min_lr, last_epoch=-1)
        elif CFG.scheduler=='CosineAnnealingWarmRestarts':
            scheduler = CosineAnnealingWarmRestarts(optimizer, T_0=CFG.T_0, T_mult=1, eta_min=CFG.min_lr, last_epoch=-1)
        return scheduler
    
    encoder_optimizer = AdamW(encoder.parameters(), lr=CFG.encoder_lr, weight_decay=CFG.weight_decay, amsgrad=False)
    encoder_scheduler = get_scheduler(encoder_optimizer)

    decoder_optimizer = AdamW(decoder.parameters(), lr=CFG.decoder_lr, weight_decay=CFG.weight_decay, amsgrad=False)
    decoder_scheduler = get_scheduler(decoder_optimizer)
    
    if load_path:
        states = torch.load(
            os.path.join(load_path, f'{args.encoder}_{args.decoder}_best.pth'),
            map_location=torch.device('cpu')
        )
        encoder_optimizer.load_state_dict(states['encoder_optimizer'])
        decoder_optimizer.load_state_dict(states['decoder_optimizer'])
        if args.init_scheduler:
            for group in encoder_optimizer.param_groups:
                group['lr'] = CFG.encoder_lr
            for group in decoder_optimizer.param_groups:
                group['lr'] = CFG.decoder_lr
        else:
            encoder_scheduler.load_state_dict(states['encoder_scheduler'])
            decoder_scheduler.load_state_dict(states['decoder_scheduler'])
        print_rank_0(f"Optimizer loaded from {load_path}")
        
    return encoder_optimizer, encoder_scheduler, decoder_optimizer, decoder_scheduler


def setup_ddp(args, models, optimizers):
    if args.fp16:
        models, optimizers = amp.initialize(models, optimizers, opt_level="O1")
    if args.local_rank != -1:
        models = [DDP(model, device_ids=[args.local_rank], output_device=args.local_rank) for model in models]
        print_rank_0("DDP setup finished") 
    return models, optimizers
              

def train_fn(train_loader, encoder, decoder, criterion, encoder_optimizer, decoder_optimizer, epoch,
             encoder_scheduler, decoder_scheduler, device, global_step, SUMMARY, args):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    # switch to train mode
    encoder.train()
    decoder.train()
    
    start = end = time.time()

    for step, (images, refs) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)
        images = images.to(device)
        labels = refs[args.format][0].to(device)
        label_lengths = refs[args.format][1].to(device)
        batch_size = images.size(0)
        features = encoder(images)
        predictions, caps_sorted, decode_lengths = decoder(features, labels, label_lengths)
        targets = caps_sorted[:, 1:]
        # multi-gpu: needs to sort
#         decode_lengths, sort_ind = decode_lengths.sort(dim=0, descending=True)
#         predictions = predictions[sort_ind]
#         targets = targets[sort_ind]
#         decode_lengths = decode_lengths.tolist()
        predictions = pack_padded_sequence(predictions, decode_lengths, batch_first=True).data
        targets = pack_padded_sequence(targets, decode_lengths, batch_first=True).data
        loss = criterion(predictions, targets)
        # record loss
        losses.update(loss.item(), batch_size)
        if args.gradient_accumulation_steps > 1:
            loss = loss / args.gradient_accumulation_steps
        if args.fp16:
            with amp.scale_loss(loss, [encoder_optimizer, decoder_optimizer]) as scaled_loss:
                scaled_loss.backward()
            encoder_grad_norm = torch.nn.utils.clip_grad_norm_(amp.master_params(encoder_optimizer), CFG.max_grad_norm)
            decoder_grad_norm = torch.nn.utils.clip_grad_norm_(amp.master_params(decoder_optimizer), CFG.max_grad_norm)
        else:
            loss.backward()
            encoder_grad_norm = torch.nn.utils.clip_grad_norm_(encoder.parameters(), CFG.max_grad_norm)
            decoder_grad_norm = torch.nn.utils.clip_grad_norm_(decoder.parameters(), CFG.max_grad_norm)
        if (step + 1) % args.gradient_accumulation_steps == 0:
            encoder_optimizer.step()
            decoder_optimizer.step()
            encoder_optimizer.zero_grad()
            decoder_optimizer.zero_grad()
            global_step += 1
        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        if step % CFG.print_freq == 0 or step == (len(train_loader)-1):
            print_rank_0('Epoch: [{0}][{1}/{2}] '
                  'Data {data_time.avg:.3f}s ({sum_data_time}) '
                  'Elapsed {remain:s} '
                  'Loss: {loss.val:.4f}({loss.avg:.4f}) '
                  'Enc Grad: {encoder_grad_norm:.4f}  '
                  'Dec Grad: {decoder_grad_norm:.4f}  '
                  'LR: {encoder_lr:.6f} {decoder_lr:.6f}  '
                  .format(
                   epoch+1, step, len(train_loader), batch_time=batch_time,
                   data_time=data_time, loss=losses,
                   sum_data_time=asMinutes(data_time.sum),
                   remain=timeSince(start, float(step+1)/len(train_loader)),
                   encoder_grad_norm=encoder_grad_norm,
                   decoder_grad_norm=decoder_grad_norm,
                   encoder_lr=encoder_scheduler.get_lr()[0],
                   decoder_lr=decoder_scheduler.get_lr()[0],
                   ))
            losses.reset()
            
    if isinstance(encoder_scheduler, CosineAnnealingLR):
        encoder_scheduler.step()
    elif isinstance(encoder_scheduler, CosineAnnealingWarmRestarts):
        encoder_scheduler.step()

    if isinstance(decoder_scheduler, CosineAnnealingLR):
        decoder_scheduler.step()
    elif isinstance(decoder_scheduler, CosineAnnealingWarmRestarts):
        decoder_scheduler.step()
        
    return losses.avg, global_step


def valid_fn(valid_loader, encoder, decoder, tokenizer, device):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    # switch to evaluation mode
    if hasattr(decoder, 'module'):
        encoder = encoder.module
        decoder = decoder.module
    encoder.eval()
    decoder.eval()
    text_preds = {}
    start = end = time.time()
    for step, (indices, images) in enumerate(valid_loader):
        # measure data loading time
        data_time.update(time.time() - end)
        images = images.to(device)
        batch_size = images.size(0)
        with torch.no_grad():
            features = encoder(images)
            predictions = decoder.predict(features)
        predicted_sequence = torch.argmax(predictions.detach().cpu(), -1).numpy()
        _text_preds = tokenizer.predict_captions(predicted_sequence)
#         text_preds.append(_text_preds)
        for idx, text in zip(indices.tolist(), _text_preds):
            text_preds[idx] = text
        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        if step % CFG.print_freq == 0 or step == (len(valid_loader)-1):
            print_rank_0('EVAL: [{0}/{1}] '
                  'Data {data_time.avg:.3f}s ({sum_data_time}) '
                  'Elapsed {remain:s} '
                  .format(
                   step, len(valid_loader), batch_time=batch_time,
                   data_time=data_time,
                   sum_data_time=asMinutes(data_time.sum),
                   remain=timeSince(start, float(step+1)/len(valid_loader)),
                   ))
#     text_preds = np.concatenate(text_preds)
    return text_preds


def train_loop(args, train_df, valid_df, tokenizer, save_path):
    
    SUMMARY = None
    
    if args.local_rank == 0 and not args.debug:
        # LOGGER = init_logger()
        SUMMARY = init_summary_writer(save_path)
        
    print_rank_0("========== training ==========")
        
    device = args.device
    os.makedirs(save_path, exist_ok=True)

    # ====================================================
    # loader
    # ====================================================

    train_dataset = TrainDataset(args, train_df, tokenizer, labelled=True)
    if args.local_rank != -1:
        train_sampler = DistributedSampler(train_dataset, shuffle=True)
    else:
        train_sampler = RandomSampler(train_dataset)
    train_loader = DataLoader(train_dataset, 
                              batch_size=args.batch_size, 
                              sampler=train_sampler,
                              num_workers=CFG.num_workers, 
                              pin_memory=True,
                              drop_last=True, 
                              collate_fn=bms_collate)

    # ====================================================
    # model & optimizer
    # ====================================================
    encoder, decoder = get_model(args, tokenizer, device, load_path=args.load_path)
    
    encoder_optimizer, encoder_scheduler, decoder_optimizer, decoder_scheduler = \
        get_optimizer_and_scheduler(args, encoder, decoder, load_path=args.load_path)
    
    if args.local_rank != -1 or args.fp16:
        [encoder, decoder], [encoder_optimizer, decoder_optimizer] = \
            setup_ddp(args, [encoder, decoder], [encoder_optimizer, decoder_optimizer])

    # ====================================================
    # loop
    # ====================================================
    criterion = nn.CrossEntropyLoss(ignore_index=tokenizer.stoi["<pad>"])

    best_score = np.inf
    best_loss = np.inf
    
    global_step = 0
    start_epoch = encoder_scheduler.last_epoch

    for epoch in range(start_epoch, args.epochs):
        
        if args.local_rank != -1:
            dist.barrier()
            train_sampler.set_epoch(epoch)
        
        start_time = time.time()
        encoder_lr = encoder_scheduler.get_lr()[0]
        decoder_lr = decoder_scheduler.get_lr()[0]
        
        # train
        avg_loss, global_step = train_fn(
            train_loader, encoder, decoder, criterion, encoder_optimizer, decoder_optimizer, epoch, 
            encoder_scheduler, decoder_scheduler, device, global_step, SUMMARY, args)
        
        # eval
        scores = inference(args, valid_df, tokenizer, encoder, decoder, save_path, split='valid')
        
        if args.local_rank != 0:
            continue
    
        elapsed = time.time() - start_time

        print_rank_0(f'Epoch {epoch+1} - avg_train_loss: {avg_loss:.4f}  time: {elapsed:.0f}s')
        print_rank_0(f'Epoch {epoch+1} - Score: ' + json.dumps(scores))
        
        if SUMMARY:
            SUMMARY.add_scalar('train/loss', avg_loss, epoch)
            SUMMARY.add_scalar('train/encoder_lr', encoder_lr, epoch)
            SUMMARY.add_scalar('train/decoder_lr', decoder_lr, epoch)
            for key in scores:
                SUMMARY.add_scalar(f'valid/{key}', scores[key], epoch)

        save_obj = {'encoder': encoder.state_dict(), 
                    'encoder_optimizer': encoder_optimizer.state_dict(), 
                    'encoder_scheduler': encoder_scheduler.state_dict(), 
                    'decoder': decoder.state_dict(),
                    'decoder_optimizer': decoder_optimizer.state_dict(), 
                    'decoder_scheduler': decoder_scheduler.state_dict(),
                    'global_step': global_step
                   }
        torch.save(save_obj, os.path.join(save_path, f'{args.encoder}_{args.decoder}_ep{epoch}.pth'))

        score = scores['inchi'] if args.format == 'inchi' else scores['smiles']

        if score < best_score:
            best_score = score
            print_rank_0(f'Epoch {epoch+1} - Save Best Score: {best_score:.4f} Model')
            torch.save(save_obj, os.path.join(save_path, f'{args.encoder}_{args.decoder}_best.pth'))
    
    if args.local_rank != -1:
        dist.barrier()


def inference(args, data_df, tokenizer, encoder=None, decoder=None, save_path=None, split='test'):
    
    print_rank_0("========== inference ==========")
    
    device = args.device

    dataset = TrainDataset(args, data_df, tokenizer, labelled=False)
    if args.local_rank != -1:
        sampler = DistributedSampler(dataset, shuffle=False)
    else:
        sampler = RandomSampler(dataset)
    dataloader = DataLoader(dataset, 
                            batch_size=args.batch_size * 2, 
                            sampler=sampler, 
                            num_workers=CFG.num_workers, 
                            pin_memory=True, 
                            drop_last=False)
    
    if encoder is None or decoder is None:
        encoder, decoder = get_model(args, tokenizer, device, save_path, ddp=True)
    
    local_preds = valid_fn(dataloader, encoder, decoder, tokenizer, device)
    all_preds = [None for i in range(dist.get_world_size())]
    dist.all_gather_object(all_preds, local_preds)
    
    if args.local_rank != 0:
        return
    
    text_preds = [None for i in range(len(dataset))]
    for preds in all_preds:
        for idx, text in preds.items():
            text_preds[idx] = text

    pred_df = data_df[['image_id']].copy()
    
    if args.format == 'inchi':
        # InChI
        pred_df['InChI'] = [f"InChI=1S/{text}" for text in text_preds]
    else:
        # SMILES
        pred_df['SMILES'] = text_preds
        print('Converting SMILES to InChI ...')
        inchi_list, r_success = batch_convert_smiles_to_inchi(text_preds)
        pred_df['InChI'] = inchi_list
        print(f'{split} SMILES to InChI success ratio: {r_success:.4f}')
        
    # Compute scores
    scores = {}
    if split == 'valid':
        scores['inchi'], scores['inchi_em'] = get_score(data_df['InChI'].values, pred_df['InChI'].values)
        if args.format != 'inchi':
            scores['smiles'], scores['smiles_em'] = get_score(data_df['SMILES'].values, pred_df['SMILES'].values)
            print('label:')
            print(data_df['SMILES'].values[:4])
            print('pred:')
            print(pred_df['SMILES'].values[:4])
            
    # Save predictions
    if split == 'test':
        pred_df[['image_id', 'InChI']].to_csv(os.path.join(save_path, 'submission.csv'), index=False)
    else:
        fields = ['image_id', 'InChI']
        if args.format != 'inchi':
            fields.append('SMILES')
        pred_df[fields].to_csv(os.path.join(save_path, f'prediction_{split}.csv'), index=False)

    return scores
            

def get_train_file_path(image_id):
    return "data/train/{}/{}/{}/{}.png".format(image_id[0], image_id[1], image_id[2], image_id)

def get_test_file_path(image_id):
    return "data/test/{}/{}/{}/{}.png".format(image_id[0], image_id[1], image_id[2], image_id)


def main():

    args = get_args()
    seed_torch(seed=CFG.seed)
    
    args.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    if args.local_rank != -1:
        dist.init_process_group(backend='nccl', init_method='env://')
        torch.cuda.set_device(args.local_rank)
        torch.backends.cudnn.benchmark = True

    if args.do_train:
        train_df = pd.read_csv('data/train_folds.csv')
        train_df['file_path'] = train_df['image_id'].apply(get_train_file_path)
        print_rank_0(f'train.shape: {train_df.shape}')
    
    if args.do_train or args.do_valid:
        valid_df = pd.read_csv('data/valid_folds.csv')
        valid_df['file_path'] = valid_df['image_id'].apply(get_train_file_path)
        print_rank_0(f'valid.shape: {valid_df.shape}')
        if args.concat_valid:
            train_df = pd.concat([train_df, valid_df])
    
    if args.do_test:
        test_df = pd.read_csv('data/sample_submission.csv')
        test_df['file_path'] = test_df['image_id'].apply(get_test_file_path)
        print_rank_0(f'test.shape: {test_df.shape}')

    if args.debug:
#         args.epochs = 5
        args.save_path = 'output/debug'
        CFG.print_freq = 50
#         if args.do_train:
#             train_df = train_df.sample(n=10000, random_state=CFG.seed).reset_index(drop=True)
        if args.do_train or args.do_valid:
            valid_df = valid_df.sample(n=1000, random_state=CFG.seed).reset_index(drop=True)
        if args.do_test:
            test_df = test_df.sample(n=1000, random_state=CFG.seed).reset_index(drop=True)

    tokenizer = torch.load('data/' + FORMAT_INFO[args.format]['tokenizer'])

    if args.do_train:
        train_loop(args, train_df, valid_df, tokenizer, args.save_path)
        
    if args.do_valid:
        scores = inference(args, valid_df, tokenizer, save_path=args.save_path, split='valid')
        print_rank_0(scores)

    if args.do_test:
        inference(args, test_df, tokenizer, save_path=args.save_path, split='test')


if __name__ == "__main__":
    main()
