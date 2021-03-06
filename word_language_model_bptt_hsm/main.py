import argparse
import time
import collections
import copy
import math
import torch
import numpy as np
import torch.nn as nn
from torch.autograd import Variable

import data
import model

parser = argparse.ArgumentParser(description='PyTorch PennTreeBank RNN/LSTM Language Model')
parser.add_argument('--data', type=str, default='./data/penn',
                    help='location of the data corpus')
parser.add_argument('--model', type=str, default='LSTM',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU)')
parser.add_argument('--emsize', type=int, default=200,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=200,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=2,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=20,
                    help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=40,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=20, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=35,
                    help='sequence length')
parser.add_argument('--bptt_step', type=int, default=None,
                    help='bptt step size')
parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')
parser.add_argument('--log-interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str,  default='model.pt',
                    help='path to save the final model')
args = parser.parse_args()

args.bptt_step = args.bptt_step if args.bptt_step else args.bptt
print('bptt step size is %d' % args.bptt_step)

# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)
if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")
    else:
        torch.cuda.manual_seed(args.seed)

###############################################################################
# Load data
###############################################################################

corpus = data.Corpus(args.data)

def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    if args.cuda:
        data = data.cuda()
    return data

eval_batch_size = 10
train_data = batchify(corpus.train, args.batch_size)
val_data = batchify(corpus.valid, eval_batch_size)
test_data = batchify(corpus.test, eval_batch_size)

###############################################################################
# Build the model
###############################################################################

ntokens = len(corpus.dictionary)
model = model.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, args.dropout, args.tied)
if args.cuda:
    model.cuda()

criterion = nn.CrossEntropyLoss(size_average=False)

###############################################################################
# Training code
###############################################################################

def repackage_hidden(h, volatile=False, requires_grad=False):
    """Wraps hidden states in new Variables, to detach them from their history."""
    if type(h) == Variable:
        return Variable(h.data, volatile=volatile, requires_grad=requires_grad)
    else:
        return tuple(repackage_hidden(v, volatile=volatile,
           requires_grad=requires_grad) for v in h)


def get_batch(source, i, evaluation=False):
    seq_len = min(args.bptt, len(source) - 1 - i)
    data = Variable(source[i:i+seq_len], volatile=evaluation)
    #target = Variable(source[i+1:i+1+seq_len].view(-1))
    target = Variable(source[i+1:i+1+seq_len])
    return data, target


def evaluate(data_source):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(eval_batch_size)
    for i in range(0, data_source.size(0) - 1, args.bptt):
        data, targets = get_batch(data_source, i, evaluation=True)
        output, hidden = model(data, hidden)
        output_flat = output.view(-1, ntokens)
        total_loss += len(data) * criterion(output_flat, targets.view(-1)).data
        hidden = repackage_hidden(hidden)
    return total_loss[0] / (len(data_source)*eval_batch_size*args.bptt)


def train():
    # Turn on training mode which enables dropout.
    model.train()
    total_loss = 0
    start_time = time.time()
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(args.batch_size)
    for batch, i in enumerate(range(0, train_data.size(0) - 1, args.bptt)):
        data, targets = get_batch(train_data, i)
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        model.zero_grad()
        # original:
        # hidden = repackage_hidden(hidden)
        #output, hidden = model(data, hidden)
        #loss = criterion(output.view(-1, ntokens), targets)
        #loss.backward()

        # Begin bptt hsm code
        hidden_v = repackage_hidden(hidden, volatile=True)
        data_v, _ = get_batch(train_data, i, evaluation=True)
        hsm = { -1 : repackage_hidden(hidden) }
        intervals = list(enumerate(range(0, data.size(0), args.bptt_step)))
        # Record states at selective intervals and flag the need for grads.
        # Note we don't need to forward the last interval as we'll do it below.
        # This loop is most of the extra computation for this approach.
        for f_i,f_v in intervals[:-1]:
            output,hidden_v = model(data_v[f_v:f_v+args.bptt_step], hidden_v)
            hsm[f_i] = repackage_hidden(hidden_v, volatile=False,
                requires_grad=True)

        save_grad=None
        loss = 0
        for b_i, b_v in reversed(intervals):
            output,h = model(data[b_v:b_v+args.bptt_step], hsm[b_i-1])
            iloss = criterion(output.view(-1, ntokens), 
                targets[b_v:b_v+args.bptt_step].view(-1))
            if b_v+args.bptt_step >= data.size(0):
                # No gradient from the future needed.
                # These are the hidden states for the next sequence.
                hidden = h
                iloss.backward()
            else:
                variables=[iloss]
                grad_variables=[None]   # scalar = None
                # Associate stored gradients with state variables for 
                # multi-variable backprop
                for l in h:
                    variables.append(l)
                    g = save_grad.popleft()
                    grad_variables.append(g)
                torch.autograd.backward(variables, grad_variables)
            if b_i > 0:
                # Save the gradients left on the input state variables
                save_grad = collections.deque()
                for l in hsm[b_i-1]:
                    # If this fails, could be a non-leaf, in which case exclude;
                    # its grad will be handled by a leaf
                    assert(l.grad is not None)  
                    save_grad.append(l.grad)
            loss += iloss.data[0]

        av = 1/(args.batch_size*args.bptt)
        loss *= av
        for g in model.parameters():
            g.grad.data.mul_(av)
        # end bptt hsm code

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
        for p in model.parameters():
            p.data.add_(-lr, p.grad.data)

        total_loss += loss

        if batch % args.log_interval == 0 and batch > 0:
            cur_loss = total_loss / args.log_interval
            elapsed = time.time() - start_time
            print('| epoch {:3d} | {:5d}/{:5d} batches | lr {:02.2f} | ms/batch {:5.2f} | '
                    'loss {:5.2f} | ppl {:8.2f}'.format(
                epoch, batch, len(train_data) // args.bptt, lr,
                elapsed * 1000 / args.log_interval, cur_loss, math.exp(cur_loss)))
            total_loss = 0
            start_time = time.time()

# Loop over epochs.
lr = args.lr
best_val_loss = None

# At any point you can hit Ctrl + C to break out of training early.
try:
    for epoch in range(1, args.epochs+1):
        epoch_start_time = time.time()
        train()
        val_loss = evaluate(val_data)
        print('-' * 89)
        print('| end of epoch {:3d} | time: {:5.2f}s | valid loss {:5.2f} | '
                'valid ppl {:8.2f}'.format(epoch, (time.time() - epoch_start_time),
                                           val_loss, math.exp(val_loss)))
        print('-' * 89)
        # Save the model if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            with open(args.save, 'wb') as f:
                torch.save(model, f)
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0
except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')

# Load the best saved model.
with open(args.save, 'rb') as f:
    model = torch.load(f)

# Run on test data.
test_loss = evaluate(test_data)
print('=' * 89)
print('| End of training | test loss {:5.2f} | test ppl {:8.2f}'.format(
    test_loss, math.exp(test_loss)))
print('=' * 89)
