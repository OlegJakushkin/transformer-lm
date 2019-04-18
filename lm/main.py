import json
import os
from pathlib import Path
import statistics
import shutil
import sys

import attr
import fire
import json_log_plots
import numpy as np
import torch.cuda
import torch.distributed
import torch.backends.cudnn as cudnn
import torch.multiprocessing as mp
from torch import nn, optim
import tqdm
import sentencepiece as spm

from .fire_utils import only_allow_defined_args, get_defined_args
from .model import Model, HParams


def main(
        run_path,
        dataset_path,
        sp_model_path,
        epochs=10,
        lr=2.5e-4,
        batch_size=2,  # per GPU
        g_accum_gradients=None,  # accumulate gradients N times (globally)
        n_ctx=1024,
        n_embed=768,
        n_head=12,
        n_layer=12,
        n_hidden=None,  # equal to n_embed by default (better leave at None)
        clean=False,  # clean run folder
        log_every=1,
        save_every=1000,
        validate_every=None,  # same as save_every by default
        only_validate=False,
        max_tokens=None,
        master_port='40390',
        master_addr='127.0.0.1',
        # These are set automatically when multiple GPUs are available
        device_id=None,
        n_devices=None,
        ):
    if n_devices is None:
        n_devices = torch.cuda.device_count()
        if n_devices > 1:
            locals_ = locals()
            kwargs = {a: locals_[a] for a in get_defined_args(main)}
            mp.spawn(_main_mp, (kwargs,), n_devices)
            return

    is_main = device_id in {0, None}
    world_size = max(1, n_devices)
    if g_accum_gradients is None:
        g_accum_gradients = world_size
    assert g_accum_gradients % world_size == 0
    accum_gradients = g_accum_gradients // world_size
    if validate_every is None:
        validate_every = save_every

    run_path = Path(run_path)
    if is_main:
        run_path_mark = run_path / '.lm'
        if clean and run_path.exists():
            assert run_path_mark.exists()  # to avoid removing unrelated folder
            shutil.rmtree(run_path)
        run_path.mkdir(exist_ok=True, parents=True)
        run_path_mark.touch()

    sp_model = spm.SentencePieceProcessor()
    sp_model.load(sp_model_path)

    hparams = HParams(
        n_vocab=len(sp_model),
        n_ctx=n_ctx,
        n_embed=n_embed,
        n_hidden=n_hidden or n_embed,
        n_head=n_head,
        n_layer=n_layer,
    )
    params = dict(
        hparams=attr.asdict(hparams),
        argv=' '.join(sys.argv),
        epochs=epochs,
        lr=lr,
        batch_size=batch_size,
        g_accum_gradients=g_accum_gradients,
    )
    params_s = json.dumps(params, indent=4, sort_keys=True, ensure_ascii=False)
    if is_main:
        print(params_s)
        (run_path / 'params.json').write_text(params_s, encoding='utf8')

    dataset_path = Path(dataset_path)
    print(f'Loading dataset from {dataset_path}')
    valid_dataset = np.load(dataset_path / 'valid.npy')
    train_dataset = np.load(dataset_path / 'train.npy')
    print(f'Train dataset has {len(train_dataset):,} tokens')
    print(f'Validation dataset has {len(valid_dataset):,} tokens')

    if torch.cuda.is_available():
        device = torch.device('cuda', index=device_id)
    else:
        device = torch.device('cpu')
    model = Model(hparams).to(device)
    cross_entropy = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=lr)
    loss_meter = AverageMeter()
    cudnn.benchmark = True

    if device_id is not None:
        print(f'device {device} initializing process group')
        os.environ['MASTER_PORT'] = master_port
        os.environ['MASTER_ADDR'] = master_addr
        torch.distributed.init_process_group(
            backend='nccl', rank=device_id, world_size=world_size)
        model = nn.parallel.DistributedDataParallel(
            model, device_ids=[device_id], output_device=device_id)
        print(f'process group for {device} initialized')

    step = 1
    step_tokens = n_ctx * batch_size * g_accum_gradients  # all GPUs
    epoch_size = len(train_dataset) // step_tokens  # all GPUs
    loss_scale = n_ctx * batch_size * accum_gradients / (512 * 4 * 32)

    def loss_fn(logits, ctx):
        return cross_entropy(
            input=logits[:, :-1].reshape([-1, logits.shape[-1]]),
            target=ctx[:, 1:].reshape(-1))

    def train_step():
        """ Train step on one GPU.
        """
        context = _gen_training_batch(
            train_dataset, n_ctx=n_ctx, batch_size=batch_size * accum_gradients)
        context = torch.LongTensor(context)
        optimizer.zero_grad()
        for ctx in torch.split(context, batch_size):
            ctx = ctx.to(device=device)
            logits = model(ctx)['logits']
            loss = loss_fn(logits, ctx)
            (loss * loss_scale).backward()
            loss_meter.update(float(loss.item()))
        optimizer.step()

    def train():
        nonlocal step
        for epoch in tqdm.trange(1, epochs + 1, desc='epoch',
                                 dynamic_ncols=True, disable=not is_main):
            epoch_pbar = tqdm.trange(epoch_size, desc=f'epoch {epoch}',
                                     dynamic_ncols=True, disable=not is_main)
            for _ in epoch_pbar:
                if step % save_every == 0:
                    save()
                if max_tokens and step * step_tokens >= max_tokens:
                    print(f'max_tokens {max_tokens} reached, '
                          f'saving and exiting')
                    save()
                    validate()
                    return
                train_step()
                step += 1
                epoch_pbar.set_postfix({
                    'step': step,
                    'loss': f'{loss_meter.mean():.2f}'})
                if is_main and step % log_every == 0:
                    json_log_plots.write_event(
                        run_path, step=step * step_tokens,
                        loss=loss_meter.mean())
                    loss_meter.reset()
                if step % validate_every == 0:
                    validate()
            # end of epoch
            save()
            validate()

    def validate():
        if not is_main:
            return
        json_log_plots.write_event(
            run_path, step=step * step_tokens,
            valid_loss=get_valid_loss())

    def get_valid_loss():
        """ Run validation, return mean loss. This is a pessimistic score,
        as validation contexts are non-overlapping.
        """
        # TODO how will this work with multi-GPU?
        model.eval()
        losses = AverageMeter()
        with torch.no_grad():
            for ctx in _valid_batch_iter(
                    valid_dataset, batch_size=batch_size, n_ctx=n_ctx):
                ctx = torch.LongTensor(ctx).to(device)
                logits = model(ctx)['logits']
                loss = loss_fn(logits, ctx)
                losses.update(float(loss.item()))
        model.train()
        return losses.mean()

    def save():
        if not is_main:
            return
        model_path = run_path / 'model.pt'
        optim_path = run_path / 'optim.pt'
        for path in [model_path, optim_path]:
            if path.exists():
                shutil.copy(path, run_path / f'{path.stem}-prev{path.suffix}')
        torch.save({'state_dict': model.state_dict(), 'step': step}, model_path)
        torch.save(optimizer.state_dict(), optim_path)

    if only_validate:
        if is_main:
            print('Validation loss: {validate():.4f}')
    else:
        try:
            train()
        except KeyboardInterrupt:
            if is_main:
                print('Interrupted, saving')
                save()
                sys.exit(1)


def _gen_training_batch(dataset: np.ndarray, n_ctx: int, batch_size: int):
    indices = [np.random.randint(0, len(dataset) - n_ctx)
               for _ in range(batch_size)]
    return [dataset[idx: idx + n_ctx] for idx in indices]


def _valid_batch_iter(dataset: np.ndarray, *, batch_size: int, n_ctx: int):
    start_indices = range(0, len(dataset) - n_ctx, n_ctx)
    return _batch_it(
        (dataset[start_idx: start_idx + n_ctx] for start_idx in tqdm.tqdm(
            start_indices, desc='validation', leave=False)),
        batch_size=batch_size)


def _batch_it(it, batch_size: int):
    batch = []
    for x in it:
        batch.append(x)
        if len(batch) == batch_size:
            yield batch
            batch = []
    yield batch


class AverageMeter:
    def __init__(self):
        self.values = []

    def update(self, value):
        self.values.append(value)

    def mean(self):
        return statistics.mean(self.values)

    def reset(self):
        self.values.clear()


def _main_mp(i, kwargs):
    """ Wrapper to use with mp.spawn.
    """
    kwargs['device_id'] = i
    return main(**kwargs)


def fire_main():
    fire.Fire(only_allow_defined_args(main))
