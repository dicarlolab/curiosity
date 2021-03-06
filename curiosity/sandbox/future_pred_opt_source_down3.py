"""
coupled symmetric model with from-below coupling
     --top-down is freely parameterized num-channels but from-below and top-down have same spatial extent 
     --top-down and bottom-up are combined via convolution to the correct num-channel shape:
        I = ReluConv(concat(top_down, bottom_up))
     --error is compuated as:
       (future_bottom_up - current_I)**2
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools
import argparse
import sys
import copy
import numpy as np
import time
import os
import zmq
import struct
import cPickle
import json
import pymongo as pm
from PIL import Image

import gzip

from six.moves import urllib
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

IMAGE_SIZE = 256
NUM_CHANNELS = 3
PIXEL_DEPTH = 255
BATCH_SIZE = 64
NUM_TRAIN_STEPS = 2048000
TEST_FREQUENCY = 20
SAVE_MULTIPLE = 25
OBSERVATION_LENGTH = 1
ATOMIC_ACTION_LENGTH = 14
MAX_NUM_ACTIONS = 10

tf.app.flags.DEFINE_boolean("self_test", False, "True if running a self test.")
FLAGS = tf.app.flags.FLAGS

ctx = zmq.Context()
sock = ctx.socket(zmq.REQ)

print("connecting...")
sock.connect("tcp://18.93.3.135:23043")
print("...connected")

THRES = 100

class HiLossError(Exception):
  pass

def norml(x):
  return (x - PIXEL_DEPTH/2.0) / PIXEL_DEPTH


def recv_array(socket, flags=0, copy=True, track=False):
  """recv a numpy array"""
  md = socket.recv_json(flags=flags)
  msg = socket.recv(flags=flags, copy=copy, track=track)
  buf = buffer(msg)
  A = np.frombuffer(buf, dtype=md['dtype'])
  return A.reshape(md['shape'])


def process_action(action):
   act = action.get('actions', [])
   if len(act) == 0:
     act = {'action_pos': [0, 0], 'force': [0, 0, 0], 'torque': [0, 0, 0]}
   else:
     act = act[0]
   actvec = action.get('vel', [0, 0, 0]) + action.get('ang_vel', [0, 0, 0]) + act.get('action_pos', [0, 0]) + act.get('force', [0, 0, 0]) + act.get('torque', [0, 0, 0])
   return actvec

def getNextBatch(N, rng):
  sock.send_json({'batch_num': N, 
                  'batch_size': 128})
  info = sock.recv_json()
  ims = norml(recv_array(sock))
  norms = norml(recv_array(sock))
  objs = recv_array(sock)

  obss = []
  time_diffs = []
  actionss = []
  future_inds = []  
  while (len(obss) < BATCH_SIZE):
    i = rng.randint(len(ims))
    if i < OBSERVATION_LENGTH - 1:
      continue
    _b = [info[j].get('teleport_random', False) for j in range(i-OBSERVATION_LENGTH+1, i+1)]
    if any(_b):
      continue
    
    for j in range(i, min(i+1+MAX_NUM_ACTIONS, len(info))):
      if info[j].get('teleport_random', False):
        break
    if j == i:
      continue
      
    t = rng.randint(i+1, j+1)

    if (t, t-i) in zip(future_inds, time_diffs):
      continue

    future_inds.append(t)
    time_diffs.append(t - i)
    
    newshape = (IMAGE_SIZE, IMAGE_SIZE, NUM_CHANNELS * OBSERVATION_LENGTH)
    obs = ims[i - OBSERVATION_LENGTH + 1: i + 1].transpose((1, 2, 3, 0)).reshape(newshape)
    obss.append(obs)
    
    fut = ims[t - OBSERVATION_LENGTH + 1: t + 1].transpose((1, 2, 3, 0)).reshape(newshape)
    futs.append(fut)    
    
    action_seq = []
    for _j in range(i, i + MAX_NUM_ACTIONS):
      if _j < len(info):
         action_seq.extend(process_action(info[_j]))
      else:
         action_seq.extend([0] * ATOMIC_ACTION_LENGTH)
    actionss.append(action_seq)

  batch = {'observations': np.array(obss),
           'future': np.array(futs),
           'actions': np.array(actionss),
           'time_diff': np.array(time_diffs) }

  return batch


def error_rate(predictions, imgs):
  """Return the error rate based on dense predictions and sparse labels."""
  return 0.5 * ((predictions - imgs)**2).mean()


def getEncodeDepth(rng, cfg):
  if 'encode_depth' in cfg:
    d = cfg['encode_depth'] 
  else:
    d = rng.choice([1, 2, 3, 4, 5])
    if 'encode' in cfg:
      maxv = max(cfg['encode'].keys())
      d = max(d, maxv)
  return d

def getEncodeConvFilterSize(i, encode_depth, rng, cfg, prev=None):
  if 'encode' in cfg and (i in cfg['encode']):
    if 'conv' in cfg['encode'][i]:
      if 'filter_size' in cfg['encode'][i]['conv']:
        return cfg['encode'][i]['conv']['filter_size']  
  L = [1, 3, 5, 7, 9, 11, 13, 15]
  if prev is not None:
    L = [_l for _l in L if _l <= prev]
  return rng.choice(L)

def getEncodeConvNumFilters(i, encode_depth, rng, cfg):
  if 'encode' in cfg and (i in cfg['encode']):
    if 'conv' in cfg['encode'][i]:
      if 'num_filters' in cfg['encode'][i]['conv']:
        return cfg['encode'][i]['conv']['num_filters']
  L = [3, 48, 96, 128, 256, 128]
  return L[i]

def getEncodeConvStride(i, encode_depth, rng, cfg):
  if 'encode' in cfg and (i in cfg['encode']):
    if 'conv' in cfg['encode'][i]:
      if 'stride' in cfg['encode'][i]['conv']:
        return cfg['encode'][i]['conv']['stride']
  if encode_depth > 1:
    return 2 if i == 1 else 1
  else:
    return 3 if i == 1 else 1

def getEncodeDoPool(i, encode_depth, rng, cfg):
  if 'encode' in cfg and (i in cfg['encode']):
    if 'do_pool' in cfg['encode'][i]:
      return cfg['encode'][i]['do_pool']
    elif 'pool' in cfg['encode'][i]:
      return True
  if i < 3 or i == encode_depth:
    return rng.uniform() < .75
  else:
    return rng.uniform() < .25

def getEncodePoolFilterSize(i, encode_depth, rng, cfg):
  if 'encode' in cfg and (i in cfg['encode']):
    if 'pool' in cfg['encode'][i]:
      if 'filter_size' in cfg['encode'][i]['pool']:
        return cfg['encode'][i]['pool']['filter_size']
  return rng.choice([2, 3, 5])

def getEncodePoolStride(i, encode_depth, rng, cfg):  
  if 'encode' in cfg and (i in cfg['encode']):
    if 'pool' in cfg['encode'][i]:
      if 'stride' in cfg['encode'][i]['pool']:
        return cfg['encode'][i]['pool']['stride']
  return 2

def getEncodePoolType(i, encode_depth, rng, cfg):
  if 'encode' in cfg and (i in cfg['encode']):
    if 'pool' in cfg['encode'][i]:
      if 'type' in cfg['encode'][i]['pool']:
        return cfg['encode'][i]['pool']['type']
  return rng.choice(['max', 'avg'])

def getHiddenDepth(rng, cfg):
  if 'hidden_depth' in cfg:
    return cfg['hidden_depth']
  else:
    d = rng.choice([1, 2, 3])
    if 'hidden' in cfg:
       maxv = max(cfg['hidden'].keys())
       d = max(d, maxv)
    return d
       
def getHiddenNumFeatures(i, hidden_depth, rng, cfg):
  if 'hidden' in cfg and (i in cfg['hidden']):
    if 'num_features' in cfg['hidden'][i]:
      return cfg['hidden'][i]['num_features']
  return 1024

def getDecodeDepth(rng, cfg):
  if 'decode_depth' in cfg:
    return cfg['decode_depth']
  else:
    d = rng.choice([1, 2, 3])
    if 'decode' in cfg:
      maxv = max(cfg['decode'].keys())
      d = max(d, maxv)
    return d

def getDecodeNumFilters(i, decode_depth, rng, cfg):
  if i < decode_depth:
    if 'decode' in cfg and (i in cfg['decode']):
      if 'num_filters' in cfg['decode'][i]:
        return cfg['decode'][i]['num_filters']
    return 32
  else:
    return NUM_CHANNELS

def getDecodeFilterSize(i, decode_depth, rng, cfg):
  if 'decode' in cfg and (i in cfg['decode']):
     if 'filter_size' in cfg['decode'][i]:
       return cfg['decode'][i]['filter_size']
  return 7

def getDecodeSize(i, decode_depth, init, final, rng, cfg):
  if 'decode' in cfg and (i in cfg['decode']):
    if 'size' in cfg['decode'][i]:
      return cfg['decode'][i]['size']
  s = np.log2(init)
  e = np.log2(final)
  increment = (e - s) / decode_depth
  l = np.around(np.power(2, np.arange(s, e, increment)))
  if len(l) < decode_depth + 1:
    l = np.concatenate([l, [final]])
  l = l.astype(np.int)
  return l[i]

def getDecodeBypass(i, encode_nodes, decode_size, decode_depth, rng, cfg):
  if 'decode' in cfg and (i in cfg['decode']):
    if 'bypass' in cfg['decode'][i]:
      return cfg['decode'][i]['bypass']
  switch = rng.uniform() 
  print('sw', switch)
  if switch < 0.5:
    sdiffs = [e.get_shape().as_list()[1] - decode_size for e in encode_nodes]
    return np.abs(sdiffs).argmin()

def getFilterSeed(rng, cfg):
  if 'filter_seed' in cfg:
    return cfg['filter_seed']
  else:  
    return rng.randint(10000)
  

def model(current, future, actions_node, time_node, rng, cfg):
  """The Model definition."""
  cfg0 = {} 

  fseed = getFilterSeed(rng, cfg)
  
  #encoding
  nf0 = NUM_CHANNELS * OBSERVATION_LENGTH
  imsize = IMAGE_SIZE
  encode_depth = getEncodeDepth(rng, cfg)
  cfg0['encode_depth'] = encode_depth
  print('Encode depth: %d' % encode_depth)
  encode_nodes_current = []
  encode_nodes_current.append(current)
  encode_nodes_future = []
  encode_nodes_future.append(future)
  cfs0 = None
  cfg0['encode'] = {}
  for i in range(1, encode_depth + 1):
    cfg0['encode'][i] = {}
    cfs = getEncodeConvFilterSize(i, encode_depth, rng, cfg, prev=cfs0)
    cfg0['encode'][i]['conv'] = {'filter_size': cfs}
    cfs0 = cfs
    nf = getEncodeConvNumFilters(i, encode_depth, rng, cfg)
    cfg0['encode'][i]['conv']['num_filters'] = nf
    cs = getEncodeConvStride(i, encode_depth, rng, cfg)
    cfg0['encode'][i]['conv']['stride'] = cs
    W = tf.Variable(tf.truncated_normal([cfs, cfs, nf0, nf],
                                        stddev=0.01,
                                        seed=fseed))
    new_encode_node_current = tf.nn.conv2d(encode_nodes_current[i-1], W,
                               strides = [1, cs, cs, 1],
                               padding='SAME')
    new_encode_node_current = tf.nn.relu(new_encode_node_current)
    new_encode_node_future = tf.nn.conv2d(encode_nodes_future[i-1], W,
                               strides = [1, cs, cs, 1],
                               padding='SAME')
    new_encode_node_future = tf.nn.relu(new_encode_node_future)
    b = tf.Variable(tf.zeros([nf]))
    new_encode_node_current = tf.nn.bias_add(new_encode_node_current, b)
    new_encode_node_future = tf.nn.bias_add(new_encode_node_future, b)
    imsize = imsize // cs
    print('Encode conv %d with size %d stride %d num channels %d numfilters %d for shape' % (i, cfs, cs, nf0, nf), new_encode_node_current.get_shape().as_list())    
    do_pool = getEncodeDoPool(i, encode_depth, rng, cfg)
    if do_pool:
      pfs = getEncodePoolFilterSize(i, encode_depth, rng, cfg)
      cfg0['encode'][i]['pool'] = {'filter_size': pfs}
      ps = getEncodePoolStride(i, encode_depth, rng, cfg)
      cfg0['encode'][i]['pool']['stride'] = ps
      pool_type = getEncodePoolType(i, encode_depth, rng, cfg)
      cfg0['encode'][i]['pool']['type'] = pool_type
      if pool_type == 'max':
        pfunc = tf.nn.max_pool
      elif pool_type == 'avg':
        pfunc = tf.nn.avg_pool
      new_encode_node_current = pfunc(new_encode_node_current,
                          ksize = [1, pfs, pfs, 1],
                          strides = [1, ps, ps, 1],
                          padding='SAME')
      new_encode_node_future = pfunc(new_encode_node_current_future,
                          ksize = [1, pfs, pfs, 1],
                          strides = [1, ps, ps, 1],
                          padding='SAME')                        
      print('Encode %s pool %d with size %d stride %d for shape' % (pool_type, i, pfs, ps),
                    new_encode_node_current.get_shape().as_list())
      imsize = imsize // ps
    nf0 = nf

    encode_nodes_current.append(new_encode_node_current)   
    encode_nodes_future.append(new_encode_node_future)

  encode_node = encode_nodes_current[-1]
  enc_shape = encode_node.get_shape().as_list()
  encode_flat = tf.reshape(encode_node, [enc_shape[0], np.prod(enc_shape[1:])])
  print('Flatten to shape %s' % encode_flat.get_shape().as_list())

  encode_flat = tf.concat(1, [encode_flat, actions_node, time_node]) 
  #hidden
  nf0 = encode_flat.get_shape().as_list()[1]
  hidden_depth = getHiddenDepth(rng, cfg)
  cfg0['hidden_depth'] = hidden_depth
  hidden = encode_flat
  cfg0['hidden'] = {}
  for i in range(1, hidden_depth + 1):
    nf = getHiddenNumFeatures(i, hidden_depth, rng, cfg)
    cfg0['hidden'][i] = {'num_features': nf}
    W = tf.Variable(tf.truncated_normal([nf0, nf],
                                        stddev = 0.01,
                                        seed=fseed))    
    b = tf.Variable(tf.constant(0.01, shape=[nf]))
    hidden = tf.nn.relu(tf.matmul(hidden, W) + b)
    print('hidden layer %d %s' % (i, str(hidden.get_shape().as_list())))
    nf0 = nf

  #decode
  ds = encode_nodes_future[encode_depth].get_shape().as_list()[1]
  nf1 = getDecodeNumFilters(0, encode_depth, rng, cfg)
  cfg0['decode'] = {0: {'num_filters': nf1}}
  if ds * ds * nf1 != nf0:
    W = tf.Variable(tf.truncated_normal([nf0, ds * ds * nf1],
                                        stddev = 0.01,
                                        seed=fseed))
    b = tf.Variable(tf.constant(0.01, shape=[ds * ds * nf1]))
    hidden = tf.matmul(hidden, W) + b
    print("Linear from %d to %d for input size %d" % (nf0, ds * ds * nf1, ds))
  decode = tf.reshape(hidden, [BATCH_SIZE, ds, ds, nf1])  
  print("Unflattening to", decode.get_shape().as_list())
  
  pred = tf.concat(3, [decode, encode_nodes_current[encode_depth]])
  nf = encode_nodes_future[encode_depth].get_shape().as_list()[-1]
  cfs = cfg0['encode'][encode_depth]['conv']['filter_size']
  W = tf.Variable(tf.truncated_normal([cfs, cfs, nf + nf1, nf],
										stddev=0.1,
										seed=fseed))
  b = tf.Variable(tf.zeros([nf]))
  pred = tf.nn.conv2d(pred,
						  W,
						  strides=[1, 1, 1, 1],
						  padding='SAME')
  pred = tf.nn.bias_add(pred, b)
  
  norm = (ds**2) * BATCH_SIZE * nf
  loss = tf.nn.l2_loss(pred - encode_nodes_future[encode_depth]) / norm
  for i in range(1, encode_depth + 1):
    nf0 = nf1
    ds = encode_nodes_future[encode_depth - i].get_shape().as_list()[1]
    decode = tf.image.resize_images(decode, ds, ds)
    print('Decode resize %d to shape' % i, decode.get_shape().as_list())
    cfs = getDecodeFilterSize(i, encode_depth, rng, cfg)
    cfg0['decode'][i] = {'filter_size': cfs}
    nf1 = getDecodeNumFilters(i, encode_depth, rng, cfg)
    cfg0['decode'][i]['num_filters'] = nf1
    W = tf.Variable(tf.truncated_normal([cfs, cfs, nf0, nf],
                                        stddev=0.1,
                                        seed=fseed))
    b = tf.Variable(tf.zeros([nf1]))
    decode = tf.nn.conv2d(decode,
                          W,
                          strides=[1, 1, 1, 1],
                          padding='SAME')
    decode = tf.nn.bias_add(decode, b)
    
    pred = tf.concat(3, [decode, encode_nodes_current[encode_depth - i]])
    
    cfs = cfg0['encode'][encode_depth - i]['conv']['filter_size']  #set some other way?
    nf = encode_nodes_future[encode_depth - i].get_shape().as_list()[-1]
    W = tf.Variable(tf.truncated_normal([cfs, cfs, nf + nf1, nf],
                                        stddev=0.1,
                                        seed=fseed))
    b = tf.Variable(tf.zeros([nf]))
    pred = tf.nn.conv2d(pred,
                        W,
                        strides=[1, 1, 1, 1],
                        padding='SAME')
    pred = tf.nn.bias_add(pred, b)

    if i < encode_depth:  #add relu to all but last ... need this?
      decode = tf.nn.relu(decode)
      pred = tf.nn.relu(pred)
    
    norm = (ds**2) * BATCH_SIZE * nf
    loss = loss + tf.nn.l2_loss(pred - encode_nodes_future[encode_depth - i]) / norm
 
  return loss, cfg0


def main(dbname, colname, experiment_id, seed=0, cfgfile=None, savedir='.', dosave=True, learningrate=1.0, decaystep=100000, decayrate=0.95):
  conn = pm.MongoClient('localhost', 29101)
  db = conn[dbname]
  coll = db[colname] 
  r = coll.find_one({"experiment_id": experiment_id, 'saved_filters': True})
  if r:
    init = False
    r = coll.find_one({'experiment_id': experiment_id, 'step': -1})
    assert r
    cfg1 = postprocess_config(r['cfg'])
    seed = r['seed']
    cfg0 = postprocess_config(r['cfg0'])
  else:  
    init = True
    cfg1 = None
    if cfgfile is not None:
      cfg0 = postprocess_config(json.load(open(cfgfile)))
    else:
      cfg0 = {}  
  
  rng = np.random.RandomState(seed=seed)

  current_node = tf.placeholder(
      tf.float32,
      shape=(BATCH_SIZE, IMAGE_SIZE, IMAGE_SIZE, NUM_CHANNELS * OBSERVATION_LENGTH))

  future_node = tf.placeholder(
        tf.float32,
      shape=(BATCH_SIZE, IMAGE_SIZE, IMAGE_SIZE, NUM_CHANNELS * OBSERVATION_LENGTH))

  actions_node = tf.placeholder(tf.float32,
                                shape=(BATCH_SIZE,
                                       ATOMIC_ACTION_LENGTH * MAX_NUM_ACTIONS))
  
  time_node = tf.placeholder(tf.float32,
                             shape=(BATCH_SIZE, 1))

  loss, cfg = model(current_node, future_node, actions_node, time_node, rng=rng, cfg=cfg0)
  if not init: 
    assert cfg1 == cfg, (cfg1, cfg)
  else:
    assert not coll.find_one({'experiment_id': experiment_id, 'saved_filters': True})
    rec = {'experiment_id': experiment_id,
           'cfg': preprocess_config(cfg),
           'seed': seed,
           'cfg0': preprocess_config(cfg0),
           'step': -1}
    coll.insert(rec)

  batch = tf.Variable(0, trainable=False)

  learning_rate = tf.train.exponential_decay(
      learningrate,                # Base learning rate.
      batch * BATCH_SIZE,  # Current index into the dataset.
      decaystep,          # Decay step.
      decayrate,                # Decay rate.
      staircase=True)

  optimizer = tf.train.MomentumOptimizer(learning_rate, 0.9).minimize(loss, global_step=batch)

  sdir = os.path.join(savedir, dbname, colname, experiment_id)
  if not os.path.exists(sdir):
    os.makedirs(sdir)

  start_time = time.time()
  with tf.Session() as sess:
    if init or not dosave:
      tf.initialize_all_variables().run()
      #saver = tf.train.Saver()
      print('Initialized!')
      step0 = -1
    else:
      step0 = max(coll.find({'experiment_id': experiment_id, 'saved_filters': True}).distinct('step'))
      #pathval = os.path.join(sdir, '%d.ckpt' % step0)
      Vars = tf.all_variables()
      for v in Vars:
        pth = get_checkpoint_path(sdir, v.name.replace('/', '__'), step0)
        val = np.load(pth)
        sess.run(v.assign(val))
      #assert os.path.exists(pathval)
      #saver = tf.train.Saver()
      #saver.restore(sess, pathval)
      
      print("Restored from %s at timestep %d" % (sdir, step0))

    for step in xrange(step0 + 1, NUM_TRAIN_STEPS // BATCH_SIZE):
      batch_data = getNextBatch(step, rng)
      #with open('/home/yamins/borkstep%d.p' % step, 'w') as _f:
      #  cPickle.dump(batch_data, _f)
      feed_dict = {current_node: batch_data['observations'],
                   future_node: batch_data['future'],
                   actions_node: batch_data['actions'],
                   time_node: batch_data['time_diff'][:, np.newaxis]}
      # Run the graph and fetch some of the nodes.
      _, l, lr, predictions = sess.run(
          [optimizer, loss, learning_rate, train_prediction],
          feed_dict=feed_dict)

      print(step, l, lr)
      if l > THRES:
        raise HiLossError("Loss: %.3f, Thres: %.3f" % (l, THRES))

      spath = os.path.join(sdir, 'predictions.npy')
      np.save(spath, predictions)
      if step % TEST_FREQUENCY == 0:
        #pathval = os.path.join(sdir, '%d.ckpt' % step)
        if dosave and (step % (TEST_FREQUENCY * SAVE_MULTIPLE) == 0):
          #save_path = saver.save(sess, pathval)
          Vars = tf.all_variables()
          for v in Vars:
            pth = get_checkpoint_path(sdir, v.name.replace('/', '__'), step)
            val = v.eval()
            np.save(pth, val)
          saved_filters = True
        else:
          saved_filters = False
        rec = {'experiment_id': experiment_id, 
               'cfg': preprocess_config(cfg),
               'saved_filters': saved_filters,
               'step': step,
               'loss': float(l),
               'learning_rate': float(lr)}
        coll.insert(rec)
         

def preprocess_config(cfg):
  cfg = copy.deepcopy(cfg)
  for k in ['encode', 'decode', 'hidden']:
    if k in cfg:
      ks = cfg[k].keys()
      for _k in ks:
        assert isinstance(_k, int), _k
        cfg[k][str(_k)] = cfg[k].pop(_k)
  return cfg

def postprocess_config(cfg):
  cfg = copy.deepcopy(cfg)
  for k in ['encode', 'decode', 'hidden']:
    if k in cfg:
      ks = cfg[k].keys()
      for _k in ks:
        cfg[k][int(_k)] = cfg[k].pop(_k)
  return cfg

def get_variable(name):
  return [_x for _x in tf.all_variables() if _x.name == name][0]

def get_checkpoint_path(dirn, vname, step):
  cdir = os.path.join(dirn, vname)
  if not os.path.exists(cdir):
    os.makedirs(cdir)
  return os.path.join(cdir, '%d.npy' % step)

        
if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('dbname', type=str, help="dbname string value")
  parser.add_argument('colname', type=str, help="colname string value")
  parser.add_argument('experiment_id', type=str, help="Experiment ID string value")
  parser.add_argument('--seed', type=int, help='seed for config', default=0)
  parser.add_argument('--cfgfile', type=str, help="Config to load model specs from")
  parser.add_argument('--savedir', type=str, default='.')
  parser.add_argument('--dosave', type=int, default=1)
  parser.add_argument('--learningrate', type=float, default=1.)
  parser.add_argument('--decaystep', type=int, default=100000)
  parser.add_argument('--decayrate', type=float, default=0.95)
  args = vars(parser.parse_args())
  main(**args) 
  
