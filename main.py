import argparse
import os
import random
import torch
import torchvision
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torchvision.transforms as transforms
import copy
import torchvision.utils as vutils
from vfl import Client, Server, VFLNN
from our_attack import attack_test, pseudo_training, cal_test
from model import cifar_mobilenet, cifar_decoder, cifar_discriminator_model, vgg16, cifar_pseudo, bank_net, bank_pseudo, bank_discriminator,bank_decoder
import numpy as np
from torch.utils.data import Subset
from random import shuffle
import math
from agn import AGN_training
from fsha import fsha
from datasets import ExperimentDataset, getSplittedDataset
import time
import logging
import argparse
import pytz
from datetime import datetime
from logging import Formatter

# 设置时区为北京时间
class BeijingFormatter(Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, pytz.timezone('Asia/Shanghai'))
        if datefmt:
            s = dt.strftime(datefmt)
        else:
            s = dt.isoformat()
        return s

# INFO级别以上的日志会记录到日志文件，critical级别的日志会输出到控制台
def initlogging(logfile):
    # debug, info, warning, error, critical
    # set up logging to file
    logging.shutdown()
    
    logger = logging.getLogger()
    logger.handlers = []
    # 设置日志记录级别为INFO，即只有INFO级别及以上的会被记录
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        filename=logfile,
                        filemode='w')
    
    for handler in logging.getLogger().handlers:
        handler.setFormatter(BeijingFormatter('%(asctime)s - %(levelname)s - %(message)s'))
    
    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(logging.CRITICAL) # 只有critical级别的才会输出到控制台
    # add formatter to ch
    ch.setFormatter(logging.Formatter('%(message)s')) # 控制台只输出日志消息的内容
    logging.getLogger().addHandler(ch)  

def save_model(model, path):
    os.makedirs(os.path.join(path, 'pseudo'), exist_ok=True)
    os.makedirs(os.path.join(path, 'pseudo_inverse_model'), exist_ok=True)
    

def main():
    parser = argparse.ArgumentParser(description="VFL of implementation")
    parser.add_argument('--iteration', type=int, default=12000, help="")
    parser.add_argument('--lr', type=float, default=1e-4, help="the learning rate of pseudo_inverse model")
    parser.add_argument('--dlr', type=float, default=1e-4, help="the learning rate of discriminate")
    parser.add_argument('--batch_size', type=int, default=64, help="")
    parser.add_argument('--print_freq', type=int, default='50', help="the print frequency of ouput")
    parser.add_argument('--dataset', type=str, default='cifar10', help="the test dataset")
    parser.add_argument('--level', type=int, default=2, help="the split layer of model")
    parser.add_argument('--dataset_portion', type=float, default=0.05, help="the size portion of auxiliary data")
    parser.add_argument('--train_portion', type=float, default=0.7, help="the train_data portion of bank/drive data")
    parser.add_argument('--test_portion', type=float, default=0.3, help="the test portion of bank.drive data")
    parser.add_argument('--attack', type=str, default='our', help="the type of attack agn, our, fsha, grna")
    parser.add_argument('--loss_threshold', type=float, default=1.8, help="the loss flag of our attack")
    parser.add_argument('--if_update', action='store_true', help="the flag of update the pseudo model")

    args = parser.parse_args()
    print(args.if_update)

    gid = '0'
    date_time_file = datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d-%H-%M-%S")


    if args.dataset == 'bank':
        dataset_path = 'data/bank_cleaned.csv'
        vfl_input_dim = 10
        vfl_output_dim = 2
        cat_dimension = 1 # 拼接维度
    elif args.dataset == 'drive':
        dataset_path = 'data/drive_cleaned.csv'
        vfl_input_dim = 24
        vfl_output_dim = 11
        cat_dimension = 1 # 拼接维度
    # dataset_num = 1524
    
     # 固定初始化，可重复性
    torch.manual_seed(3407)
    random.seed(3407)
    np.random.seed(3407)
    cudnn.deterministic = True
    cudnn.benchmark = False

    
    path_name = os.path.join('log', args.attack, args.dataset)
    os.makedirs(path_name, exist_ok=True)
    initlogging(logfile=os.path.join(path_name, date_time_file + '.log'))
    logging.info(">>>>>>>>>>>>>>Running settings>>>>>>>>>>>>>>")
    for arg in vars(args):
        logging.info("%s: %s", arg, getattr(args, arg))
    logging.info(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>\n\n")

    use_cuda = torch.cuda.is_available()
    if use_cuda:
        id = 'cuda:'+ gid
        device = torch.device(id)
        torch.cuda.set_device(id)
        # cudnn.benchmark = True
    else:
        device = torch.device('cpu')

    print(device)
   

    cinic_transform = transforms.Compose([
                transforms.ToTensor(),
                transforms.Normalize((0.5,0.5,0.5), (0.5,0.5,0.5)),
            ])
    if args.dataset == 'cifar10':
        train_dataset = torchvision.datasets.CIFAR10(root='./data', train = True, transform=cinic_transform, download=True)
        test_dataset = torchvision.datasets.CIFAR10(root='./data', train = False, transform=cinic_transform, download=True)
        # 取2500/5000个私有数据
        dataset_num = len(train_dataset) * args.dataset_portion
        shadow_dataset = Subset(test_dataset, range(0, int(dataset_num)))
        cat_dimension = 3
    else: # bank 数据集
        bank_expset = ExperimentDataset(datafilepath=dataset_path)
        train_dataset, test_dataset = getSplittedDataset(args.train_portion, args.test_portion, bank_expset)
        dataset_num = len(train_dataset) * args.dataset_portion
        shadow_dataset = Subset(test_dataset, range(0, dataset_num))
        cat_dimension = 1
   

    logging.info("DataSet:%s", args.dataset)
    logging.info("Train Dataset: %d",len(train_dataset))
    logging.info("Test Dataset: %d",len(test_dataset))
    logging.info("Shadow Dataset:%d",len(shadow_dataset))
    train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size= args.batch_size, shuffle=True, num_workers = 4, pin_memory = True)
    test_dataloader = torch.utils.data.DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers = 4, pin_memory = True)
    shadow_dataloader = torch.utils.data.DataLoader(shadow_dataset, batch_size=args.batch_size, shuffle=True, num_workers = 4, pin_memory = True)

    if args.dataset == 'cifar10':
        target_bottom1, target_top = cifar_mobilenet(args.level)
        target_bottom2 = copy.deepcopy(target_bottom1)
        data_shape = train_dataset[0][0].shape
        pseudo_model, _ = vgg16(args.level, batch_norm = True)
        test_data = torch.ones(1,data_shape[0], data_shape[1], data_shape[2])
        with torch.no_grad():
            test_data_output = pseudo_model(test_data)
            discriminator_input_shape = test_data_output.shape[1:] # 除去第0维以后的维度，0维是批次大小
        print(discriminator_input_shape) # 中间特征大小

        d_input_shape = 3 if args.attack == 'agn' else discriminator_input_shape[0]
        agn = True if args.attack == 'agn' else False
        # 初始化鉴别器, agn==3
        discriminator = cifar_discriminator_model(d_input_shape, args.level, agn)
        # 初始化逆网络(inchannel, levle, outchannel)
        pseudo_inverse_model = cifar_decoder(discriminator_input_shape, args.level, data_shape[0])
    else:
        target_bottom1, target_top = bank_net(input_dim=vfl_input_dim, output_dim=vfl_output_dim)
        target_bottom2 = copy.deepcopy(target_bottom1)
        data_shape = train_dataset[0][0].shape
        test_data = torch.ones(1, data_shape[0])
        with torch.no_grad():
            test_data_output = target_bottom1(test_data)
            d_input_shape = test_data_output.shape[1]
        pseudo_model = bank_pseudo(input_dim=vfl_input_dim, output_dim = d_input_shape)
        discriminator = bank_discriminator(input_dim = d_input_shape)
        pseudo_inverse_model = bank_decoder(input_dim = 200, output_dim = vfl_input_dim * 2)

    target_bottom1, target_bottom2, target_top = target_bottom1.to(device), target_bottom2.to(device), target_top.to(device)
    pseudo_model = pseudo_model.to(device)
    discriminator = discriminator.to(device)
    pseudo_inverse_model = pseudo_inverse_model.to(device)



    # 初始化服务器和客户端
    pas_client = Client(target_bottom1)
    act_client = Client(target_bottom2)
    act_server = Server(target_top, cat_dimension)

    # 初始化优化器
    # 对于cifar10 lr dlr都是1e-4
    # if args.dataset == 'cifar10':
    pas_client_optimizer = optim.Adam(target_bottom1.parameters(), lr=1e-3)
    act_client_optimizer = optim.Adam(target_bottom2.parameters(), lr=1e-3)
    act_server_optimizer = optim.Adam(target_top.parameters(), lr=1e-3)
    pseudo_optimizer = optim.Adam(pseudo_model.parameters(), lr=args.lr)
    pseudo_inverse_model_optimizer = optim.Adam(pseudo_inverse_model.parameters(), lr=args.lr)
    discriminator_optimizer = optim.Adam(discriminator.parameters(), lr=args.dlr)
    # else:
    #     pas_client_optimizer = optim.Adam(target_bottom1.parameters(), lr=0.01)
    #     act_client_optimizer = optim.Adam(target_bottom2.parameters(), lr=0.01)
    #     act_server_optimizer = optim.Adam(target_top.parameters(), lr=0.01)
    #     pseudo_optimizer = optim.Adam(pseudo_model.parameters(), lr=0.001)
    #     pseudo_inverse_model_optimizer = optim.Adam(pseudo_inverse_model.parameters(), lr=0.0001)
    #     discriminator_optimizer = optim.Adam(discriminator.parameters(), lr=0.001)

    target_vflnn = VFLNN(pas_client, act_client, act_server, [pas_client_optimizer, act_client_optimizer], act_server_optimizer)

    target_iterator = iter(train_dataloader)
    shadow_iterator = iter(shadow_dataloader)

    


    for n in range(1, args.iteration+1):
        if (n-1)%int((len(train_dataset)/args.batch_size)) == 0 :        
            target_iterator = iter(train_dataloader) # 从头开始迭代
        if (n-1)%int((len(shadow_dataset)/args.batch_size)) == 0 :        
            shadow_iterator = iter(shadow_dataloader) # 从头开始迭代         
        try:
            target_data, target_label = next(target_iterator)
        except StopIteration:
            target_iterator = iter(train_dataloader)
            target_data, target_label = next(target_iterator)
        try:     
            shadow_data, shadow_label = next(shadow_iterator)
        except StopIteration:
            shadow_iterator = iter(shadow_dataloader)
            shadow_data, shadow_label = next(shadow_iterator)
        if target_data.size(0) != shadow_data.size(0):
            print("The number is not match")
            exit() 
        if args.dataset == 'bank':
            target_label = target_label.long()
            shadow_label = shadow_label.long()
        
        if args.attack == 'agn':
            # AGN攻击测试
            AGN_training(target_vflnn, pseudo_inverse_model, pseudo_inverse_model_optimizer, discriminator, discriminator_optimizer, target_data, target_label, shadow_data, device, n, cat_dimension, args)
        elif args.attack == 'fsha':
            fsha(pas_client, act_client, pseudo_model, pseudo_inverse_model, discriminator, pas_client_optimizer,pseudo_optimizer, pseudo_inverse_model_optimizer, discriminator_optimizer, target_data, target_label, device, shadow_data, shadow_label, n, cat_dimension, args)
        elif args.attack == 'our':
            target_vflnn_pas_intermediate, target_vflnn_act_intermediate = pseudo_training(target_vflnn, pseudo_model, pseudo_inverse_model, pseudo_optimizer, pseudo_inverse_model_optimizer, discriminator, discriminator_optimizer, target_data, target_label, shadow_data, shadow_label, device, n, cat_dimension, args)
            # 每隔100次迭代进行攻击测试，保存图片
            # if args.attack == True and n % 100 == 0:
            #     attack_test(pseudo_inverse_model, target_data, target_vflnn_pas_intermediate, target_vflnn_act_intermediate, device, n)

            # 下面测试伪模型的实用性
            if n % 50 == 0:
                # 正常VFL测试
                logging.critical("Start testing the accuracy of the model: \n")
                vfl_loss, vfl_acc = cal_test(target_vflnn, None, test_dataloader, device, args.dataset)
                # 伪被动客户端VFL测试
                pseudo_loss, pseudo_acc = cal_test(target_vflnn, pseudo_model, test_dataloader, device, args.dataset)
            
                logging.critical("VFL Loss: {:.4f}, VFL Acc: {:.4f},\n Pseudo Loss: {:.4f}, Pseudo Acc: {:.4f}".format(vfl_loss, vfl_acc, pseudo_loss, pseudo_acc))

if __name__ == '__main__':
    main()