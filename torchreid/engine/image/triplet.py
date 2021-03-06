from __future__ import division, print_function, absolute_import
import time
import datetime

from torchreid import metrics
from torchreid.utils import (AverageMeter, open_all_layers,
                             open_specified_layers)
from torchreid.losses import TripletLoss, CrossEntropyLoss, CenterLoss, RangeLoss

from ..engine import Engine
import torch


class ImageTripletEngine(Engine):
    def __init__(self,
                 datamanager,
                 model,
                 optimizer,
                 margin=0.3,
                 weight_t=1,
                 weight_x=1,
                 weight_c=0,
                 scheduler=None,
                 use_gpu=True,
                 label_smooth=True):
        super(ImageTripletEngine, self).__init__(datamanager, model, optimizer,
                                                 scheduler, use_gpu)

        self.weight_t = weight_t
        self.weight_x = weight_x
        self.weight_c = weight_c

        self.criterion_t = TripletLoss(margin=margin)
        self.criterion_x = CrossEntropyLoss(
            num_classes=self.datamanager.num_train_pids,
            use_gpu=self.use_gpu,
            label_smooth=label_smooth)

        if self.weight_c != 0:
            self.criterion_c = CenterLoss(
                num_classes=self.datamanager.num_train_pids, feat_dim=512)

            self.criterion_ca = CenterLoss(
                num_classes=self.datamanager.num_train_pids, feat_dim=64)

            self.criterion_cb = CenterLoss(
                num_classes=self.datamanager.num_train_pids, feat_dim=96)

            self.criterion_cc = CenterLoss(
                num_classes=self.datamanager.num_train_pids, feat_dim=128)

    def train(self,
              epoch,
              max_epoch,
              writer,
              print_freq=10,
              fixbase_epoch=0,
              open_layers=None):
        losses_t = AverageMeter()
        losses_x = AverageMeter()
        accs = AverageMeter()
        batch_time = AverageMeter()
        data_time = AverageMeter()

        self.model.train()
        if (epoch + 1) <= fixbase_epoch and open_layers is not None:
            print('* Only train {} (epoch: {}/{})'.format(
                open_layers, epoch + 1, fixbase_epoch))
            open_specified_layers(self.model, open_layers)
        else:
            open_all_layers(self.model)

        num_batches = len(self.train_loader)
        end = time.time()
        for batch_idx, data in enumerate(self.train_loader):
            data_time.update(time.time() - end)

            imgs, pids = self._parse_data_for_train(data)
            if self.use_gpu:
                imgs = imgs.cuda()
                pids = pids.cuda()

            self.optimizer.zero_grad()

            outputs, features = self.model(imgs)
            loss_t = self._compute_loss(self.criterion_t, features, pids)
            loss_x = self._compute_loss(self.criterion_x, outputs, pids)
            if self.weight_c != 0:
                loss_c = self._compute_loss(self.criterion_c, features[0],
                                            pids)
                loss_ca = self._compute_loss(self.criterion_ca, features[1],
                                             pids)
                loss_cb = self._compute_loss(self.criterion_cb, features[2],
                                             pids)
                loss_cc = self._compute_loss(self.criterion_cc, features[3],
                                             pids)
                loss_c = loss_c + loss_ca + loss_cb + loss_cc
            else:
                self.weight_c = 0
                loss_c = 0

            loss = self.weight_t * loss_t + self.weight_x * loss_x + self.weight_c * loss_c
            loss.backward()
            self.optimizer.step()

            batch_time.update(time.time() - end)

            losses_t.update(loss_t.item(), pids.size(0))
            losses_x.update(loss_x.item(), pids.size(0))
            accs.update(metrics.accuracy(outputs, pids)[0].item())

            if (batch_idx + 1) % print_freq == 0:
                # estimate remaining time
                eta_seconds = batch_time.avg * (num_batches - (batch_idx + 1) +
                                                (max_epoch -
                                                 (epoch + 1)) * num_batches)
                eta_str = str(datetime.timedelta(seconds=int(eta_seconds)))
                print('Epoch: [{0}/{1}][{2}/{3}]\t'
                      'Loss_t {loss_t.val:.4f} ({loss_t.avg:.4f})\t'
                      'Loss_x {loss_x.val:.4f} ({loss_x.avg:.4f})\t'
                      'Acc {acc.val:.2f} ({acc.avg:.2f})\t'
                      'Lr {lr:.6f}\t'
                      'eta {eta}'.format(
                          epoch + 1,
                          max_epoch,
                          batch_idx + 1,
                          num_batches,
                          loss_t=losses_t,
                          loss_x=losses_x,
                          acc=accs,
                          lr=self.optimizer.param_groups[0]['lr'],
                          eta=eta_str))

            if writer is not None:
                n_iter = epoch * num_batches + batch_idx
                writer.add_scalar('Train/Time', batch_time.avg, n_iter)
                writer.add_scalar('Train/Data', data_time.avg, n_iter)
                writer.add_scalar('Train/Loss_t', losses_t.avg, n_iter)
                writer.add_scalar('Train/Loss_x', losses_x.avg, n_iter)
                writer.add_scalar('Train/Acc', accs.avg, n_iter)
                writer.add_scalar('Train/Lr',
                                  self.optimizer.param_groups[0]['lr'], n_iter)

            end = time.time()

        if self.scheduler is not None:
            self.scheduler.step()
