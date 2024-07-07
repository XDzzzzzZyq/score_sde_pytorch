import os
import tensorboard
from pinn_kalman.pinn import PINN_Net
import losses
import torch
import logging
import sampling
from models import utils as mutils
from models.ema import ExponentialMovingAverage
import datasets
from torch.utils import tensorboard
from torchvision.utils import make_grid, save_image
from utils import save_checkpoint, load_checkpoint, restore_checkpoint

def train(config, workdir):
    # Create directories for experimental logs
    sample_dir = os.path.join(workdir, "samples")
    os.makedirs(sample_dir, exist_ok=True)

    tb_dir = os.path.join(workdir, "tensorboard")
    os.makedirs(tb_dir, exist_ok=True)
    writer = tensorboard.SummaryWriter(tb_dir)

    # Initialize model.
    mean_value = torch.ones(3, device=config.device) * 0.5  # 需要提供适当的归一化均值和标准差
    std_value = torch.ones(3, device=config.device) * 0.5

    model = PINN_Net(config, mean_value, std_value)
    ema = ExponentialMovingAverage(model.parameters(), decay=config.model.ema_rate)
    optimizer = losses.get_optimizer(config, model.parameters())
    state = dict(optimizer=optimizer, model=model, ema=ema, step=0)

    # Create checkpoints directory
    checkpoint_dir = os.path.join(workdir, "checkpoints")
    # Intermediate checkpoints to resume training after pre-emption in cloud environments
    checkpoint_meta_dir = os.path.join(workdir, "checkpoints-meta", "checkpoint.pth")
    os.makedirs(checkpoint_dir, exist_ok=True)
    os.makedirs(os.path.dirname(checkpoint_meta_dir), exist_ok=True)
    # Resume training when intermediate checkpoints are detected
    state = restore_checkpoint(checkpoint_meta_dir, state, config.device)
    initial_step = int(state['step'])

    # Build data iterators
    train_ds, eval_ds = datasets.get_dataset(config,
                                             uniform_dequantization=config.data.uniform_dequantization)
    train_iter = iter(train_ds)  # pytype: disable=wrong-arg-types
    eval_iter = iter(eval_ds)  # pytype: disable=wrong-arg-types

    # Build one-step training and evaluation functions
    optimize_fn = losses.optimization_manager(config)
    #continuous = config.training.continuous
    #reduce_mean = config.training.reduce_mean
    #likelihood_weighting = config.training.likelihood_weighting
    train_step_fn = losses.get_pinn_step_fn(config, train=True, optimize_fn=optimize_fn)
    eval_step_fn = losses.get_pinn_step_fn(config, train=False, optimize_fn=optimize_fn)


    num_train_steps = config.training.n_iters
    print("num_train_steps", num_train_steps)

    # In case there are multiple hosts (e.g., TPU pods), only log to host 0
    logging.info("Starting training loop at step %d." % (initial_step,))

    for step in range(initial_step, num_train_steps + 1):
        try:
            batch, t, target = next(train_iter)
        except StopIteration:
            train_iter = iter(train_ds)

        # Convert data to JAX arrays and normalize them. Use ._numpy() to avoid copy.
        batch = batch.to(config.device).float()
        t = t.to(config.device).float()
        target = target.to(config.device).float()

        # Execute one training step
        loss, loss_e, loss_d = train_step_fn(state, batch, t, target)

        if step % config.training.log_freq == 0:
            logging.info("step: %d, training_loss: %.5e = (%.5e, %.5e)" % (step, loss.item(), loss_e.item(), loss_d.item()))
            writer.add_scalar("training_loss", loss, step)

        # Save a temporary checkpoint to resume training after pre-emption periodically
        if step != 0 and step % config.training.snapshot_freq_for_preemption == 0:
            save_checkpoint(checkpoint_meta_dir, state)

        # Report the loss on an evaluation dataset periodically
        if step % config.training.eval_freq == 0:
            try:
                eval_batch, eval_t, eval_target = next(eval_iter)
            except StopIteration:
                eval_iter = iter(eval_ds)
            eval_batch = eval_batch.to(config.device).float()
            eval_t = eval_t.to(config.device).float()
            eval_target = eval_target.to(config.device).float()

            eval_loss, eval_loss_e, eval_loss_d = eval_step_fn(state, eval_batch, eval_t, eval_target)
            logging.info("step: %d, eval_loss: %.5e = (%.5e, %.5e)" % (step, loss.item(), loss_e.item(), loss_d.item()))
            writer.add_scalar("eval_loss", eval_loss.item(), step)

        # Save a checkpoint periodically and generate samples if needed
        if step != 0 and step % config.training.snapshot_freq == 0 or step == num_train_steps:
            # Save the checkpoint.
            save_step = step // config.training.snapshot_freq
            save_checkpoint(os.path.join(checkpoint_dir, f'checkpoint_{save_step}.pth'), state)
            print(f">>> checkpoint_{save_step}.pth saved")


if __name__ == "__main__":
    import matplotlib.pyplot as plt
    from configs.pinn.pinn_pde import get_config
    config = get_config()
    workdir = "workdir/pde-pinn_kalman/checkpoints/checkpoint-2.pth"

    mean_value = torch.ones(3, device=config.device) * 0.5  # 需要提供适当的归一化均值和标准差
    std_value = torch.ones(3, device=config.device) * 0.5

    model = PINN_Net(config, mean_value, std_value)
    model = load_checkpoint(workdir, model, config.device)


    # Build data iterators
    eval_ds, _ = datasets.get_dataset(config,
                                             uniform_dequantization=config.data.uniform_dequantization)
    eval_iter = iter(eval_ds)  # pytype: disable=wrong-arg-types
    eval_batch, eval_t, eval_target = next(eval_iter)

    nrow = int(8)
    image_grid = make_grid(eval_target, nrow, padding=2)
    plt.imshow(image_grid[0])
    plt.show()

    predict = model(eval_batch, eval_t)

    fig, axe = plt.subplots(nrows=2, ncols=3, figsize=(40, 20))
    axe[0][0].imshow(predict[0, 0].cpu().detach().numpy())
    axe[0][1].imshow(predict[0, 1].cpu().detach().numpy())
    axe[0][2].imshow(predict[0, 2].cpu().detach().numpy())

    axe[1][0].imshow(eval_target[0, 0].cpu().detach().numpy())
    axe[1][1].imshow(eval_target[0, 1].cpu().detach().numpy())
    axe[1][2].imshow(eval_target[0, 2].cpu().detach().numpy())

    plt.show()

