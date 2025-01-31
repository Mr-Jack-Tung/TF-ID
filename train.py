# this script uses code snippets from Roboflow's tutorial: https://colab.research.google.com/github/roboflow-ai/notebooks/blob/main/notebooks/how-to-finetune-florence-2-on-detection-dataset.ipynb

import torch
from transformers import AutoModelForCausalLM, AutoProcessor
from PIL import Image
from transformers import (
    AdamW,
    AutoModelForCausalLM,
    AutoProcessor,
    get_scheduler
)
from tqdm import tqdm
import os
import json
from torch.utils.data import Dataset, DataLoader
from typing import List, Dict, Any, Tuple, Generator
from accelerate import Accelerator

# run coco_to_florence.py first to convert coco annotations to florence format
# use "accelerate launch train.py" to run this script

BATCH_SIZE = 4 # adjust based on your GPU specs
gradient_accumulation_steps = 2 # adjust based on your GPU specs
NUM_WORKERS = 0
epochs = 8
learning_rate = 5e-6

img_dir = "./images"
train_labels = "./annotations/train.jsonl" # generated by running coco_to_florence.py
test_labels = "./annotations/test.jsonl" # generated by running coco_to_florence.py
output_dir = "./model_checkpoints"

CHECKPOINT = "microsoft/Florence-2-base-ft"
# CHECKPOINT = "microsoft/Florence-2-large-ft"

accelerator = Accelerator(gradient_accumulation_steps=gradient_accumulation_steps)

DEVICE = accelerator.device
model = AutoModelForCausalLM.from_pretrained(
    CHECKPOINT, trust_remote_code=True).to(DEVICE)
processor = AutoProcessor.from_pretrained(
    CHECKPOINT, trust_remote_code=True)

class JSONLDataset:
    def __init__(self, jsonl_file_path: str, image_directory_path: str):
        self.jsonl_file_path = jsonl_file_path
        self.image_directory_path = image_directory_path
        self.entries = self._load_entries()

    def _load_entries(self) -> List[Dict[str, Any]]:
        entries = []
        with open(self.jsonl_file_path, 'r') as file:
            for line in file:
                data = json.loads(line)
                entries.append(data)
        return entries

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, idx: int) -> Tuple[Image.Image, Dict[str, Any]]:
        if idx < 0 or idx >= len(self.entries):
            raise IndexError("Index out of range")

        entry = self.entries[idx]
        image_path = os.path.join(self.image_directory_path, entry['image'])
        try:
            image = Image.open(image_path)
            return (image, entry)
        except FileNotFoundError:
            raise FileNotFoundError(f"Image file {image_path} not found.")

class DetectionDataset(Dataset):
    def __init__(self, jsonl_file_path: str, image_directory_path: str):
        self.dataset = JSONLDataset(jsonl_file_path, image_directory_path)

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, idx):
        image, data = self.dataset[idx]
        prefix = data['prefix']
        suffix = data['suffix']
        return prefix, suffix, image

def collate_fn(batch):
	questions, answers, images = zip(*batch)
	inputs = processor(text=list(questions), images=list(images), return_tensors="pt", padding=True).to(DEVICE)
	return inputs, answers

train_dataset = DetectionDataset(
    jsonl_file_path = train_labels,
    image_directory_path = img_dir
)

test_dataset = DetectionDataset(
    jsonl_file_path = test_labels,
    image_directory_path = img_dir
)

train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, collate_fn=collate_fn, num_workers=NUM_WORKERS, shuffle=True)
test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE, collate_fn=collate_fn, num_workers=NUM_WORKERS, shuffle=False)

def train_model(train_loader, val_loader, model, processor, epochs, lr):
	optimizer = AdamW(model.parameters(), lr=lr)
	num_training_steps = epochs * len(train_loader)
	lr_scheduler = get_scheduler(
		name="linear",
		optimizer=optimizer,
		num_warmup_steps=12,
		num_training_steps=num_training_steps,
	)

	model, optimizer, train_loader, lr_scheduler = accelerator.prepare(
		model, optimizer, train_loader, lr_scheduler
	)

	for epoch in range(epochs):
		model.train()
		train_loss = 0
		for inputs, answers in tqdm(train_loader, desc=f"Training Epoch {epoch + 1}/{epochs}"):
			with accelerator.accumulate(model):
				input_ids = inputs["input_ids"]
				pixel_values = inputs["pixel_values"]
				labels = processor.tokenizer(
					text=answers,
					return_tensors="pt",
					padding=True,
					return_token_type_ids=False
				).input_ids.to(DEVICE)

				outputs = model(input_ids=input_ids, pixel_values=pixel_values, labels=labels)
				loss = outputs.loss

				accelerator.backward(loss), 
				optimizer.step(), 
				lr_scheduler.step(), 
				optimizer.zero_grad()
				train_loss += loss.item()

				avg_train_loss = train_loss / len(train_loader)

		print(f"Average Training Loss: {avg_train_loss}")

		model.eval()
		val_loss = 0
		with torch.no_grad():
			for inputs, answers in tqdm(val_loader, desc=f"Validation Epoch {epoch + 1}/{epochs}"):

				input_ids = inputs["input_ids"]
				pixel_values = inputs["pixel_values"]
				labels = processor.tokenizer(
					text=answers,
					return_tensors="pt",
					padding=True,
					return_token_type_ids=False
				).input_ids.to(DEVICE)

				outputs = model(input_ids=input_ids, pixel_values=pixel_values, labels=labels)
				loss = outputs.loss

				val_loss += loss.item()

			avg_val_loss = val_loss / len(val_loader)
			print(f"Average Validation Loss: {avg_val_loss}")
			# this loss is not very informative, should use a better metric for evaluation

		weights_output_dir = output_dir + f"/epoch_{epoch+1}"
		os.makedirs(weights_output_dir, exist_ok=True)
		accelerator.save_model(model, weights_output_dir)

train_model(train_loader, test_loader, model, processor, epochs=epochs, lr=learning_rate)