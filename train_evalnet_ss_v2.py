# -*- coding: utf-8 -*-
"""
Created on Tue Jun 18 18:17:28 2019

@author: ld
"""

import torch.optim as optim
import torch
import torchvision
import torch.nn as nn
import torch.nn.functional as F
import os
import argparse
import time

import numpy as np

# import matplotlib.pyplot as plt
from pre_color import plot_label

from sklearn.metrics import accuracy_score
from sklearn.metrics import recall_score
from sklearn.metrics import cohen_kappa_score
from processing_library import load_data, one_hot, disorder, next_batch, next_batch_unl
from processing_library import contrary_one_hot, expend, windowFeature, pca_trans_expend
from processing_library import save_result, random_cut_bands
import copy
from processing_library import index_band_selection

from net import Net
os.environ["CUDA_VISIBLE_DEVICES"] = '1'

parser = argparse.ArgumentParser()

parser.add_argument('--data_name', type=str, default='PaviaU',
                    help='数据集名称')
parser.add_argument('--patch_size', type=int, default=31,
                    help='窗口大小')
parser.add_argument('--batch_size', type=int, default=128,
                    help='窗口大小')
parser.add_argument('--learning_rate', type=float, default=0.0005,
                    help='学习率')
parser.add_argument('--num_epoch', type=int, default=2000,
                    help='epoch')
parser.add_argument('--lambda_center_Loss', type=float, default=.5,
                    help='center_Loss占loss的比例')
parser.add_argument('--lambda_center_Loss_us', type=float, default=.5,
                    help='无监督centerloss占loss的比例')
parser.add_argument('--eval_net_path', type=str, default = 'eval_net_train',
                    help='evalnet的权值保存位置')
FLAGS, unparsed = parser.parse_known_args()


###############################################################################
# data_norm,labels_ori,x_train,y_train,train_loc,x_test,y_test,test_loc=load_data('Indian_pines')
data_norm, labels_ori, y_train, train_loc, y_test, test_loc, y_val, val_loc, _ = load_data(
    FLAGS.data_name)
ulab_loc = val_loc  # Caution！
train_num = int(0.5* train_loc.shape[0])
# print(train_num)
print(train_loc.shape[0])
train_loc = train_loc[:train_num,:]
y_train = y_train[:train_num]

nrows, ncols, ndim = data_norm.shape
dim_input = ndim
# dim_input = ndim
batch_size = FLAGS.batch_size
display_step = 100
step = 1
index = batch_size

lr_intit = FLAGS.learning_rate  # 初始学习率【调参】
lr_decay_step = 1000 # 每过lr_decay_step个step，学习就下降0.9
lr_decay_rate = 0.9
num_classification = int(np.max(labels_ori))  # 类别数
w = 31  # 图像块大小

num_epoch = FLAGS.num_epoch  # 训练循环次数

pca_dum_out = 5

data_norm = expend(data_norm, w)

#data2_norm = expend(data2_norm,w)

dim_input = np.shape(data_norm)[2]

# import os
# os.environ["CUDA_VISIBLE_DEVICES"]='0'
###############################################################################
# load data

Y_train = one_hot(y_train, num_classification)
Y_test = one_hot(y_test, num_classification)
Y_val = one_hot(y_val, num_classification)

train_loc, Y_train = disorder(train_loc, Y_train)
test_loc, Y_test = disorder(test_loc, Y_test)
val_loc, Y_val = disorder(val_loc, Y_val)
###############################################################################

data_norm = torch.tensor(data_norm, dtype=torch.float32)
train_loc = torch.tensor(train_loc, dtype=torch.int32)
Y_train = torch.tensor(Y_train, dtype=torch.long)
ulab_loc = torch.tensor(ulab_loc, dtype=torch.int32)
###############################################################################
newdata = torch.zeros(
        [1024, w, w, data_norm.shape[2]])
def _cut_bands_by_index(self, data, index):
    '''在GPU中完成，以实现加速'''
    data_new = torch.zeros_like(data)
    data_new[:, index, :, :] = data[:, index, :, :]
    return data_new
def random_cut_bands_torch(x):
    '''
    按均匀分布随机选择波段集合
    '''
    
    num_bands = x.shape[1]
    num_zero = num_bands - int(np.random.uniform(low=1, high=num_bands))
    index_zero_bands = np.random.choice(range(num_bands),num_zero,replace=False)
    x[:,index_zero_bands,:,:] = 0
    return x

def get_oa(data, X_valid_loc, Y_valid):
    # X_valid_loc = X_valid_loc.transpose()

    size = np.shape(X_valid_loc)
    num = size[0]
    index_all = 0
    step_ = 200
    y_pred = []
    while index_all < num:
        if index_all + step_ > num:
            input_loc = X_valid_loc[index_all:, :]
        else:
            input_loc = X_valid_loc[index_all:(index_all+step_), :]
        input = _windowFeature_torch(data, input_loc, w).permute(
            0, 3, 1, 2)
        input = random_cut_bands_torch(input)
#        input = torch.tensor(x, dtype=torch.float32)
#        input = input.reshape([input.shape[0],input.shape[1],input.shape[2],input.shape[3],1])
        index_all += step_

        temp1, _ = net(input)
        y_pred1 = contrary_one_hot(temp1.cpu()).astype('int32')
        y_pred.extend(y_pred1)
    y = contrary_one_hot(Y_valid).astype('int32')
    return y_pred, y


def _contrary_one_hot_torch(label):
    label_ori = torch.zeros([label.shape[0], ], dtype=torch.long)
    for i in range(label.shape[0]):
        label_ori[i] = torch.argmax(label[i])
    return label_ori


def _windowFeature_torch(data_expand, loc, w, newdata=newdata):
    newdata = torch.zeros_like(newdata)
    for i in range(loc.shape[0]):
        x1 = loc[i, 0]  # 没错！
        y1 = loc[i, 1]
        x2 = loc[i, 0] + w
        y2 = loc[i, 1] + w
        c = data_expand[x1:x2, y1:y2, :]
        newdata[i, :, :, :] = c
    return newdata[:loc.shape[0],:,:,:]


def center_Loss(feature, y_true, num_classification):
    # 有监督的centerloss
    total_center = torch.mean(feature, axis=0, keepdim=True)
#    print(feature,y_true)
#    total_center_tile=total_center.view([1,1,feature.shape[1]]).repeat([feature.shape[0],num_classification,1])
#    print(total_center_tile.shape)
    class_center = torch.tensor([], dtype=torch.float32).view(
        [-1, feature.shape[1]])  # 类内中心

    for i in range(num_classification):
        index = torch.where(y_true == torch.tensor(
            i, dtype=torch.long))[0]
#        print("index",index)
        if index.shape[0] != 0:
            result = torch.mean(feature[index, :], axis=0, keepdim=True)
#            print("1",result.shape)
        else:
            result = total_center
#            print("2",result.shape)
        class_center = torch.cat([class_center, result])
#        print("30",class_center.shape)
    '''有标签样本特征类内距离'''
    class_center_gather = class_center[y_true]
    loss_wtl = torch.mean(torch.mean(torch.abs(
        feature-class_center_gather), axis=1, keepdim=True).view([feature.shape[0]]))
    return loss_wtl


#        result=tf.reshape(result,[1,n])
#        class_center=torch.concat([class_center,result],0)

def center_Loss_us(feature, y_pred_us, num_classification):
    # 无监督的centerloss
    y_afs = F.softmax(y_pred_us, dim=-1)
#    print(y_afs.shape)
#    print(torch.argmax(y_afs,axis =1))
    y_afs_tile = y_afs.view([feature.shape[0], num_classification, 1]).repeat(
        [1, 1, feature.shape[1]])
    feature_unl_tile = feature.view(
        [feature.shape[0], 1, feature.shape[1]]).repeat([1, num_classification, 1])

    pij_sum = torch.sum(y_afs, axis=0, keepdim=True).view(
        [num_classification, 1]).repeat([1, feature.shape[1]])

    class_center_unl = torch.div(torch.sum(torch.mul(feature_unl_tile, y_afs_tile), axis=0, keepdim=True).view(
        [num_classification, feature.shape[1]]), pij_sum)

    class_center_unl_tile1 = (class_center_unl.view(
        [1, num_classification, feature.shape[1]]).repeat([feature.shape[0], 1, 1]))
    '''无标签样本特征类内距离，固定/不固定特征中心，没有太大差别，采用固定的方式loss_wtu1'''
    loss_wtu1 = torch.sum(torch.mul(torch.mean(torch.abs(feature_unl_tile-class_center_unl_tile1),
                                               axis=2).view([feature.shape[0], num_classification]), y_afs))/(feature.shape[0])

    return loss_wtu1


net = Net(input_bands=ndim, num_classification=num_classification)
#net = torch.nn.DataParallel(net)
criterion = nn.CrossEntropyLoss()
optimizer = optim.RMSprop(net.parameters(), lr=lr_intit, momentum=0.9)
#optimizer = optim.Adam(net.parameters(), lr=lr_intit)

#############training###################################################
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

print(device)
net.to(device)
# net = torch.nn.DataParallel(net, [0,1])

epoch = 0
step = 1
running_loss = 0.0
time_train_start = time.perf_counter()
index = batch_size

#print('===> Try resume from checkpoint')
#if os.path.isdir('checkpoint'):
#    try:
#        checkpoint = torch.load('./checkpoint/Indian_pines.t7')
#        net.load_state_dict(checkpoint['state'])        # 从字典中依次读取
#        start_epoch = checkpoint['epoch']
#        print('===> Load last checkpoint data')
#    except FileNotFoundError:
#        print('Can\'t found net.t7')
#else:
#    start_epoch = 0
#    print('===> Start from scratch')
print("Start training...")

while epoch < num_epoch:

    with torch.no_grad():
        batch_train_loc, batch_y_one_hot = next_batch(
            train_loc, Y_train, index, batch_size)
        batch_unl_loc = next_batch_unl(ulab_loc, index, batch_size)
        batch_x = _windowFeature_torch(data_norm, batch_train_loc, w).permute(
            0, 3, 1, 2)
        batch_x_unl = _windowFeature_torch(data_norm, batch_unl_loc, w).permute(
            0, 3, 1, 2)
        batch_y = _contrary_one_hot_torch(
            batch_y_one_hot)
        batch_x = random_cut_bands_torch(batch_x)
        batch_x_unl = random_cut_bands_torch(batch_x_unl)
    optimizer.zero_grad()

    outputs, feature = net(batch_x)

    outputs_unl, feature_unl = net(batch_x_unl)

    loss_wrt = center_Loss(feature, batch_y, num_classification)

    loss_unl = center_Loss_us(feature_unl, outputs_unl, num_classification)

    loss = criterion(outputs, batch_y) + loss_wrt*FLAGS.lambda_center_Loss + loss_unl*FLAGS.lambda_center_Loss_us

    loss.backward()
    optimizer.step()  # 更新分类层权值
    running_loss += loss.item()
    if step % display_step == 0:    # print every 2000 mini-batches
        print('[%d, %5d] loss: %.7f' %
              (epoch + 1, step + 1, running_loss / 100),
              " | loss_wrt: %.5f" % (loss_wrt.data),
              " | loss_unl: %.5f" % (loss_unl.data),
              " | learning_rate: %.5f" % (optimizer.param_groups[0]["lr"]))
        running_loss = 0.0
        with torch.no_grad():
            y_pr, y_real = get_oa(data_norm, test_loc, Y_test)
            oa = accuracy_score(y_real, y_pr)
            per_class_acc = recall_score(y_real, y_pr, average=None)
            aa = np.mean(per_class_acc)
            kappa = cohen_kappa_score(y_real, y_pr)
            time_test_end = time.perf_counter()
            # print(per_class_acc)
            print("OA: %.4f" % oa,
                  " | AA:  %.4f" % aa,
                  " | Kappa:  %.4f" % kappa,
                  " | Time: %.1f" %(time_test_end-time_train_start)) 
            time_train_start = time_test_end
    if step % lr_decay_step == 0:
        # 学习率衰减
        for p in optimizer.param_groups:
            p['lr'] *= lr_decay_rate

    index = index+batch_size
    step += 1
#            print(step)
    if index > train_loc.shape[0]:
        index = batch_size
#                print('epoch:',epoch)
        epoch = epoch+1

        # ReShuffle the data
        perm = np.arange(len(train_loc))
        np.random.shuffle(perm)
        train_loc = train_loc[perm]
        Y_train = Y_train[perm]

        perm0 = np.arange(len(ulab_loc))
        np.random.shuffle(perm0)
        ulab_loc = ulab_loc[perm0]
        # Start next epoch
time_train_end = time.perf_counter()
print('Finished Training')

# save model
state = {
    'state': net.state_dict(),
    'epoch': epoch                   # 将epoch一并保存
}
if not os.path.isdir('checkpoint'):
    os.mkdir('checkpoint')
torch.save(state, './checkpoint/'+FLAGS.data_name+'.t7')
print('model saved')


##############################################################################

with torch.no_grad():
    y_pr, y_real = get_oa(data_norm, test_loc, Y_test)
    oa = accuracy_score(y_real, y_pr)
    per_class_acc = recall_score(y_real, y_pr, average=None)
    aa = np.mean(per_class_acc)
    kappa = cohen_kappa_score(y_real, y_pr)
    time_test_end = time.perf_counter()
    print(per_class_acc)
    print(oa, aa, kappa)

    with open(FLAGS.eval_net_path+"/result.txt", "a") as f:
        f.write("score:"+" OA: "+str(oa)+" AA"+str(aa)+" kappa:"+str(kappa))
        f.write("\n\n")
# exit()
