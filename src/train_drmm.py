import argparse
import gensim.downloader as api
import torch
from torch import Tensor
import torch.nn as nn
from torch.optim import AdamW
from torch.utils.data import DataLoader
from tqdm import tqdm

from dataset import DRMMDataset, collate_batch
from models.DRMM import DRMM

def loss_fn(scores_pos: Tensor, scores_neg: Tensor, device: str) -> Tensor:
    z = torch.zeros(scores_pos.shape).to(device)
    return torch.sum(torch.max(z, 1.0 - scores_pos + scores_neg))

def model_fn(batch, word_embedding, model, device):
    query, pos_doc, neg_doc, query_len = batch
    query, pos_doc, neg_doc = query.to(device), pos_doc.to(device), neg_doc.to(device)
    query = word_embedding(query)
    pos_doc = word_embedding(pos_doc)
    neg_doc = word_embedding(neg_doc)
    scores_pos = drmm_model(query, pos_doc, query_len)
    scores_neg = drmm_model(query, neg_doc, query_len)
    loss = loss_fn(scores_pos, scores_neg, device)
    return loss

def valid_fn(dataloader, iterator, word_embedding, model, valid_num, batch_size, device):
    drmm_model.eval()
    running_loss = 0.0
    pbar = tqdm(total=valid_num, ncols=0, desc='Valid', unit=' step')
    for i in range(valid_num):
        try:
            batch = next(iterator)
        except StopIteration:
            iterator = iter(dataloader)
            batch = next(iterator)
        with torch.no_grad():
            loss = model_fn(batch, word_embedding, model, device)
            running_loss += (loss.item() / batch_size)
        pbar.update()
        pbar.set_postfix(
            loss=f'{running_loss / (i+1):.4f}',
        )
    pbar.close()
    drmm_model.train()
    return running_loss / valid_num

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='dataset')
    parser.add_argument('qrels_file', type=str, help="Qrel file in json format")
    parser.add_argument('topics_file', type=str, help="Topic file in json format")
    parser.add_argument('docs_dir', type=str, help="Doc dir in json format")
    parser.add_argument('--model_path', type=str, default='drmm.ckpt', help="Path to model checkpoint")
    parser.add_argument('--valid_steps', type=int, default=5000, help="Steps to validation")
    parser.add_argument('--save_steps', type=int, default=5000, help="Steps to save best model")
    parser.add_argument('--valid_num', type=int, default=250, help="Number of steps doing validation")
    parser.add_argument('--batch_size', type=int, default=8, help="Batch size")
    parser.add_argument('--lr', type=float, default=1e-3, help="Learning rate")
    parser.add_argument('--nbins', type=int, default=30, help="Number of bins for histogram")
    argvs = parser.parse_args()
    print(argvs)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'Using device {device}')

    print('Loading word2vec model...')
    word2vec = api.load('word2vec-google-news-300')
    embedding_weights = torch.FloatTensor(word2vec.vectors)
    word_embedding = nn.Embedding.from_pretrained(embedding_weights).to(device)
    word_embedding.requires_grad = False

    train_set = DRMMDataset(
        argvs.qrels_file, 
        argvs.topics_file, 
        argvs.docs_dir,
        word_model=word2vec,
        mode='train',
    )
    train_loader = DataLoader(
        train_set,
        batch_size=argvs.batch_size, 
        shuffle=True, 
        collate_fn=collate_batch,
    )
    test_set = DRMMDataset(
        argvs.qrels_file, 
        argvs.topics_file, 
        argvs.docs_dir,
        word_model=word2vec,
        mode='test',
    )
    test_loader = DataLoader(
        test_set,
        batch_size=argvs.batch_size, 
        shuffle=False, 
        collate_fn=collate_batch,
    )
    print(f'Train dataset with size {len(train_set)}')
    print(f'Test dataset with size {len(test_set)}')
    train_iterator = iter(train_loader)
    test_iterator = iter(test_loader)

    drmm_model = DRMM(
        embed_dim=embedding_weights.shape[1], 
        nbins=argvs.nbins,
        device=device,
    ).to(device)
    optimizer = AdamW(drmm_model.parameters(), argvs.lr)

    valid_steps = argvs.valid_steps
    save_steps = argvs.save_steps
    valid_num = argvs.valid_num
    pbar = tqdm(total=valid_steps, ncols=0, desc='Train', unit=' step')
    step = 0
    min_loss = float('inf')
    prev_loss = float('inf')
    best_state_dict = None

    while True:
        try:
            batch = next(train_iterator)
        except StopIteration:
            train_iterator = iter(train_loader)
            batch = next(train_iterator)

        loss = model_fn(batch, word_embedding, drmm_model, device)
        loss.backward()
        optimizer.step()
        optimizer.zero_grad()

        pbar.update()
        pbar.set_postfix(
            loss=f'{loss.item() / argvs.batch_size:.4f}',
            step=step + 1,
        )

        if (step + 1) % valid_steps == 0:
            # do validation
            pbar.close()
            valid_loss = valid_fn(test_loader, test_iterator, word_embedding, 
                drmm_model, valid_num, argvs.batch_size, device)

            if valid_loss < min_loss:
                min_loss = valid_loss
                best_state_dict = drmm_model.state_dict()

            pbar = tqdm(total=valid_steps, ncols=0, desc='Train', unit=' step')

        if (step + 1) % save_steps == 0:
            if min_loss < prev_loss: 
                torch.save(best_state_dict, argvs.model_path)
                prev_loss = min_loss
                pbar.write(f'Step {step+1}, best model saved with loss {min_loss:.4f}')

        step += 1

    pbar.close() 
