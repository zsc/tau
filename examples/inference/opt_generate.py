# Copyright (c) Meta Platforms, Inc. and affiliates
import argparse
import os

import torch
import pippy
import pippy.fx
import pippy.utils
from pippy import run_pippy
from pippy.hf import PiPPyHFTracer
from transformers import AutoTokenizer, AutoModelForCausalLM


pippy.fx.Tracer.proxy_buffer_attributes = True

gigabyte_size = 1024 ** 3


def format_to_gb(item, precision=4):
    """quick function to format numbers to gigabyte and round to (default) 4 digit precision"""
    metric_num = item / gigabyte_size
    metric_num = round(metric_num, ndigits=precision)
    return metric_num


def print_mem_usage():
    memory_reserved = format_to_gb(torch.cuda.memory_reserved())
    memory_allocated = format_to_gb(torch.cuda.memory_allocated())
    print(
        f"memory_reserved: {memory_reserved} GB, "
        f"memory_allocated: {memory_allocated} GB"
    )


def get_number_of_params(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def generate_input(args):
    bs = args.batch_size * args.chunks
    seq_length = args.seq_length
    model_config = args.model.config
    torch.manual_seed(args.rank)

    inp = torch.empty(bs, seq_length, dtype=torch.long, device=args.device).random_(model_config.vocab_size)
    model_input_dict = {
        "input_ids": inp,
        #"attention_mask": None,
    }

    return model_input_dict


def run_all(pp_ranks, args):
    model = args.model
    #model.to(args.device)
    model.eval()
    model.config.use_cache = False  # don't output `past_key_values`
    num_ranks = len(pp_ranks)

    if args.rank == 0:
        print(model.config)
        print(f"model total number of params = {get_number_of_params(model) // 10 ** 6}M")

    split_policy = pippy.split_into_equal_size(num_ranks)

    model_input_dict = generate_input(args)
    # Use default value for other kwargs than those in `model_input_dict`
    concrete_args = pippy.create_default_args(
        model,
        except_keys=model_input_dict.keys(),
    )

    pipe_driver, stage_mod = pippy.all_compile(
        model,
        num_ranks,
        args.chunks,
        split_policy=split_policy,
        tracer=PiPPyHFTracer(),
        concrete_args=concrete_args,
    )

    params = get_number_of_params(stage_mod)
    print(f"submod_{args.rank} {params // 10 ** 6}M params")

    if args.rank != 0:
        return

    # Master continues
    print_mem_usage()

    # Inject pipeline driver's forward function back to original model to support HF's `generate()` method
    pippy.utils.inject_pipeline_forward(model, pipe_driver)

    # OPT generate
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    prompt = ["Hey, are you conscious? Can you talk to me?"] * 10 
    input = tokenizer(prompt, return_tensors="pt")

    input_ids = input["input_ids"].to(args.device)
    outputs = model.generate(input_ids, max_length=30, do_sample=True, temperature=0.5)
    for response in tokenizer.batch_decode(outputs, skip_special_tokens=True, clean_up_tokenization_spaces=False):
        print(response)
        print('-' * 20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--world_size', type=int, default=int(os.getenv("WORLD_SIZE", 4)))
    parser.add_argument('--rank', type=int, default=int(os.getenv("RANK", -1)))
    parser.add_argument('--master_addr', type=str, default=os.getenv('MASTER_ADDR', 'localhost'))
    parser.add_argument('--master_port', type=str, default=os.getenv('MASTER_PORT', '29500'))
    parser.add_argument('--model_name', type=str, default='bigscience/bloom-3b')
    parser.add_argument('--batch_size', type=int, default=1)
    parser.add_argument('--chunks', type=int, default=1)
    parser.add_argument('--seq_length', type=int, default=16)
    parser.add_argument('--cuda', type=int, default=int(torch.cuda.is_available()))
    parser.add_argument('--pp_group_size', type=int, default=int(os.getenv("WORLD_SIZE", 4)))

    args = parser.parse_args()

    assert args.world_size % args.pp_group_size == 0

    # Main process loads model
    print(f"Loading model {args.model_name}")
    model = AutoModelForCausalLM.from_pretrained(args.model_name, use_cache=False)
    args.model = model

    args.gspmd = 1
    run_pippy(run_all, args)
