import copy

import numpy as np

import curiosity.utils.base as base
import curiosity.models.normal_encoder_asymmetric_with_bypass as modelsource
import curiosity.datasources.images_and_normals as datasource

dbname = 'threeworld_normals'
colname = 'test'
experiment_id = 'test0_longdr'
model_func = modelsource.get_model
model_func_kwargs = {"host": "18.93.3.135",
                     "port": 23044,
                     "datapath": "/data2/datasource6"}
data_func = datasource.getNextBatch
data_func_kwargs = copy.deepcopy(model_func_kwargs)
num_train_steps = 20480000
batch_size = 128
slippage = 0
cfgfile = '/home/yamins/curiosity/curiosity/configs/normals_config_winner0.cfg'
savedir = '/data/normalsopt'
erase_earlier = 3
decaystep=1024000

base.run(dbname,
         colname,
         experiment_id,
         model_func,
         model_func_kwargs,
         data_func,
         data_func_kwargs,
         num_train_steps,
         batch_size,
         slippage=slippage,
         cfgfile=cfgfile,
         savedir=savedir,
         erase_earlier=erase_earlier,
         base_learningrate=1.0,
         decaystep=decaystep)
