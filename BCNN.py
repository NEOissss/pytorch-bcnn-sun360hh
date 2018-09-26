import csv
import json
from datetime import datetime
import numpy as np
from scipy import misc
import torch
import torchvision.models as models

class BilinearAlex(torch.nn.Module):
    def __init__(self, freeze=None):
        torch.nn.Module.__init__(self)
        self.features = models.alexnet(pretrained=True).features
        bfc_list = list(models.alexnet().classifier.children())[:-1]
        bfc_list.append(torch.nn.Linear(4096, 512))
        self.bfc = torch.nn.Sequential(*bfc_list)
        self.fc = torch.nn.Linear(512**2, 512)

        # Freeze layers.
        if freeze:
            self._freeze(freeze)

        # Initialize the last bfc layers.
        torch.nn.init.kaiming_normal_(self.bfc[-1].weight.data)
        if self.bfc[-1].bias is not None:
            torch.nn.init.constant_(self.bfc[-1].bias.data, val=0)

        # Initialize the fc layers.
        torch.nn.init.kaiming_normal_(self.fc.weight.data)
        if self.fc.bias is not None:
            torch.nn.init.constant_(self.fc.bias.data, val=0)

    def forward(self, X):
        X = X.float()
        N = X.size()[0]
        assert X.size() == (N, 3, 227, 227)
        X = self.features(X)
        X = X.view(N, 256 * 6 * 6)
        X = self.bfc(X)
        assert X.size() == (N, 512)
        X = X.view(N, -1, 512)
        X = torch.matmul(torch.transpose(X, 1, 2), X)
        assert X.size() == (N, 512, 512)
        X = X.view(N, 512**2)
        X = self.fc(X)
        assert X.size() == (N, 512)
        return X

    def _freeze(self, option):
        if option == 'part':
            for param in self.features.parameters():
                param.requires_grad = False
            for layer in self.bfc[:-1]:
                for param in layer.parameters():
                    param.requires_grad = False
        elif option == 'all':
            for param in self.features.parameters():
                param.requires_grad = False
            for param in self.bfc.parameters():
                param.requires_grad = False
            for param in self.fc.parameters():
                param.requires_grad = False
        else:
            raise ValueError('Unavailable freeze option.')


class BilinearAlexManager(object):
    def __init__(self, freeze='part', param_path=None):
        self._net = torch.nn.DataParallel(BilinearAlex(freeze=freeze)).cuda()
        print(self._net)
        # Load parameters
        if param_path:
            self._load(param_path)
        # Criterion.
        self._margin = 1.0
        self._criterion = torch.nn.TripletMarginLoss(margin = self._margin).cuda()
        # Batch size
        self._batch = 1
        # Epoch
        self._epoch = 1
        # If not test
        if freeze != 'all':
            # Solver.
            self._solver = torch.optim.SGD(filter(lambda p: p.requires_grad, self._net.parameters()), lr=0.001, momentum=0.9)
            self._scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self._solver, mode='max', factor=0.1, patience=3, verbose=True, threshold=1e-4)

    def train(self):
        """Train the network."""
        print('Training.')
        for t in range(self._epoch):
            epoch_loss = []
            num_correct = 0
            num_total = 0
            iter_num = 0
            for a, p, n in self._data_loader():
                # Data.
                A, P, N = self._image_loader(a, p, n)
                # Clear the existing gradients.
                self._solver.zero_grad()
                # Forward pass.
                feat_a = self._net(A)
                feat_p = self._net(P)
                feat_n = self._net(N)
                loss = self._criterion(feat_a, feat_p, feat_n)
                epoch_loss.append(loss.data[0])
                # Backward pass.
                loss.backward()
                self._solver.step()
                iter_num += 1
            if iter_num%50 == 0:
                print('Triplet loss ', epoch_loss[-1])
        self._save()

    def test(self):
        print('Testing.')
        num_correct = 0
        num_total = 0
        for a, p, n in self._data_loader(train=False):
            # Data.
            A, P, N = self._image_loader(a, p, n)
            # Forward pass.
            feat_a = self._net(A).detach().numpy()
            feat_p = self._net(P).detach().numpy()
            feat_n = self._net(N).detach().numpy()
            num_correct += ((((feat_a-feat_p)**2).sum(axis=1) - ((feat_a-feat_n)**2).sum(axis=1) + self._margin) <= 0).sum()
            num_total += len(a)
        print('Test accuracy ', num_correct/num_total)


    def _data_loader(self, train=True):
        if train:
            return sun360h_data_load(part='train', batch=self._batch)
        else:
            return sun360h_data_load(part='test', batch=self._batch)

    def _image_loader(self, a, p, n):
        k = len(a)
        a_numpy = np.ndarray([k, 3, 227, 227])
        p_numpy = np.ndarray([k, 3, 227, 227])
        n_numpy = np.ndarray([k, 3, 227, 227])
        for i in range(k):
            a_numpy[i,:,:,:] = np.transpose(misc.imresize(misc.imread(a[i]), size=(227,227,3)), (2,0,1))
            p_numpy[i,:,:,:] = np.transpose(misc.imresize(misc.imread(p[i]), size=(227,227,3)), (2,0,1))
            n_numpy[i,:,:,:] = np.transpose(misc.imresize(misc.imread(n[i]), size=(227,227,3)), (2,0,1))
        return torch.from_numpy(a_numpy), torch.from_numpy(p_numpy), torch.from_numpy(n_numpy)

    def _save(self):
        PATH = './bcnn-param-' + datetime.now().strftime('%Y%m%d%H%M%S')
        torch.save(self._net.state_dict(), PATH)
        print('Model parameters saved: ' + PATH)

    def _load(self, PATH):
        self._net.load_state_dict(torch.load(PATH))
        print('Model parameters loaded: ' + PATH)

def sun360h_data_load(part='train', ver=0, batch=1):
    root_path = '/mnt/nfs/scratch1/gluo/SUN360/HalfHalf/'
    imgs_path = '/IMGs/'

    if part=='train' or part=='test':
        task_path = 'task_' + part
        gt_path = 'gt_' + part
    else:
        raise ValueError('Unavailable dataset part!')

    if ver==0:
        task_path += '/'
        gt_path += '.csv'
    elif ver==1:
        task_path += '_v1/'
        gt_path += '_v1.csv'
    elif ver==2:
        task_path += '_v2/'
        gt_path += '_v2.csv'
    else:
        raise ValueError('Unavailable dataset version!')

    with open(root_path + gt_path, 'r') as csv_file:
        gt_list = list(csv.reader(csv_file, delimiter=','))
        gt_len = len(gt_list)

    result = []
    idx = np.random.permutation(gt_len)

    for i in range(0, gt_len, batch):
        a_bacth, p_batch, n_batch = [], [], []
        for j in idx[i:min(i+batch, gt_len)]:
            with open(root_path + task_path + gt_list[j][0] + '.json', 'r') as f:
                names = json.load(f)
                a_bacth.append(root_path + imgs_path + names[0])
                p_batch.append(root_path + imgs_path + names[1][int(gt_list[j][1])])
                n_batch.append(root_path + imgs_path + names[1][[k for k in range(10) if k!=int(gt_list[j][1])][np.random.randint(9)]])
        result.append([a_bacth, p_batch, n_batch])

    return result

def main():
    bcnn = BilinearAlexManager()
    bcnn.train()
    bcnn.test()

if __name__ == '__main__':
    main()
