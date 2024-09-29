import os

import torch
import trl

from transformers import AutoTokenizer, LlamaConfig, AutoModelForCausalLM, LlamaForCausalLM, TrainingArguments, PreTrainedTokenizerFast, AdamW, get_cosine_schedule_with_warmup
from datasets import load_dataset, Dataset
from tokenizers import ByteLevelBPETokenizer

BATCH_SIZE = 128
EPOCHS = 1
LEARNING_RATE = 2e-4
FACTOR = 22 * 30
MAX_SEQ_LENGTH = 128
VOCAB_SIZE = 32000
INPUT_DATASET = "HuggingFaceTB/smollm-corpus"
INSTRUCT_DATASET = "nroggendorff/elephant"
OUTPUT_REPO = "nroggendorff/smallama"
INSTRUCT_FINETUNE_BOOL = False
INIT = 0#/13
SHARD_SIZE = int(3e+6)
FP16 = True
WARMUP_STEPS = 0
DECAY = 0
GRADIENT_ACCUMULATION_STEPS = 1
PUSH_TO_HUB = True

def load_data():
    if not INSTRUCT_FINETUNE_BOOL:
        dataset = load_dataset(INPUT_DATASET, "cosmopedia-v2", split="train")#, streaming=True)
        # dataset = Dataset.from_generator(lambda: dataset.take(int(5e+6)))
        # dataset = dataset.shard(num_shards=len(dataset) // SHARD_SIZE, index=INIT)
    else:
        dataset = load_dataset(INSTRUCT_DATASET, split="train")#, streaming=True)
        # dataset = Dataset.from_generator(lambda: dataset.take(int(5e+6)))
    return dataset

def create_tokenizer(training_corpus):
    tokenizer = ByteLevelBPETokenizer()
    special_tokens = ["<s>", "<pad>", "</s>", "<unk>", "<mask>"]
    if INSTRUCT_FINETUNE_BOOL:
        special_tokens.append(["<|user|>", "<|bot|>", "<|end|>"])
    tokenizer.train_from_iterator(
        training_corpus,
        vocab_size=VOCAB_SIZE,
        min_frequency=2,
        special_tokens=special_tokens
    )

    fast_tokenizer = PreTrainedTokenizerFast(tokenizer_object=tokenizer._tokenizer)
    return fast_tokenizer

def load_tokenizer():
    tokenizer = AutoTokenizer.from_pretrained(OUTPUT_REPO)
    return tokenizer

def get_training_corpus(dataset):
    texts = []
    #for field in ['pretrain', 'instruct']:
    #    texts.extend(dataset[field]['text'])
    texts.extend(dataset['text'])

    for i in range(0, len(texts), 1000):
        yield texts[i : i + 1000]

def format_prompts(examples, tokenizer, isinst):
    texts = []
    for text in examples['text']:
        if isinst:
            conversation = []
            parts = text.split('<|end|>')
            for i in range(0, len(parts) - 1, 2):
                prompt = parts[i].replace("<|user|>", "")
                response = parts[i + 1].replace("<|bot|>", "")
                conversation.append({"role": "user", "content": prompt})
                conversation.append({"role": "assistant", "content": response})
            formatted_conversation = tokenizer.apply_chat_template(conversation, tokenize=False)
            texts.append(formatted_conversation)
        else:
            texts.append(tokenizer.bos_token + text + tokenizer.eos_token)
    return {"text": texts}

def create_model(tokenizer):
    config = LlamaConfig(
        vocab_size=tokenizer.vocab_size,
        hidden_size=FACTOR,
        intermediate_size=FACTOR * 4,
        num_hidden_layers=max(1, FACTOR // 32),
        num_attention_heads=max(1, FACTOR // 64),
        max_position_embeddings=MAX_SEQ_LENGTH,
        rms_norm_eps=1e-6,
        initializer_range=0.02,
        use_cache=True,
        pad_token_id=tokenizer.pad_token_id,
        bos_token_id=tokenizer.bos_token_id,
        eos_token_id=tokenizer.eos_token_id,
        tie_word_embeddings=False,
    )
    
    model = LlamaForCausalLM(config)
    return model

def load_model():
    model = AutoModelForCausalLM.from_pretrained(OUTPUT_REPO)
    return model

def configure_tokenizer(tokenizer):
    special_tokens = {
        "bos_token": "<s>",
        "eos_token": "</s>",
        "unk_token": "<unk>",
        "pad_token": "<pad>",
        "mask_token": "<mask>"
    }
    if INSTRUCT_FINETUNE_BOOL:
        special_tokens["additional_special_tokens"] = ["<|user|>", "<|bot|>", "<|end|>"]
    tokenizer.add_special_tokens(special_tokens)

    if INSTRUCT_FINETUNE_BOOL:
        tokenizer.user_token_id = tokenizer.convert_tokens_to_ids("<|user|>")
        tokenizer.assistant_token_id = tokenizer.convert_tokens_to_ids("<|bot|>")
    
        chat_template = "{{ bos_token }}{% for message in messages %}{% if (message['role'] == 'user') != (loop.index0 % 2 == 0) %}{{ raise_exception('Conversation roles must alternate user/assistant/user/assistant/...') }}{% endif %}{% if message['role'] == 'user' %}{{ '<|user|>\n' + message['content'] + '<|end|>\n' }}{% elif message['role'] == 'assistant' %}{{ '<|bot|>\n' + message['content'] + '<|end|>\n' + eos_token}}{% else %}{{ raise_exception('Only user and assistant roles are supported!') }}{% endif %}{% endfor %}"
        tokenizer.chat_template = chat_template

def train_model(model, tokenizer, dataset, push, isinst):
    args = TrainingArguments(
        output_dir="model",
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        learning_rate=LEARNING_RATE,
        optim="adamw_torch",
        warmup_steps=WARMUP_STEPS,
        weight_decay=DECAY,
        gradient_accumulation_steps=GRADIENT_ACCUMULATION_STEPS,
        fp16=FP16,
        save_steps=int(1e+10),
        logging_steps=10
    )

    dataset = dataset.shard(num_shards=len(dataset) // SHARD_SIZE, index=INIT)

    optimizer = AdamW(model.parameters(), lr=args.learning_rate)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer,
        num_warmup_steps=args.warmup_steps, 
        num_training_steps=(len(dataset) // args.per_device_train_batch_size) * args.num_train_epochs
    )
    
    dataset = dataset.map(lambda examples: format_prompts(examples, tokenizer, isinst), batched=True, remove_columns=dataset.column_names)
   
    trainer = trl.SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        args=args,
        train_dataset=dataset,
        dataset_text_field='text',
        max_seq_length=MAX_SEQ_LENGTH,
        optimizers=(optimizer, scheduler)
    )
    
    train = trainer.train()
    
    trained_model = trainer.model
    trained_tokenizer = trainer.tokenizer
    
    if push:
        if INSTRUCT_FINETUNE_BOOL:      
            repo_id = OUTPUT_REPO + "-it"
        else:
            repo_id = OUTPUT_REPO
        msg = str(train.training_loss)
        trained_model.push_to_hub(repo_id, commit_message=msg, force=True)
        trained_tokenizer.push_to_hub(repo_id, commit_message=msg, force=True)
    else:
        trained_model.save_pretrained("model")
        trained_tokenizer.save_pretrained("tokenizer")

def main(push_to_hub=True, is_inst_finetune=False):
    dataset = load_data()
    if not is_inst_finetune:
        training_corpus = get_training_corpus(dataset)
        if INIT == 0:
            tokenizer = create_tokenizer(training_corpus)
        else:
            tokenizer = load_tokenizer()
    else:
        tokenizer = load_tokenizer()
    configure_tokenizer(tokenizer)
    if is_inst_finetune:
        model = load_model()
        model.resize_token_embeddings(len(tokenizer))
        train_model(model, tokenizer, dataset, push_to_hub, True)
    else:
        if INIT == 0:
            model = create_model(tokenizer)
        else:
            model = load_model()
        train_model(model, tokenizer, dataset, push_to_hub, False)

if __name__ == "__main__":
    main(PUSH_TO_HUB, INSTRUCT_FINETUNE_BOOL)
    raise RuntimeError("The script is finished.")