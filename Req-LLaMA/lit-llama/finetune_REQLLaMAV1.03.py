
import os
import time

import lightning as L
import numpy as np
import torch
import csv
import wandb

from generate import generate
from lit_llama.lora import mark_only_lora_as_trainable, lora, lora_state_dict
from lit_llama.model import LLaMA, LLaMAConfig
from lit_llama.tokenizer import Tokenizer
#from scripts.prepare_alpaca import generate_prompt
from scripts.prepare_HS2Data import generate_prompt

eval_interval = 100
save_interval = 100
eval_iters = 100
log_interval = 1

# Hyperparameters
learning_rate = 1e-5
batch_size = 32
micro_batch_size = 4
gradient_accumulation_steps = batch_size // micro_batch_size
max_iters = 75000 * 3 // micro_batch_size
weight_decay =  1e-5 #0.0
max_seq_length = 256  # see scripts/prepare_alpaca.py
lora_r = 10
lora_alpha = 14
lora_dropout = 0.05
warmup_steps = 100

wandb.init(
  project="llamaV1.04",
  dir="gdrive/MyDrive/Transformers/LLAMA/wandbV2",
  magic=True,
  config=
  {"learning_rate":learning_rate})


def main(
    data_dir: str = "/gdrive/MyDrive/Transformers/LLAMA/Datasets", 
    pretrained_path: str = "/gdrive/MyDrive/Transformers/LLAMA/WEIGHTS/lit-llama/7B/lit-llama.pth",
    out_dir: str = "/gdrive/MyDrive/Transformers/LLAMA/WEIGHTS/REQLLaMAV3",
):

    fabric = L.Fabric(accelerator="cuda", devices=1, precision="bf16-true")
    fabric.launch()
    fabric.seed_everything(1337 + fabric.global_rank)

    if fabric.global_rank == 0:
        os.makedirs(out_dir, exist_ok=True)

    train_data, val_data = load_datasets(data_dir=data_dir)

    config = LLaMAConfig.from_name("7B")
    config.block_size = max_seq_length

    checkpoint = torch.load(pretrained_path)

    with fabric.init_module(), lora(r=lora_r, alpha=lora_alpha, dropout=lora_dropout, enabled=True):
        model = LLaMA(config)
        # strict=False because missing keys due to LoRA weights not contained in checkpoint state
        model.load_state_dict(checkpoint, strict=False)
    
    mark_only_lora_as_trainable(model)

    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    model, optimizer = fabric.setup(model, optimizer)
    train(fabric, model, optimizer, train_data, val_data, out_dir)

    # Save the final LoRA checkpoint at the end of training
    checkpoint = lora_state_dict(model)
    fabric.save(os.path.join(out_dir, "REQLLaMA-finetuned.pth"), checkpoint)

#-----------


def train(
    fabric: L.Fabric,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    train_data: np.ndarray,
    val_data: np.ndarray,
    out_dir: str,
) -> None:
    """The training loop.

    Loosely based on the nanoGPT implementation: https://github.com/karpathy/nanoGPT.
    """
    step_count = 0

    with open(os.path.join(out_dir, "training_loss.csv"), "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["iter_num", "training loss", "time_step", "time"])  # Write the header row
        with open(os.path.join(out_dir, "val_loss.csv"), "w", newline="") as csvfileval:
            writerval = csv.writer(csvfileval)
            writerval.writerow(["iter_num", "step_count", "validation Loss"])  # Write the header row
            for iter_num in range(max_iters):

                if step_count <= warmup_steps:
                    # linear warmup
                    lr = learning_rate * step_count / warmup_steps
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = lr

                t0 = time.time()

                input_ids, targets = get_batch(fabric, train_data)
                logits = model(input_ids)
                loss = loss_fn(logits, targets)
                fabric.backward(loss)

                if (iter_num + 1) % gradient_accumulation_steps == 0:
                    optimizer.step()
                    optimizer.zero_grad()
                    step_count += 1
                    
                    if step_count% eval_interval == 0:
                        val_loss = validate(fabric, model, val_data)
                        fabric.print(f"step {iter_num}: val loss {val_loss:.4f}")
                        wandb.log({"iterations" : iter_num, "val loss": val_loss})
                        writerval.writerow([iter_num, step_count, val_loss])
                        csvfileval.flush()  # Flush the buffer to ensure immediate writing
                        fabric.barrier()


                    if step_count % save_interval == 0:
                        print(f"Saving LoRA weights to {out_dir}")
                        # We are only saving the LoRA weights
                        # TODO: Provide a function/script to merge the LoRA weights with pretrained weights
                        checkpoint = lora_state_dict(model)
                        fabric.save(os.path.join(out_dir, f"iter-{iter_num:06d}-ckpt.pth"), checkpoint)

                dt = time.time() - t0
                if iter_num % log_interval == 0:
                    fabric.print(f"iter {iter_num}: loss {loss.item():.4f}, time: {dt*1000:.2f}ms")
                    wandb.log({"iterations" : iter_num, "Training loss": loss.item()})
                    # Write iter_num and training loss to the CSV file
                    writer.writerow([iter_num, loss.item(), step_count,dt*1000])
                    csvfile.flush()  # Flush the buffer to ensure immediate writing
        wandb.finish()
    
def generate_response(model, instruction):
    tokenizer = Tokenizer("/gdrive/MyDrive/Transformers/LLAMA/WEIGHTS/lit-llama/tokenizer.model")
    sample = {"instruction": instruction, "input": ""}
    prompt = generate_prompt(sample)
    encoded = tokenizer.encode(prompt, bos=True, eos=False, device=model.device)

    output = generate(
        model,
        idx=encoded,
        max_seq_length=max_seq_length,
        max_new_tokens=100,
    )
    output = tokenizer.decode(output)
    return output # output.split("### Response:")[1].strip()


@torch.no_grad()
def validate(fabric: L.Fabric, model: torch.nn.Module, val_data: np.ndarray) -> torch.Tensor:
    fabric.print("Validating ...")
    model.eval()
    losses = torch.zeros(eval_iters)
    for k in range(eval_iters):
        input_ids, targets = get_batch(fabric, val_data)
        logits = model(input_ids)
        loss = loss_fn(logits, targets)
        losses[k] = loss.item()
    out = losses.mean()

    # produce an example:
    instruction = "A taxi pole and shelter shall be provided at Old Oak Common station taxi rank."
    
    output = generate_response(model, instruction)
    fabric.print(instruction)
    fabric.print(output)

    model.train()
    return out.item()

def loss_fn(logits, targets):
    # shift the targets such that output n predicts token n+1
    logits = logits[..., :-1, :].contiguous()
    targets = targets[..., 1:].contiguous()
    loss = torch.nn.functional.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1), ignore_index=-1)
    return loss
    

def get_batch(fabric: L.Fabric, data: list):
    ix = torch.randint(len(data), (micro_batch_size,))

    input_ids = [data[i]["input_ids"].type(torch.int64) for i in ix]
    labels = [data[i]["labels"].type(torch.int64) for i in ix]

    max_len = max(len(s) for s in input_ids)

    def pad_right(x, pad_id):
        # pad right based on the longest sequence
        n = max_len - len(x)
        return torch.cat((x, torch.full((n,), pad_id, dtype=x.dtype)))

    x = torch.stack([pad_right(x, pad_id=0) for x in input_ids])
    y = torch.stack([pad_right(x, pad_id=-1) for x in labels])
    x, y = fabric.to_device((x.pin_memory(), y.pin_memory()))
    return x, y


def load_datasets(data_dir):
    train_data = torch.load(os.path.join(data_dir, "train.pt"))
    val_data = torch.load(os.path.join(data_dir, "test.pt"))
    return train_data, val_data


if __name__ == "__main__":
    # Uncomment this line if you see an error: "Expected is_sm80 to be true, but got false"
    # torch.backends.cuda.enable_flash_sdp(False)
    torch.set_float32_matmul_precision("high")
    
    from jsonargparse.cli import CLI

    CLI(main)
