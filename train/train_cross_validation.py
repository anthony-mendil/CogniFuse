import os
import json
import subprocess
from collections import OrderedDict

from utils.fixseed import fixseed
from utils.parser_util import train_args, is_multimodal, get_pass_through_args
from utils.eval_util import cross_validate, save_args
from utils.model_util import create_model


def main():
    #multimodal = is_multimodal()
    #if multimodal:
    args = train_args(cross_validate=True)
    save_args(args, create_model)
    #else:
        #args = unimodal_deformer_train_args(cross_validate=True)
    #    args = train_args(cross_validate=True)
    #    save_args(args, create_model)

    fixseed(args.seed)

    # Determine the folds
    folds = [d for d in os.listdir(args.data_dir)
             if os.path.isdir(os.path.join(args.data_dir, d))]

    pass_through_args = get_pass_through_args(args)

    # Training
    for f in folds:
        subprocess.run(args=[
            "python", "-m",
            "train.train",
            "--fold", f,
            *pass_through_args
        ])

    best_fold_checkpoints = cross_validate(folds, args.save_dir)

    best_fold_checkpoints = OrderedDict(sorted(best_fold_checkpoints.items()))
    val_save_path = os.path.join(args.save_dir, 'cross_validation.json')
    with open(val_save_path, 'w') as fw:
        json.dump(best_fold_checkpoints, fw, indent=4)


if __name__ == '__main__':
    main()
