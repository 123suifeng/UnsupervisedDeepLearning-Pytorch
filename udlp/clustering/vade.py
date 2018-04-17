# is there no gpu built in here???
import torch
import torch.nn as nn
from torch.nn import Parameter
import torch.nn.functional as F
import torch.optim as optim
import torchvision
from torchvision import datasets, transforms
from torch.autograd import Variable
from torchvision.utils import save_image

import numpy as np
import math
from sklearn.mixture import GaussianMixture


def cluster_acc(Y_pred, Y):
    from sklearn.utils.linear_assignment_ import linear_assignment
    assert Y_pred.size == Y.size
    D = max(Y_pred.max(), Y.max()) + 1
    w = np.zeros((D, D), dtype=np.int64)
    for i in range(Y_pred.size):
        w[Y_pred[i], Y[i]] += 1
    ind = linear_assignment(w.max() - w)
    return sum([w[i, j] for i, j in ind]) * 1.0 / Y_pred.size, w


def buildNetwork(layers, activation="relu", dropout=0):
    net = []
    for i in range(1, len(layers)):
        net.append(nn.Linear(layers[i - 1], layers[i]))
        if activation == "relu":
            net.append(nn.ReLU())
        elif activation == "sigmoid":
            net.append(nn.Sigmoid())
        if dropout > 0:
            net.append(nn.Dropout(dropout))
    return nn.Sequential(*net)


def adjust_learning_rate(init_lr, optimizer, epoch):
    lr = max(init_lr * (0.9 ** (epoch // 10)), 0.0002)
    for param_group in optimizer.param_groups:
        param_group["lr"] = lr
    return lr


log2pi = math.log(2 * math.pi)


def log_likelihood_samples_unit_gaussian(samples):
    return -0.5 * log2pi * samples.size()[1] - torch.sum(0.5 * (samples)**2, 1)


def log_likelihood_samplesImean_sigma(samples, mu, logvar):
    return -0.5 * log2pi * samples.size()[1] - torch.sum(0.5 * (samples - mu)**2 / torch.exp(logvar) + 0.5 * logvar, 1)


class VaDE(nn.Module):
    def __init__(self, input_dim=784, z_dim=10, n_centroids=10, binary=True,
                 encodeLayer=[500, 500, 2000], decodeLayer=[2000, 500, 500], debug=False):
        super(self.__class__, self).__init__()
        self.debug = debug
        self.z_dim = z_dim
        self.n_centroids = n_centroids
        self.encoder = buildNetwork([input_dim] + encodeLayer)
        self.decoder = buildNetwork([z_dim] + decodeLayer)
        self._enc_mu = nn.Linear(encodeLayer[-1], z_dim)
        self._enc_log_sigma = nn.Linear(encodeLayer[-1], z_dim)
        self._dec = nn.Linear(decodeLayer[-1], input_dim)
        self._dec_act = None
        if binary:
            self._dec_act = nn.Sigmoid()

        self.create_gmmparam(n_centroids, z_dim)

    def create_gmmparam(self, n_centroids, z_dim):

        # there is a centroid for each class of brain, like there is centroid
        # for each numeral

        # which means for each centroid there will be a different latent vector z
        # each latent vector z is a sample from mu_c, sigma_c

        # theta.size() == [n_centroids]
        # might theta be the weight of the centroid eg. pi

        # all centroids are given equal weighting at beginning
        self.theta_p = nn.Parameter(torch.ones(n_centroids) / n_centroids)

        # u_p.size() == [z_dim, n_centroids]
        # each z_i will have its a u_centroid vector

        self.u_p = nn.Parameter(torch.zeros(z_dim, n_centroids))

        # lambda_p.size() == [z_dim, n_controids]
        # lambda_p are the covariances for the centroids,
        # each z_i will have variances for each centroid
        self.lambda_p = nn.Parameter(torch.ones(z_dim, n_centroids))

    def initialize_gmm(self, dataloader):
        """
        after VaDE object is initialized the user calls this,
        and then the fit method
        and that is the _only_ time it is called.

        gmm need to be initialized properly to get them to grow properly

        if self.debug, returns all of the latent variables from an epoch

        """
        use_cuda = torch.cuda.is_available()
        if use_cuda:
            self.cuda()

        # puts the module in eval mode
        self.eval()
        data = []
        print('starting forward loop ***************')
        for batch_idx, inputs in enumerate(dataloader):
            inputs = inputs.view(inputs.size(0), -1).float()
            if use_cuda:
                inputs = inputs.cuda()
            inputs = Variable(inputs)
            z, outputs, mu, logvar = self.forward(inputs)
            if self.debug and (batch_idx % 200 == 0):
                print(f'z size = {z.size()}')
                print(f'outputs size = {outputs.size()}')
                print(f'mu size = {mu.size()}')
                print(f'logvar size = {logvar.size()}')

            # i think this moves data back to cpu memory?
            # and appends it to the data list
            data.append(z.data.cpu().numpy())

        print('ending forward loop ***************')

        data = np.concatenate(data)
        if self.debug:
            print(f"in initialize_gmm: data.shape = {data.shape}")
        gmm = GaussianMixture(n_components=self.n_centroids, covariance_type='diag')
        gmm.fit(data)
        # u_p are the means of the centroids [z_dim, num_centroids]
        # lambda_p are the covariances of the centroids

        # gmm should return a matrix (features x centroids) x [mean, Sigma]:

        self.u_p.data.copy_(torch.from_numpy(gmm.means_.T.astype(np.float32)))
        self.lambda_p.data.copy_(torch.from_numpy(gmm.covariances_.T.astype(np.float32)))
        if self.debug:
            #            print(f'gmm.means_ {gmm.means_}')
            print(f"self.u_p = {self.u_p.shape}")
#            print(f"self.u_p = {self.u_p.shape}")
#            print(f"self.u_p = {self.u_p}")
            return data

    def reparameterize(self, mu, logvar):
        """
        interestingly, we add noise (eps.mul(std).add_(mu)) only during training
        if training return a sample from N(mu, exp(logvar))
        if not return mu
        """
        if self.training:
            # var = \sigma^2 so exp(0.5 log var) = exp(0.5 2 log std) = std
            std = logvar.mul(0.5).exp_()

            eps = Variable(std.data.new(std.size()).normal_())
            # num = np.array([[ 1.096506  ,  0.3686553 , -0.43172026,  1.27677995,  1.26733758,
            #       1.30626082,  0.14179629,  0.58619505, -0.76423112,  2.67965817]], dtype=np.float32)
            # num = np.repeat(num, mu.size()[0], axis=0)
            # eps = Variable(torch.from_numpy(num))
            return eps.mul(std).add_(mu)
        else:
            return mu

    def decode(self, z):
        """
        this is our f(z,\theta)
        ? why does _dec(h) exist?
        
        _dec(h) might exist because this code deals with binary and not real values in
        x, or not. not sure.
        """
        h = self.decoder(z)
        x = self._dec(h)
        if self._dec_act is not None:
            x = self._dec_act(x)
        return x

    def get_gamma(self, z, z_mean, z_log_var):
        """
        gamma is our P(c|x) or something or q(c|z)
        """
        # for k,v in zip("z z_mean z_log_var".split( ),[z, z_mean, z_log_var]):
        #print(k + " size() = ", v.size())
        Z = z.unsqueeze(2).expand(z.size()[0], z.size()[1], self.n_centroids)  # NxDxK
        z_mean_t = z_mean.unsqueeze(2).expand(z_mean.size()[0], z_mean.size()[1], self.n_centroids)
        z_log_var_t = z_log_var.unsqueeze(2).expand(
            z_log_var.size()[0], z_log_var.size()[1], self.n_centroids)
        u_tensor3 = self.u_p.unsqueeze(0).expand(
            z.size()[0], self.u_p.size()[0], self.u_p.size()[1])  # NxDxK
        lambda_tensor3 = self.lambda_p.unsqueeze(0).expand(
            z.size()[0], self.lambda_p.size()[0], self.lambda_p.size()[1])
        theta_tensor2 = self.theta_p.unsqueeze(0).expand(z.size()[0], self.n_centroids)  # NxK
        
        # def of gamma implies p_c_z = p(c)p(z|c)
        # p(c) = theta ??
        # p(z|c) = N(z| u_c, sigma_c)
        # log(ab)= log(a) + log(b)
        # 
        # theta / \sigma (\sqrt{})
        p_c_z = torch.exp(
            torch.log(theta_tensor2) - # why wouldn't this be '+', unless its + -() . yup, that's it 
              torch.sum(0.5 * torch.log(2 * math.pi * lambda_tensor3) + # good. or at least the same
                       (Z - u_tensor3)**2 / (2 * lambda_tensor3), dim=1)) + 1e-10  # NxK
        gamma = p_c_z / torch.sum(p_c_z, dim=1, keepdim=True) # eq 16

        return gamma

    def loss_function(self, recon_x, x, z, z_mean, z_log_var, debug=False):
        """
        loss_function is the ELBO + reconstruction error

        u_p are the centroid means
        lambda_p are the centroid variances
        the centroids are a function of a batch of z's

        NxDxK : N - samples in a batch, D - dimensions of z, K - centroids in gmm
        
        in the paper we have
                     
        x -> g() -> tilde(u,sigma) -++>   gmm(?)    -> pi, u_c, sigma_c ? -> z -f-> u_x,sigma_x -> 
        
        x_i - original input |x| = D
        bf_u^l_x - generated by decoder from l-th sample z (sampled from q(z|x))
        bf_tilde_u - generated by encoder from x
        bf_tilde_sigma - generated by encoder from x (see eq 10)
            q(bf_z|bf_x) = N(bf_z|bf_tilde_u, bf_tilde_sigma)
        bf_sigma_c - umm p(bf_z| bf_u_c, bf_sigma_c)... and comme from gmm (given bf_z, i think)
        bf_u_c
        gamma_c = p(c|x)?
        pi_c - simply p(c) ?
        D is dimensionality of x and bf_u_x (= |x|)
        J is the dimensionality of bf_u_c, bf_sigma_c, bf_tilde_u, bf_tilde_sigma  (= |z|)
        L is the number of monte carlo samples of 
        
        
        """
        # NxD -> NxDxK
        Z = z.unsqueeze(2).expand(-1, -1, self.n_centroids)  # this is better
        #print(f'Z shape {Z.size()}')
        #print('Z', Z)
        if self.debug:
            Z1 = z.unsqueeze(2).expand(z.size()[0], z.size()[1], self.n_centroids)  # NxDxK
            assert((Z == Z1).all())

        # NxD -> NxDxK
        z_mean_t = z_mean.unsqueeze(2).expand(-1, -1, self.n_centroids)
#        z_mean_t = z_mean.unsqueeze(2).expand(z_mean.size()[0], z_mean.size()[1], self.n_centroids)

        # NxD -> NxDxK
        z_log_var_t = z_log_var.unsqueeze(2).expand(-1, -1, self.n_centroids)
#       z_log_var_t = z_log_var.unsqueeze(2).expand(z_log_var.size()[0], z_log_var.size()[1], self.n_centroids)

        # DxK -> NxDxK
        # self.u_p are centroid means
        u_tensor3 = self.u_p.unsqueeze(0).expand(z.size(0), -1, -1)  # NxDxK
#        u_tensor3 = self.u_p.unsqueeze(0).expand(z.size()[0], self.u_p.size()[0], self.u_p.size()[1]) # NxDxK

        # lambda_tensor3 is the centroid variances expanded to batch format
        lambda_tensor3 = self.lambda_p.unsqueeze(0).expand(z.size(0), -1, -1)
#        lambda_tensor3 = self.lambda_p.unsqueeze(0).expand(z.size()[0], self.lambda_p.size()[0], self.lambda_p.size()[1])

        # K -> NxK
        theta_tensor2 = self.theta_p.unsqueeze(0).expand(z.size(0), self.n_centroids)  # NxK
#        theta_tensor2 = self.theta_p.unsqueeze(0).expand(z.size()[0], self.n_centroids)  # NxK


# this is correct: it is p(c)p(z|c) = theta * N(z|u_c, sigma_c)
        # log p(z|c)
        log_norm = -0.5 * (torch.log(2* math.pi * lambda_tensor3) + (Z - u_tensor3)**2/lambda_tensor3 )
        # log p(c)p(z|c)
        p_c_z = torch.exp(torch.log(theta_tensor2) +  # + -() ..
                          torch.sum(log_norm, dim=1)) + 1e-10 # NxK
        if self.debug:
            #            print(f"lambda_p = {self.lambda_p}")
            print(f"p_c_z = {p_c_z}")
            print(f"log_norm.max() = {log_norm.max()}")
          #  print(f"log_num.max() = {log_num.max()}")
          #  print(f"log_tot_mass.min() = {log_tot_mass.min()}")
          #  print(f"log_num.min() = {log_num.min()}")
          #  print(f"N() = - log_tot_mass - log_num = {(- log_tot_mass - log_num)[:,:5,:]}")
           # print(f"torch.sum(log_tot_mass + log_num, 1) = {torch.sum(log_tot_mass + log_num, 1)}")
            print(f"theta_tensor2.abs().max() {theta_tensor2.abs().max()}")
        # p_c_z not used below this line... so gamma is a normalized p_c_z
        
        # see eq 16 (perfect match) ... qcx == pcz = VVV (only if p_c_z really means p(c)*p(z|c))
        gamma = p_c_z / torch.sum(p_c_z, dim=1, keepdim=True)  # NxK

        # PROBLEM: i think we need MSE here??
#        BCE = -torch.sum(x*torch.log(torch.clamp(recon_x, min=1e-10))+
#            (1-x)*torch.log(torch.clamp(1-recon_x, min=1e-10)), 1)o


        # so this is wrong because we will need to some Monte Carlo sampling or something 
        # see eq 2 in section 3.1
        SSE = torch.mean((x - recon_x)**2, 1)
        
        # eq 5 ? N(z| u_c, sigma_c )
        logpzc = torch.sum(0.5 * gamma * torch.sum(math.log(2 * math.pi) + torch.log(lambda_tensor3) +
                                                   torch.exp(z_log_var_t) / lambda_tensor3 +
                                                   (z_mean_t - u_tensor3)**2 / lambda_tensor3, #
                                                   dim=1),
                           dim=1)
        # last term in eq 12 ????
        qentropy = -0.5 * torch.sum(1 + z_log_var + math.log(2 * math.pi), 1)

        # 
        logpc = -torch.sum(torch.log(theta_tensor2) * gamma, 1)

        # almost term -2 in eq 12 
        logqcx = torch.sum(torch.log(gamma) * gamma, 1)

        # Normalise by same number of elements as in reconstruction
        if self.debug:
            print(f"gamma.shape = {gamma.shape}")
            print(f"gamma = {gamma}")
            print(f"x.size() = {x.size()}")
            print(f"SSE = {SSE}")
            print(f"logpzc = {logpzc}")
            print(f"qentropy = {qentropy}")
            print(f"logpc = {logpc}")
            print(f"logqcx = {logqcx}")
            
        # there's only 4 terms in the papers ELBO
        loss = torch.mean(SSE + logpzc + qentropy + logpc + logqcx)

        return loss

    #===============================================================
    # below is defined according to the released code by the authors
    # However, they are incorrect in several places
    #===============================================================

    # def get_gamma(self, z, z_mean, z_log_var):
    #     Z = z.unsqueeze(2).expand(z.size()[0], z.size()[1], self.n_centroids) # NxDxK
    #     z_mean_t = z_mean.unsqueeze(2).expand(z_mean.size()[0], z_mean.size()[1], self.n_centroids)
    #     z_log_var_t = z_log_var.unsqueeze(2).expand(z_log_var.size()[0], z_log_var.size()[1], self.n_centroids)
    #     u_tensor3 = self.u_p.unsqueeze(0).expand(z.size()[0], self.u_p.size()[0], self.u_p.size()[1]) # NxDxK
    #     lambda_tensor3 = self.lambda_p.unsqueeze(0).expand(z.size()[0], self.lambda_p.size()[0], self.lambda_p.size()[1])
    #     theta_tensor3 = self.theta_p.unsqueeze(0).unsqueeze(1).expand(z.size()[0], z.size()[1], self.n_centroids) # NxDxK

    #     p_c_z = torch.exp(
    #       torch.sum(torch.log(theta_tensor3) - 
    #                       0.5*torch.log(2*math.pi*lambda_tensor3) -
    #                       (Z-u_tensor3)**2/(2*lambda_tensor3), 
    #                           dim=1)) + 1e-10 # NxK
    #     gamma = p_c_z / torch.sum(p_c_z, dim=1, keepdim=True) # NxK

    #     return gamma

    # def loss_function(self, recon_x, x, z, z_mean, z_log_var):
    #     Z = z.unsqueeze(2).expand(z.size()[0], z.size()[1], self.n_centroids) # NxDxK
    #     z_mean_t = z_mean.unsqueeze(2).expand(z_mean.size()[0], z_mean.size()[1], self.n_centroids)
    #     z_log_var_t = z_log_var.unsqueeze(2).expand(z_log_var.size()[0], z_log_var.size()[1], self.n_centroids)
    #     u_tensor3 = self.u_p.unsqueeze(0).expand(z.size()[0], self.u_p.size()[0], self.u_p.size()[1]) # NxDxK
    #     lambda_tensor3 = self.lambda_p.unsqueeze(0).expand(z.size()[0], self.lambda_p.size()[0], self.lambda_p.size()[1])
    #     theta_tensor3 = self.theta_p.unsqueeze(0).unsqueeze(1).expand(z.size()[0], z.size()[1], self.n_centroids) # NxDxK

    #     p_c_z = torch.exp(torch.sum(torch.log(theta_tensor3) - 
    #                       0.5*torch.log(2*math.pi*lambda_tensor3) -
    #                       (Z-u_tensor3)**2/(2*lambda_tensor3), 
    #                       dim=1)) + 1e-10 # NxK
    #     gamma = p_c_z / torch.sum(p_c_z, dim=1, keepdim=True) # NxK
    #     gamma_t = gamma.unsqueeze(1).expand(gamma.size(0), self.z_dim, gamma.size(1)) #

    #     BCE = -torch.sum(x*torch.log(torch.clamp(recon_x, min=1e-10))+
    #         (1-x)*torch.log(torch.clamp(1-recon_x, min=1e-10)), 1)
    
    #     logpzc = torch.sum(torch.sum(0.5*gamma_t*(self.z_dim*math.log(2*math.pi)+torch.log(lambda_tensor3)+\
    #         torch.exp(z_log_var_t)/lambda_tensor3 + (z_mean_t-u_tensor3)**2/lambda_tensor3), dim=1), dim=1)
    #     qentropy = -0.5*torch.sum(1+z_log_var+math.log(2*math.pi), 1)
    #     logpc = -torch.sum(torch.log(self.theta_p.unsqueeze(0).expand(z.size()[0], self.n_centroids))*gamma, 1)
    #     logqcx = torch.sum(torch.log(gamma)*gamma, 1)

    #     loss = torch.mean(BCE + logpzc + qentropy + logpc + logqcx)

    #     # return torch.mean(qentropy)
    #     return loss

    def forward(self, x):
        h = self.encoder(x)
        mu = self._enc_mu(h)
        logvar = self._enc_log_sigma(h)
        z = self.reparameterize(mu, logvar)
        return z, self.decode(z), mu, logvar

    def save_model(self, path):
        torch.save(self.state_dict(), path)

    def load_model(self, path):
        pretrained_dict = torch.load(path, map_location=lambda storage, loc: storage)

        # state_dict appears to be a method of nn or super class
        model_dict = self.state_dict()
        # restrict pretrained_dict to keys that already exist in state_dict
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        # update model_dict with pretained_dict values
        model_dict.update(pretrained_dict)
        self.load_state_dict(model_dict)

    def fit(self, trainloader, validloader, lr=0.001, batch_size=128, num_epochs=10,
            visualize=False, anneal=False):
        use_cuda = torch.cuda.is_available()
        if use_cuda:
            self.cuda()

        optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.parameters()), lr=lr)

        # validate
        self.eval()
        valid_loss = 0.0
        for batch_idx, inputs in enumerate(validloader):
            inputs = inputs.view(inputs.size(0), -1).float()
            if use_cuda:
                inputs = inputs.cuda()
            inputs = Variable(inputs)
            # inputs are the orginal images (in batches (why 4, not 2?))
            z, outputs, mu, logvar = self.forward(inputs)
            if self.debug:
                print(f'z size : {z.size()}')
                print(f'outputs size : {outputs.size()}')
                print(f'inputs size : {inputs.size()}')
                print(f'mu size : {mu.size()}')
            loss = self.loss_function(outputs, inputs, z, mu, logvar, debug=True)
            valid_loss += loss.data[0] * len(inputs)
            # total_loss += valid_recon_loss.data[0] * inputs.size()[0]
            # total_num += inputs.size()[0]

        # valid_loss = total_loss / total_num
        print("#Epoch -1: Valid Loss: %.5f" % (valid_loss / len(validloader.dataset)))

        for epoch in range(num_epochs):
            # train 1 epoch
            self.train()
            if anneal:
                epoch_lr = adjust_learning_rate(lr, optimizer, epoch)
            train_loss = 0
            for batch_idx, inputs in enumerate(trainloader):
                inputs = inputs.view(inputs.size(0), -1).float()
                if use_cuda:
                    inputs = inputs.cuda()
                optimizer.zero_grad()
                inputs = Variable(inputs)

                z, outputs, mu, logvar = self.forward(inputs)
                loss = self.loss_function(outputs, inputs, z, mu, logvar)
                train_loss += loss.data[0] * len(inputs)
                loss.backward()
                optimizer.step()
                # print("    #Iter %3d: Reconstruct Loss: %.3f" % (
                #     batch_idx, recon_loss.data[0]))

            # validate
            self.eval()
            valid_loss = 0.0
            Y = []
            Y_pred = []
            for batch_idx, inputs in enumerate(validloader):  # remove labels
                inputs = inputs.view(inputs.size(0), -1).float()
                if use_cuda:
                    inputs = inputs.cuda()
                inputs = Variable(inputs)
                z, outputs, mu, logvar = self.forward(inputs)

                loss = self.loss_function(outputs, inputs, z, mu, logvar)
                valid_loss += loss.data[0] * len(inputs)
                # total_loss += valid_recon_loss.data[0] * inputs.size()[0]
                # total_num += inputs.size()[0]
                gamma = self.get_gamma(z, mu, logvar).data.cpu().numpy()
#                Y.append(labels.numpy())
#                Y_pred.append(np.argmax(gamma, axis=1))

                # view reconstruct
                if visualize and batch_idx == 0:
                    n = min(inputs.size(0), 8)
                    comparison = torch.cat([inputs.view(-1, 1, 28, 28)[:n],
                                            outputs.view(-1, 1, 28, 28)[:n]])
                    save_image(comparison.data.cpu(),
                               'results/vae/reconstruct/reconstruction_' + str(epoch) + '.png', nrow=n)

#            Y = np.concatenate(Y)
#            Y_pred = np.concatenate(Y_pred)
#            acc = cluster_acc(Y_pred, Y)
            # valid_loss = total_loss / total_num
            print("#Epoch %3d: lr: %.5f, Train Loss: %.5f, Valid Loss: %.5f, acc: %.5f" % (
                epoch, epoch_lr, train_loss / len(trainloader.dataset), valid_loss / len(validloader.dataset), 0))  # acc[0]

            # view sample
            if visualize:
                sample = Variable(torch.randn(64, self.z_dim))
                if use_cuda:
                    sample = sample.cuda()
                sample = self.decode(sample).cpu()
                save_image(sample.data.view(64, 1, 28, 28),
                           'results/vae/sample/sample_' + str(epoch) + '.png')

    def log_marginal_likelihood_estimate(self, x, num_samples):
        weight = torch.zeros(x.size(0))
        for i in range(num_samples):
            z, recon_x, mu, logvar = self.forward(x)
            zloglikelihood = log_likelihood_samples_unit_gaussian(z)
            dataloglikelihood = torch.sum(x * torch.log(torch.clamp(recon_x, min=1e-10)) +
                                          (1 - x) * torch.log(torch.clamp(1 - recon_x, min=1e-10)), 1)
            log_qz = log_likelihood_samplesImean_sigma(z, mu, logvar)
            weight += torch.exp(dataloglikelihood + zloglikelihood - log_qz).data
        # pdb.set_trace()
        return torch.log(torch.clamp(weight / num_samples, min=1e-40))
