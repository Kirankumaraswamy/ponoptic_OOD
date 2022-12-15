import argparse
import time
import os, sys

import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim

from torch.utils.data import DataLoader
from detectron2.engine import DefaultTrainer, default_argument_parser, default_setup, launch, create_ddp_model
from detectron2.projects.panoptic_deeplab import (
    add_panoptic_deeplab_config
)
from detectron2.projects.deeplab import add_deeplab_config
from detectron2.config import get_cfg
from dataset.cityscapes_ood import CityscapesOOD
import config
from detectron2.modeling import build_model
from detectron2.checkpoint import DetectionCheckpointer

from torchvision.transforms import Compose, RandomHorizontalFlip, Normalize, ToTensor
from torch.nn.parallel import DistributedDataParallel
import detectron2.utils.comm as comm
from detectron2.projects.deeplab import build_lr_scheduler
import warnings

warnings.filterwarnings('ignore')
from panoptic_evaluation.evaluation import data_load, data_evaluate
import matplotlib.pyplot as plt
import tqdm
from torch.utils.data.distributed import DistributedSampler
from detectron2.solver import get_default_optimizer_params
from detectron2.solver.build import maybe_add_gradient_clipping
import detectron2.data.transforms as T
import matplotlib.pyplot as plt


def panoptic_deep_lab_collate(batch):
    data = [item[0] for item in batch]
    target = [item[1] for item in batch]
    # target = torch.stack(target)
    return data, target


def build_sem_seg_train_aug(cfg):
    augs = [
        T.ResizeShortestEdge(
            cfg.INPUT.MIN_SIZE_TRAIN, cfg.INPUT.MAX_SIZE_TRAIN, cfg.INPUT.MIN_SIZE_TRAIN_SAMPLING
        )
    ]

    if cfg.INPUT.CROP.ENABLED:
        augs.append(T.RandomCrop(cfg.INPUT.CROP.TYPE, cfg.INPUT.CROP.SIZE))

    augs.append(T.RandomFlip())
    return []


def panoptic_deep_lab_val_collate(batch):
    data = [item for item in batch]
    return data


def eval_metric_model(network):
    loss = None
    sys.stdout = open(os.devnull, 'w')
    sys.tracebacklimit = 0
    ds = data_load(root=config.cityscapes_eval_path, split="val", transform=None)
    result = data_evaluate(estimator=network, evaluation_dataset=ds,
                           collate_fn=panoptic_deep_lab_val_collate, semantic_only=False, evaluate_ood=False)
    sys.stdout = sys.__stdout__
    return result


def calculate_statistics(args, network, dataset_cfg):
    print("Evaluate statistics: ", comm.get_local_rank())
    ckpt_path = config.ckpt_path

    checkpointer = DetectionCheckpointer(
        network
    )

    if ckpt_path is not None:
        print("Checkpoint file:", ckpt_path)
        checkpointer.resume_or_load(
            ckpt_path, resume=False
        )
    else:
        raise("Check point file cannot be None.")

    dataset_train = CityscapesOOD(root=config.cityscapes_ood_path, split=config.split, cfg=dataset_cfg,
                                  transform=build_sem_seg_train_aug(dataset_cfg))

    start = time.time()
    # network = torch.nn.DataParallel(network).cuda()
    network = network.cuda()

    dataloader_train = DataLoader(dataset_train, batch_size=config.batch_size, shuffle=False,
                                  collate_fn=panoptic_deep_lab_collate, num_workers=0)
    network.eval()

    pred_list = None
    target_list = None
    max_class_mean = {}
    print("Calculating statistics...")

    statistics_file_name = config.statistics_file_name

    for i, (x, target) in enumerate(dataloader_train):
        # print("Train : len of data loader: ", len(dataloader_train), comm.get_rank())

        output_list = []
        with torch.no_grad():
            outputs = network(x)

        for output in outputs:
            output_list.append(output['sem_score'])

        for index, output in enumerate(outputs):
            if pred_list is None:
                pred_list = output['sem_score'].cpu().unsqueeze(dim=0)
                target_list = x[index]["sem_seg"].unsqueeze(dim=0)
            else:
                pred_list = torch.cat((pred_list, output['sem_score'].cpu().unsqueeze(dim=0)), 0)
                target_list = torch.cat((target_list, x[index]["sem_seg"].unsqueeze(dim=0)), 0)

        del outputs, output
        torch.cuda.empty_cache()
        print("batch: ", i)

        if i % 200 == 199 or i == len(dataloader_train) - 1:
            break

    pred_list = pred_list.permute(0, 2, 3, 1)
    pred_score, prediction = pred_list.max(3)
    in_dist = []
    in_dist_var = []
    in_dist_rest = []
    in_dist_rest_var = []
    out_dist = []
    out_dist_var = []
    out_dist_rest = []
    out_dist_rest_var = []
    void_dist = []
    void_dist_var = []
    void_dist_rest = []
    void_dist_rest_var = []

    correct_pred = target_list == prediction
    for c in range(19):

        class_pred = prediction == c

        correct_class_pred = torch.logical_and(correct_pred, class_pred)
        in_dist.append(pred_score[correct_class_pred].mean().item())
        in_dist_var.append(pred_score[correct_class_pred].std().item())

        right_probabilities = pred_list[correct_class_pred][:, c]
        sum_all_probabilities = pred_list[correct_class_pred].sum(axis=1)

        sum_non_class_probabilities = sum_all_probabilities - right_probabilities
        in_dist_rest.append(sum_non_class_probabilities.mean().item())
        in_dist_rest_var.append(sum_non_class_probabilities.std().item())

    threshold = np.array(in_dist) - np.array(in_dist_var)

    print("Threshold values: ")
    print(threshold)
    np.save(f'./stats/{statistics_file_name}_threshold.npy', threshold)

    print(prediction.size())


    out_list = target_list == 19

    for c in range(19):
        class_pred = prediction == c
        out_class_pred = torch.logical_and(out_list, class_pred)
        out_dist.append(pred_score[out_class_pred].mean().item())
        out_dist_var.append(pred_score[out_class_pred].std().item())

        sum_all_probabilities = pred_list[out_class_pred].sum(axis=1)
        sum_non_class_probabilities = sum_all_probabilities - pred_score[out_class_pred]
        out_dist_rest.append(sum_non_class_probabilities.mean().item())
        out_dist_rest_var.append(sum_non_class_probabilities.std().item())

    void_list = target_list == 255

    for c in range(19):
        class_pred = prediction == c
        void_class_pred = torch.logical_and(void_list, class_pred)
        void_dist.append(pred_score[void_class_pred].mean().item())
        void_dist_var.append(pred_score[void_class_pred].std().item())

        sum_all_probabilities = pred_list[void_class_pred].sum(axis=1)
        sum_non_class_probabilities = sum_all_probabilities - pred_score[void_class_pred]
        void_dist_rest.append(sum_non_class_probabilities.mean().item())
        void_dist_rest_var.append(sum_non_class_probabilities.std().item())
    x = [i for i in range(19)]
    plt.errorbar(x, in_dist, in_dist_var, linestyle='None', marker='^')
    plt.errorbar(x, in_dist_rest, in_dist_rest_var, linestyle='None', marker='^')
    plt.title("In distribution")
    plt.savefig(f"./indistribution_{statistics_file_name}.py")

    plt.errorbar(x, out_dist, out_dist_var, linestyle='None', marker='^')
    plt.errorbar(x, out_dist_rest, out_dist_rest_var, linestyle='None', marker='^')
    plt.title("Out distribution")
    plt.savefig(f"./ood_{statistics_file_name}.py")

    plt.errorbar(x, void_dist, void_dist_var, linestyle='None', marker='^')
    plt.errorbar(x, void_dist_rest, void_dist_rest_var, linestyle='None', marker='^')
    plt.title("void distribution")
    plt.savefig(f"./void_{statistics_file_name}.py")




    a = 1


    '''print(f"sum all class mean: {mean_sum_all_dict}")
    print(f"sum all class var: {var_sum_all_dict}")
    print("======================")
    print(f"correct class mean: {mean_class_dict}")
    print(f"correct class var: {var_class_dict}")
    print("======================")
    print(f"sum all non class mean: {mean_sum_non_class_dict}")
    print(f"sum all non class var: {var_sum_non_class_dict}")
    print("======================")

    np.save(f'./stats/sum_all_{statistics_file_name}_mean.npy', mean_sum_all_dict)
    np.save(f'./stats/sum_all_{statistics_file_name}_var.npy', var_sum_all_dict)
    np.save(f'./stats/correct_class_{statistics_file_name}_mean.npy', mean_sum_all_dict)
    np.save(f'./stats/correct_class_{statistics_file_name}_var.npy', var_sum_all_dict)
    np.save(f'./stats/sum_non_class_{statistics_file_name}_mean.npy', mean_sum_all_dict)
    np.save(f'./stats/sum_non_class_{statistics_file_name}_var.npy', var_sum_all_dict)'''

    end = time.time()
    hours, rem = divmod(end - start, 3600)
    minutes, seconds = divmod(rem, 60)
    print("FINISHED {:0>2}:{:0>2}:{:05.2f}".format(int(hours), int(minutes), seconds))


def main(args):
    # load configuration from cfg files for detectron2
    cfg = get_cfg()
    add_panoptic_deeplab_config(cfg)
    cfg.merge_from_file(config.detectron_config_file_path)
    cfg.freeze()
    default_setup(
        cfg, args
    )

    network = build_model(cfg)
    print("Model:\n{}".format(network))

    """calculate_statistics"""
    calculate_statistics(args, network, cfg)


if __name__ == '__main__':
    """Get Arguments and setup config class"""
    parser = argparse.ArgumentParser(description='OPTIONAL argument setting, see also config.py')
    parser.add_argument("-train", "--TRAINSET", nargs="?", type=str)
    parser.add_argument("-model", "--MODEL", nargs="?", type=str)
    parser.add_argument("-epoch", "--training_starting_epoch", nargs="?", type=int)
    parser.add_argument("-nepochs", "--num_training_epochs", nargs="?", type=int)
    parser.add_argument("-alpha", "--pareto_alpha", nargs="?", type=float)
    parser.add_argument("-lr", "--learning_rate", nargs="?", type=float)
    parser.add_argument("-crop", "--crop_size", nargs="?", type=int)

    # use detectron2 distributed args
    args = default_argument_parser().parse_args()
    # args from current file
    args.default_args = vars(parser.parse_args())
    args.dist_url = 'tcp://127.0.0.1:64486'
    print("Command Line Args:", args)


    main(args)
