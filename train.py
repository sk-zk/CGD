import argparse
import os
import math

import pandas as pd
import torch
from thop import profile, clever_format
from torch.optim import Adam
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.data import DataLoader
from tqdm import tqdm

from model import Model, set_bn_eval
from utils import recall, LabelSmoothingCrossEntropyLoss, BatchHardTripletLoss, ImageReader, MPerClassSampler


def train(net, optim):
    net.train()
    # fix bn on backbone network
    net.apply(set_bn_eval)
    total_loss, total_correct, total_num, data_bar = 0, 0, 0, tqdm(train_data_loader)
    for inputs, labels in data_bar:
        inputs, labels = inputs.cuda(), labels.cuda()
        features, classes = net(inputs)
        class_loss = class_criterion(classes, labels)
        if features.isnan().any():
            raise ValueError
        if classes.isnan().any():
            raise ValueError
        feature_loss = feature_criterion(features, labels)
        loss = class_loss + feature_loss
        optim.zero_grad()
        loss.backward()
        optim.step()
        pred = torch.argmax(classes, dim=-1)
        total_loss += loss.item() * inputs.size(0)
        total_correct += torch.sum(pred == labels).item()
        total_num += inputs.size(0)
        data_bar.set_description('Train Epoch {}/{} - Loss:{:.4f} - Acc:{:.2f}%'
                                 .format(epoch, num_epochs, total_loss / total_num, total_correct / total_num * 100))

    return total_loss / total_num, total_correct / total_num * 100


def test(net, recall_ids):
    net.eval()
    with torch.no_grad():
        # obtain feature vectors for all data
        for key in eval_dict.keys():
            eval_dict[key]['features'] = []
            for inputs, labels in tqdm(eval_dict[key]['data_loader'], desc='processing {} data'.format(key)):
                inputs, labels = inputs.cuda(), labels.cuda()
                features, classes = net(inputs)
                eval_dict[key]['features'].append(features)
            eval_dict[key]['features'] = torch.cat(eval_dict[key]['features'], dim=0)

        # compute recall metric
        acc_list = recall(eval_dict['test']['features'], test_data_set.labels, recall_ids)
    desc = 'Test Epoch {}/{} '.format(epoch, num_epochs)
    for index, rank_id in enumerate(recall_ids):
        desc += 'R@{}:{:.2f}% '.format(rank_id, acc_list[index] * 100)
        results['test_recall@{}'.format(rank_id)].append(acc_list[index] * 100)
    print(desc)
    return acc_list[0]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train CGD')
    parser.add_argument('--data', default='', type=str, help='dataset path')
    parser.add_argument('--name', default='run', type=str, help='run name')
    parser.add_argument('--backbone_type', default='resnet50', type=str, choices=['resnet50', 'resnext50'],
                        help='backbone network type')
    parser.add_argument('--gd_config', default='SG', type=str,
                        choices=['S', 'M', 'G', 'SM', 'MS', 'SG', 'GS', 'MG', 'GM', 'SMG', 'MSG', 'GSM'],
                        help='global descriptors config')
    parser.add_argument('--feature_dim', default=1536, type=int, help='feature dim')
    parser.add_argument('--smoothing', default=0.1, type=float, help='smoothing value for label smoothing')
    parser.add_argument('--temperature', default=0.5, type=float,
                        help='temperature scaling used in softmax cross-entropy loss')
    parser.add_argument('--margin', default=0.1, type=float, help='margin of m for triplet loss')
    parser.add_argument('--recalls', default='1,2,4,8', type=str, help='selected recall')
    parser.add_argument('--batch_size', default=128, type=int, help='train batch size')
    parser.add_argument('--num_epochs', default=20, type=int, help='train epoch number')
    parser.add_argument('--checkpoint', default='', type=str, help='resume from saved checkpoint')
    parser.add_argument('--lr', default=1e-4, type=float, help='learning rate')
    parser.add_argument('--workers', default=8, type=float, help='dataloader num_workers')

    opt = parser.parse_args()
    # args parse
    data_path, run_name, backbone_type = opt.data, opt.name, opt.backbone_type
    gd_config, feature_dim, smoothing, temperature = opt.gd_config, opt.feature_dim, opt.smoothing, opt.temperature
    margin, recalls, batch_size = opt.margin, [int(k) for k in opt.recalls.split(',')], opt.batch_size
    num_epochs = opt.num_epochs
    prev_checkpoint_path = opt.checkpoint
    learning_rate = opt.lr
    num_workers = opt.workers
    save_name_pre = '{}_{}_{}_{}_{}_{}_{}_{}'.format(run_name, backbone_type, gd_config, feature_dim,
                                                        smoothing, temperature, margin, batch_size)

    results = {'train_loss': [], 'train_accuracy': []}
    for recall_id in recalls:
        results['test_recall@{}'.format(recall_id)] = []

    # dataset loader
    train_data_set = ImageReader(os.path.join(data_path, "train"), 'train')
    train_sample = MPerClassSampler(train_data_set.labels, batch_size)
    train_data_loader = DataLoader(train_data_set, batch_sampler=train_sample, num_workers=num_workers)
    test_data_set = ImageReader(os.path.join(data_path, "val"), 'val')
    test_data_loader = DataLoader(test_data_set, batch_size, shuffle=False, num_workers=num_workers)
    eval_dict = {'test': {'data_loader': test_data_loader}}

    # model setup, model profile, optimizer config and loss definition
    model = Model(backbone_type, gd_config, feature_dim, num_classes=len(train_data_set.classes)).cuda()
    flops, params = profile(model, inputs=(torch.randn(1, 3, 224, 224).cuda(),))
    flops, params = clever_format([flops, params])
    print('# Model Params: {} FLOPs: {}'.format(params, flops))
    optimizer = Adam(model.parameters(), lr=learning_rate)
    lr_scheduler = MultiStepLR(optimizer, milestones=[int(0.6 * num_epochs), int(0.8 * num_epochs)], gamma=0.1)
    class_criterion = LabelSmoothingCrossEntropyLoss(smoothing=smoothing, temperature=temperature)
    feature_criterion = BatchHardTripletLoss(margin=margin)

    start_epoch = 1

    # load checkpoint if given
    if os.path.exists(prev_checkpoint_path):
        checkpoint = torch.load(prev_checkpoint_path)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch'] + 1
        results = checkpoint['results']       
        del checkpoint
        
    best_recall = 0.0
    for epoch in range(start_epoch, num_epochs + 1):
        train_loss, train_accuracy = train(model, optimizer)
        results['train_loss'].append(train_loss)
        results['train_accuracy'].append(train_accuracy)
        rank = test(model, recalls)
        lr_scheduler.step()

        # save statistics
        data_frame = pd.DataFrame(data=results, index=range(1, epoch + 1))
        data_frame.to_csv('results/{}_statistics.csv'.format(save_name_pre), index_label='epoch')
        # save database and model
        data_base = {}
        if rank > best_recall:
            best_recall = rank
            data_base['test_images'] = test_data_set.images
            data_base['test_labels'] = test_data_set.labels
            data_base['test_features'] = eval_dict['test']['features']
            torch.save(model.state_dict(), 'results/{}_model.pth'.format(save_name_pre))
            torch.save(data_base, 'results/{}_data_base.pth'.format(save_name_pre))
        
        # save checkpoint
        torch.save({
            'epoch': epoch,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'results': results,
            }, 'results/{}_checkpoint.pth'.format(save_name_pre))
