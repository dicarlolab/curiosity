import os
import copy

import numpy as np

import curiosity.utils.base as base
import curiosity.models.future_pred_asymmetric_with_bypass as modelsource
import curiosity.datasources.images_futurediffs_and_actions as datasource

dbname = 'threeworld_future_pred'
colname = 'test_asymmetric_with_bypass'
experiment_id = 'test0_diff_lm100_lowlr'
model_func = modelsource.get_model
model_func_kwargs = {"host": "18.93.3.135",
                     "port": 23044,
                     "datapath": "/data2/datasource6",
                     "keyname": "randompermpairs3_medium",
                     "loss_multiple": 100}
data_func = datasource.getNextBatch
data_func_kwargs = copy.deepcopy(model_func_kwargs)
data_func_kwargs.pop('loss_multiple')
num_train_steps = 20480000
batch_size = 128
slippage = 0
SKDATA_ROOT = os.environ['SKDATA_ROOT']
CODE_ROOT = os.environ['CODE_ROOT']
cfgfile = os.path.join(CODE_ROOT, 
                       'curiosity/curiosity/configs/normals_config_winner0.cfg')
savedir = os.path.join(SKDATA_ROOT, 'futurepredopt')
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
         base_learningrate=0.05,
         loss_threshold=10000,
         decaystep=decaystep)
