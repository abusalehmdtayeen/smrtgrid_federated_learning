#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.6
#Helper Source: https://github.com/AshwinRJ/Federated-Learning-PyTorch/blob/master/src/federated_main.py

import os
import copy
import time
import pickle
import math
import numpy as np
import pandas as pd
from tqdm import tqdm

import matplotlib
import matplotlib.pyplot as plt
matplotlib.use('Agg')

import helper
import utils
import torch
from torch import nn
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import mean_squared_error
from sklearn.metrics import mean_absolute_error
from online_local_model import LocalModel
from models import LSTM

#==========PARAMETERS=======================
online_epochs = 1
global_epochs = 2
local_epochs = 2
frac = 0.7 #fraction of groups/meters to choose
split_ratio = 0.8 #split ratio for train data
test_len = 1440
test_range = 48 
normalize_data = True
window_size = 48
take_all = False #whether federated learning will be applied to all participants
num_participants = 50  #number of participants to be applied to federated learning
#===========================================

# torch.cuda.is_available() checks and returns a Boolean True if a GPU is available, else it'll return False
is_cuda = torch.cuda.is_available()

# If we have a GPU available, we'll set our device to GPU.
if is_cuda:
    device = torch.device("cuda:0")
    print("GPU is available")
else:
    device = torch.device("cpu")
    print("GPU not available, CPU used")

# define paths
base_path = os.getcwd()

#---------------------------------------------
def average_weights(w):
	"""
	Returns the average of the weights.
	"""
	w_avg = copy.deepcopy(w[0])
	for key in w_avg.keys():
		for i in range(1, len(w)):
			w_avg[key] += w[i][key]
		w_avg[key] = torch.div(w_avg[key], len(w))
    
	return w_avg

#----------------------------------------------
def global_inference(test_data, test_index, scaler, model):
	""" 
	Returns the inference and loss on global test data.
	"""
	model.to(device)
	test_tensor = torch.FloatTensor(test_data).view(-1)	
	test_seq = utils.create_inout_sequences(test_tensor, window_size)

	criterion = nn.MSELoss().to(device)

	model.eval()
	#print(next(model.parameters()).is_cuda)
	losses = []
	total_seq = 0
	test_predictions = []
	actual_predictions = []
	for indx, (seq, labels) in enumerate(test_seq):	
		if indx < test_index:
			continue
		seq, labels = seq.to(device), labels.to(device)
		with torch.no_grad():
			model.hidden_cell = (torch.zeros(1, 1, model.hidden_layer_size, device=device), torch.zeros(1, 1, model.hidden_layer_size, device=device))
			# Global Inference
			outputs = model(seq)
			batch_loss = criterion(outputs, labels)
			losses.append(batch_loss.item())
				
			test_predictions.append(outputs.item())
			actual_predictions.append(labels.item())
		total_seq += 1
		if indx > (test_index+test_range):
			break		

	if normalize_data and scaler is not None:
		actual_predictions = scaler.inverse_transform(np.array(actual_predictions).reshape(-1, 1))
		test_predictions = scaler.inverse_transform(np.array(test_predictions).reshape(-1, 1))
		
		test_predictions = test_predictions[:,0]
		actual_predictions = actual_predictions[:,0]      

	return actual_predictions, test_predictions, losses

#----------------------------------------------
def global_train_test(g_id):
	"""
	Returns train and test dataset for a given group.
	"""
	dataframe = pd.read_csv(base_path + "/data/group_load/"+str(g_id)+"_val"+".csv")
	
	all_data = dataframe['group_value'].values
	
	all_data = all_data.astype('float32')
	    
	if normalize_data:
		#perform min/max scaling on the dataset which normalizes the data within a certain range of minimum and maximum values. 
		scaler = MinMaxScaler(feature_range=(0, 1))
		all_data_normalized = scaler.fit_transform(all_data.reshape(-1, 1))
		#print(all_data_normalized)
		all_data = all_data_normalized
		

	# split into train and test sets (default: 80/20)
	if test_len is None:
		train_size = int(len(all_data) * split_ratio)
		test_size = len(all_data) - train_size
	else:
		train_size = len(all_data) - test_len		
		test_size = test_len
		
	train_data, test_data = all_data[:train_size], all_data[train_size:len(all_data)]
	if not normalize_data:
		train_data = train_data.reshape(-1, 1)
		test_data = test_data.reshape(-1, 1)
	
	return train_data, test_data, scaler

#--------------------------------------------
if __name__ == '__main__':
	start_time = time.time()

    # load meter groups
	#groups = helper.find_filenames_ext(base_path + "/data/group_load/")

	gid = 'g1' #group id of the meters 
	#-------SET group ids from data folder~~~~~~~~~~~
	#group_ids = [ group[ : group.rindex("_")] for group in groups] 

	global_train, global_test, global_scaler = global_train_test(gid)
	global_test_max = np.amax(global_test)	
	global_test_min = np.amin(global_test)

	meter_ids = helper.read_txt(base_path + "/data/group_ids/" + gid)
	#~~~~~~~~SET meter_ids from folder~~~~~~~~~~~~~~
	group_ids = [ int(meter) for meter in meter_ids ] #for this experiment each group corresponds to one meter 
	#~~~~~~~~SET meter/group ids manually~~~~~~~~~~~~
	#group_ids = [4820, 2826, 7370]
	#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~	
	num_groups = len(group_ids)	
	#num_groups = len(groups)

	# create model object
	global_model = LSTM()
    
    # Set the model to train and send it to device.
	global_model.to(device)
	global_model.train()
    #print(global_model)
    # copy weights
	global_weights = global_model.state_dict()

    # Training
	train_loss = []
    
	#~~~~~~~~~~~~~~~~~CHOOSE ALL GROUPS~~~~~~~~~~~~~~~
	local_models = []	
	if take_all:		
		group_indices = np.arange(num_groups)
		for indx in group_indices:
			local_model = LocalModel(group_ids[indx], split_ratio, test_len, normalize=normalize_data, window=window_size, local_epochs=local_epochs, device=device)
			local_models.append(local_model)
	#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

	for epoch in range(global_epochs):
		local_weights, local_losses = [], []
	
		global_model.train()
		#~~~~~~~~~~~~~~~~~CHOOSE FRACTION OF ALL GROUPS RANDOMLY IN EACH EPOCH~~~~~~~~~~~ 
		if not take_all:
			m = max(int(frac * num_groups), 1)
			if num_participants is None: 
				group_indices = np.random.choice(range(num_groups), m, replace=False)
			else:
				group_indices = np.random.choice(range(num_groups), num_participants, replace=False)
		#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

		for indx in tqdm(group_indices):
			print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
			print("Training group with ID: %s"%str(group_ids[indx]))
			#~~~~~~~~~~~~~~~when FRACTION OF ALL GROUPS are choosen randomly~~~~~~~~~~~~
			if not take_all:
				local_model = LocalModel(group_ids[indx], split_ratio, test_len, normalize=normalize_data, window=window_size, local_epochs=local_epochs, device=device)
				#local_models.append(local_model)
			#~~~~~~~~~~~~~~~~~when ALL GROUPS are chosen~~~~~~~~~~~~~~~~~~~~~~~~~~~
			else:			
				local_model = local_models[indx]
			#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
			w, loss = local_model.update_weights(model=copy.deepcopy(global_model), global_round=epoch+1)
			local_weights.append(copy.deepcopy(w))
			local_losses.append(copy.deepcopy(loss))

        # update global weights
		global_weights = average_weights(local_weights)

        # update global weights
		global_model.load_state_dict(global_weights)

		loss_avg = sum(local_losses) / len(local_losses)
		train_loss.append({'epoch': epoch, 'locals_loss_avg': loss_avg})
		print("---------------------------------------------------")
		print('Global Training Round : {}, Average loss {:.3f}'.format(epoch+1, loss_avg))
		print("---------------------------------------------------")

	print('\nTotal Training Time: {0:0.4f}'.format(time.time()-start_time))
	#------------------------------------------------------------------------
	helper.make_dir(base_path, "results")
	helper.write_csv(base_path + "/results/online-federated-"+gid+"-train-avg-loss-t"+str(test_range), train_loss, ["epoch", "locals_loss_avg"])


	#~~~~~~~~~~~~~~~~~ONLINE TRAINING~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
	print ("~~~~Started Online Training~~~~~~~~")
	meter_predictions = {}
	global_predictions = {}
	global_model.to(device)

	for epoch in range(online_epochs):
		local_weights, local_losses = [], []
	
		global_model.train()
		#~~~~~~~~~~~~~~~~~CHOOSE FRACTION OF ALL GROUPS RANDOMLY IN EACH EPOCH~~~~~~~~~~~ 
		if not take_all:
			m = max(int(frac * num_groups), 1)
			if num_participants is None: 
				group_indices = np.random.choice(range(num_groups), m, replace=False)
			else:
				group_indices = np.random.choice(range(num_groups), num_participants, replace=False)
		#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
		test_index = 0
		while test_index < test_len-window_size:
			print("Predicting Test Point: %d"%test_index)
			#~~~~~~~~~~~~~~~~~~~~Performance of Aggregator using global model~~~~~~~~~~~~~~~~~~~~~~~~~~~~
			actual_values, predicted_values, test_losses = global_inference(global_test, test_index, global_scaler, global_model)
			if 'act' not in global_predictions:
				global_predictions['act'] = actual_values
			else:
				global_predictions['act'] = np.concatenate( (global_predictions['act'], actual_values) )

			if 'pred' not in global_predictions:
				global_predictions['pred'] = predicted_values
			else:
				global_predictions['pred'] = np.concatenate( (global_predictions['pred'], predicted_values) )
			#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

			for indx in tqdm(group_indices):
				meter_id = group_ids[indx]
				print("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
				print("Training meter with ID: %s"%str(group_ids[indx]))
				#~~~~~~~~~~~~~~~when FRACTION OF ALL GROUPS are choosen randomly~~~~~~~~~~~~
				if not take_all:
					local_model = LocalModel(group_ids[indx], split_ratio, test_len, test_range, normalize=normalize_data, window=window_size, local_epochs=local_epochs, device=device)
					#local_models.append(local_model)
				#~~~~~~~~~~~~~~~~~when ALL GROUPS are chosen~~~~~~~~~~~~~~~~~~~~~~~~~~~
				else:			
					local_model = local_models[indx]
				#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
				w, loss, act_values, pred_values = local_model.infer_and_update_weights(test_index, model=copy.deepcopy(global_model), global_round=epoch+1)
				local_weights.append(copy.deepcopy(w))
				local_losses.append(copy.deepcopy(loss))

				#store the meters predictions
				if meter_id not in meter_predictions:
					meter_predictions[meter_id] = {'act': act_values, 'pred': pred_values}
				else:
					meter_predictions[meter_id]['act'] = np.concatenate( (meter_predictions[meter_id]['act'],  act_values) )
					meter_predictions[meter_id]['pred'] = np.concatenate( (meter_predictions[meter_id]['pred'], pred_values) )

        	# update global weights
			global_weights = average_weights(local_weights)

        	# update global weights
			global_model.load_state_dict(global_weights)

			test_index = test_index+test_range
			
			loss_avg = sum(local_losses) / len(local_losses)

		#train_loss.append({'epoch': epoch, 'locals_loss_avg': loss_avg})
		print("---------------------------------------------------")
		print('Online Global Training Round : {}, Average loss {:.3f}'.format(epoch+1, loss_avg))
		print("---------------------------------------------------")

		global_metrics = []
			
		rmse = math.sqrt(mean_squared_error(global_predictions['act'], global_predictions['pred']))
		nrmse = rmse / (global_test_max - global_test_min)
		mae = mean_absolute_error(global_predictions['act'], global_predictions['pred'])		
		global_metrics.append({'group_id': gid, 'RMSE': rmse, 'NRMSE': nrmse, 'MAE': mae})

		print("Global RMSE of group %s: %.2f"%(gid, rmse))
		print('Global NRMSE of group %s : %.2f' %(gid, nrmse))
		print('Global MAE of group %s : %.2f' %(gid, mae))
		print("---------------------------------------------------")
		helper.write_csv(base_path + "/results/online-federated-global"+"-e"+str(epoch)+"-t"+str(test_range), global_metrics, ["group_id", "RMSE", "NRMSE", "MAE"])
		#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~	
	
		rmse_list = []
		
		for indx in tqdm(group_indices):
			meter_id = group_ids[indx]
			if not take_all:
				local_model = LocalModel(group_ids[indx], split_ratio, test_len, test_range, normalize=normalize_data, window=window_size, local_epochs=local_epochs, device=device)
			else:
				local_model = local_models[indx]
		
			test_data_max = np.amax(local_model.test)	
			test_data_min = np.amin(local_model.test)

			act_values = meter_predictions[meter_id]['act'] 
			pred_values = meter_predictions[meter_id]['pred']
		
			rmse = math.sqrt(mean_squared_error(act_values, pred_values))
			nrmse = rmse / (test_data_max - test_data_min)
			mae = mean_absolute_error(act_values, pred_values)		
			print("RMSE of meter %s: %.2f"%(meter_id, rmse))
			print('NRMSE of meter %s : %.2f' %(meter_id, nrmse))
			print('MAE of meter %s : %.2f' %(meter_id, mae))
			print("---------------------------------------------------")

			rmse_list.append({'meter_id': meter_id, 'RMSE': rmse, 'NRMSE': nrmse, 'MAE': mae})   	
 
		helper.write_csv(base_path + "/results/online-federated-local-"+gid+"-e"+str(epoch)+"-t"+str(test_range), rmse_list, ["meter_id", "RMSE", "NRMSE", "MAE"])
	
	print('\nTotal Run Time: {0:0.4f}'.format(time.time()-start_time))
    #---------------------------------------------------------
	
	
	